from __future__ import annotations

import argparse
import ast
import datetime as dt
import fcntl
import hashlib
import importlib.util
import io
import inspect
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from typing import Any
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "platform" / "tools" / "platform_backup_restore_drill.py"
SPEC = importlib.util.spec_from_file_location("platform_backup_restore_drill", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
backup_drill = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backup_drill
SPEC.loader.exec_module(backup_drill)


class StagedWorkerInput:
    """Expose START first and DONE only after the worker emits its candidate."""

    def __init__(self) -> None:
        self._reads = [backup_drill.SCRATCH_START_TOKEN, backup_drill.SCRATCH_DONE_TOKEN]

    def read(self, size: int = -1) -> bytes:
        if not self._reads:
            return b""
        value = self._reads.pop(0)
        if size >= 0 and len(value) > size:
            self._reads.insert(0, value[size:])
            return value[:size]
        return value


class PlatformBackupRestoreDrillTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_proc_start_time = backup_drill._scratch_process_start_time
        self._proc_start_time_patch = mock.patch.object(
            backup_drill, "_scratch_process_start_time", return_value="123456"
        )
        self._proc_start_time_patch.start()
        self.addCleanup(self._proc_start_time_patch.stop)

    def test_scratch_process_start_time_parses_binary_proc_stat_safely(self) -> None:
        actual = self._original_proc_start_time(str(os.getpid()))
        self.assertRegex(actual, r"^[0-9]+$")

        tail = [b"S", *([b"0"] * 18), b"987654"]
        fixture = b"123 (comm with ) paren) " + b" ".join(tail) + b"\n"
        with mock.patch.object(
            pathlib.Path, "open", return_value=io.BytesIO(fixture)
        ):
            self.assertEqual(self._original_proc_start_time("123"), "987654")

        malformed = (
            b"123 (comm) S 1 2\n",
            b"123 (comm) " + b" ".join([b"S", *([b"0"] * 18), b"12x"]) + b"\n",
            b"123 (comm) " + b" ".join([b"S", *([b"0"] * 18), b"\xff"]) + b"\n",
            b"123 comm S " + b"0 " * 20,
            b"x" * 8193,
        )
        for payload in malformed:
            with self.subTest(size=len(payload)):
                with mock.patch.object(
                    pathlib.Path, "open", return_value=io.BytesIO(payload)
                ):
                    with self.assertRaisesRegex(RuntimeError, "process identity is malformed"):
                        self._original_proc_start_time("123")

    def test_scratch_postgres_directory_setup_without_cap_fowner(self) -> None:
        self.assertEqual(os.geteuid(), 0)
        setpriv = shutil.which("setpriv")
        self.assertIsNotNone(setpriv)
        postgres = backup_drill.pwd.getpwnam("postgres")
        with tempfile.TemporaryDirectory(prefix="backup-scratch-mode-") as temporary_dir:
            scratch_root = pathlib.Path(temporary_dir) / "scratch"
            scratch_root.mkdir(mode=0o700)
            setup_prefix = "\n".join(
                (
                    "import importlib.util, json, os, pathlib, sys",
                    "source, root, uid, gid = sys.argv[1:]",
                    "uid, gid = int(uid), int(gid)",
                    "status = pathlib.Path('/proc/self/status').read_text()",
                    "effective = int(next(line.split()[1] for line in status.splitlines() if line.startswith('CapEff:')), 16)",
                    "assert effective == 0xC3, 'worker capability set differs from the reviewed four-capability boundary'",
                    "root = pathlib.Path(root)",
                    "os.chown(root, uid, gid)",
                )
            )
            old_order_script = setup_prefix + "\n" + "\n".join(
                (
                    "data = root / 'data'",
                    "data.mkdir(mode=0o700)",
                    "os.chown(data, uid, gid)",
                    "try:",
                    "    os.chmod(data, 0o700)",
                    "except OSError as exc:",
                    "    print(json.dumps({'stage': 'chmod', 'errno': exc.errno}))",
                    "else:",
                    "    raise AssertionError('old chown-before-chmod order unexpectedly passed')",
                )
            )
            old_order = subprocess.run(
                [str(setpriv), "--bounding-set=-all,+chown,+setuid,+setgid,+dac_override", "--", sys.executable,
                 "-c", old_order_script, str(SCRIPT_PATH), str(scratch_root),
                 str(postgres.pw_uid), str(postgres.pw_gid)],
                check=False, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(old_order.returncode, 0, old_order.stderr)
            self.assertEqual(
                json.loads(old_order.stdout),
                {"stage": "chmod", "errno": 1},
            )

            scratch_root = pathlib.Path(temporary_dir) / "scratch-fixed"
            scratch_root.mkdir(mode=0o700)
            fixed_order_script = setup_prefix + "\n" + "\n".join(
                (
                    "spec = importlib.util.spec_from_file_location('backup_drill', source)",
                    "module = importlib.util.module_from_spec(spec)",
                    "sys.modules[spec.name] = module",
                    "spec.loader.exec_module(module)",
                    "for name in ('data', 'socket'):",
                    "    module._scratch_prepare_postgres_directory(root / name, uid=uid, gid=gid)",
                )
            )
            fixed_order = subprocess.run(
                [str(setpriv), "--bounding-set=-all,+chown,+setuid,+setgid,+dac_override", "--", sys.executable,
                 "-c", fixed_order_script, str(SCRIPT_PATH), str(scratch_root),
                 str(postgres.pw_uid), str(postgres.pw_gid)],
                check=False, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(fixed_order.returncode, 0, fixed_order.stderr)
            for name in ("data", "socket"):
                target = scratch_root / name
                target_stat = target.stat()
                with self.subTest(path=name):
                    self.assertEqual(target_stat.st_uid, postgres.pw_uid)
                    self.assertEqual(target_stat.st_gid, postgres.pw_gid)
                    self.assertEqual(target_stat.st_mode & 0o777, 0o700)

    @staticmethod
    def _write_backup_pair(
        output_dir: pathlib.Path,
        dump_name: str,
        payload: bytes,
        *,
        restore_verified: bool = True,
    ) -> tuple[pathlib.Path, pathlib.Path]:
        dump_path = output_dir / dump_name
        metadata_path = dump_path.with_suffix(".json")
        dump_path.write_bytes(payload)
        metadata_path.write_text(
            json.dumps(
                {
                    "format_version": 2,
                    "dump_file": dump_path.name,
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "restore_verified": restore_verified,
                }
            ),
            encoding="utf-8",
        )
        return dump_path, metadata_path

    def _assert_latest_verification_preflight_rejects(
        self,
        output_dir: pathlib.Path,
        env_file: pathlib.Path,
        message: str,
    ) -> None:
        with mock.patch.object(backup_drill, "require_commands") as require_commands:
            with self.assertRaisesRegex((RuntimeError, OSError), message):
                backup_drill.verify_latest_existing_backup(
                    output_dir, env_file, max_age_hours=24
                )
        require_commands.assert_not_called()

    def test_parse_database_url_accepts_platformdb_and_decodes_credentials(self) -> None:
        target = backup_drill.parse_database_url(
            "postgresql+asyncpg://platform%5Fuser:p%40ss@127.0.0.1:5433/platformdb"
        )

        self.assertEqual(target.username, "platform_user")
        self.assertEqual(target.password, "p@ss")
        self.assertEqual(target.host, "127.0.0.1")
        self.assertEqual(target.port, 5433)
        self.assertEqual(target.database, "platformdb")

    def test_parse_database_url_refuses_legacy_database(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected the isolated platformdb"):
            backup_drill.parse_database_url(
                "postgresql+asyncpg://platform_user:secret@127.0.0.1:5432/sparkydb"
            )

    def test_scratch_terminal_diagnostic_schema_is_closed_and_keeps_initial_state(self) -> None:
        initial_snapshot = {
            "properties": {
                "ActiveState": "active",
                "SubState": "running",
                "Result": "success",
                "ExecMainStatus": "0",
            }
        }
        terminal_snapshot = {
            "load_state": "not-found",
            "active_state": "inactive",
            "sub_state": "dead",
            "result": "success",
            "exec_main_status": 0,
            "start_token_sent": False,
            "completion_token_sent": False,
        }
        diagnostic = backup_drill._scratch_unit_diagnostic(
            initial_snapshot,
            launcher_exit_status=1,
            stdout_prefix=b"",
            stdout_observed=0,
            stdout_complete=True,
            stderr_prefix=b"",
            stderr_observed=0,
            stderr_complete=True,
            phase="confinement_verification_failed",
            verification_error="os_error",
            verification_errno=2,
            terminal_snapshot=terminal_snapshot,
        )
        self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(diagnostic))
        assert diagnostic is not None
        self.assertEqual(diagnostic["schema"], 2)
        self.assertEqual(diagnostic["active_state"], "active")
        self.assertEqual(diagnostic["terminal_snapshot"]["active_state"], "inactive")

        schema1 = dict(diagnostic)
        schema1["schema"] = 1
        del schema1["terminal_snapshot"]
        self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(schema1))
        for malformed_terminal in (
            {**terminal_snapshot, "private_path": "/root/private"},
            {**terminal_snapshot, "start_token_sent": 1},
            {**terminal_snapshot, "active_state": "unknown-secret-state"},
        ):
            malformed = {**diagnostic, "terminal_snapshot": malformed_terminal}
            self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))

        required_bytes = backup_drill.SCRATCH_MEMORY_ADMISSION_BYTES
        guard_sites = (
            "parent_before_archive_open",
            "parent_before_unit_launch",
            "parent_active_poll",
            "parent_after_worker_cleanup",
            "worker_after_start_before_archive_open",
            "worker_after_initdb_before_postgres_start",
            "worker_after_postgres_start",
            "worker_pg_restore_resource_guard",
            "worker_after_restore_before_postgres_stop",
        )
        for guard_site in guard_sites:
            host_guard = {
                "schema": 2,
                "guard_site": guard_site,
                "guard_reason": "memory_reserve",
                "available_bytes": required_bytes - 1,
                "required_bytes": required_bytes,
            }
            self.assertTrue(backup_drill.valid_scratch_host_guard_diagnostic(host_guard))
            frame = backup_drill._scratch_host_guard_frame(host_guard)
            self.assertIsNotNone(frame)
            assert frame is not None
            self.assertEqual(
                backup_drill.parse_scratch_host_guard_frame(frame, truncated=False),
                host_guard,
            )
            with mock.patch.object(
                backup_drill,
                "_scratch_mem_available_bytes",
                return_value=required_bytes - 1,
            ):
                with self.assertRaises(RuntimeError) as reserve_failure:
                    backup_drill._raise_scratch_host_memory_reserve(
                        guard_site, message="fixed host reserve failure"
                    )
            self.assertEqual(
                reserve_failure.exception.scratch_host_guard_diagnostic,
                host_guard,
            )
        valid_host_guard = {
            "schema": 2,
            "guard_site": "worker_pg_restore_resource_guard",
            "guard_reason": "memory_reserve",
            "available_bytes": required_bytes - 1,
            "required_bytes": required_bytes,
        }
        for malformed in (
            {**valid_host_guard, "guard_site": "/private/host/path"},
            {**valid_host_guard, "guard_reason": "disk_floor"},
            {**valid_host_guard, "available_bytes": required_bytes},
            {**valid_host_guard, "required_bytes": required_bytes - 1},
            {**valid_host_guard, "unexpected": "private"},
            {**valid_host_guard, "schema": True},
        ):
            self.assertFalse(backup_drill.valid_scratch_host_guard_diagnostic(malformed))

        valid_frame = backup_drill._scratch_host_guard_frame(valid_host_guard)
        assert valid_frame is not None
        failure_report = json.dumps(
            {
                "ok": False,
                "error": "fixed failure",
                "scratch_host_guard_diagnostic": valid_host_guard,
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8") + b"\n"
        self.assertEqual(
            backup_drill.parse_scratch_host_guard_failure_report(
                failure_report,
                truncated=False,
            ),
            valid_host_guard,
        )
        other_site_report = json.dumps(
            {
                "ok": False,
                "error": "fixed failure",
                "scratch_host_guard_diagnostic": {
                    **valid_host_guard,
                    "guard_site": "worker_after_postgres_start",
                },
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8") + b"\n"
        report_diagnostic = backup_drill.parse_scratch_host_guard_failure_report(
            other_site_report, truncated=False
        )
        frame_diagnostic = backup_drill.parse_scratch_host_guard_frame(
            valid_frame, truncated=False
        )
        self.assertIsNotNone(report_diagnostic)
        self.assertIsNotNone(frame_diagnostic)
        self.assertNotEqual(report_diagnostic, frame_diagnostic)
        self.assertEqual(
            backup_drill._parse_bound_scratch_host_guard_diagnostic(
                failure_report,
                valid_frame,
                stdout_truncated=False,
                stderr_truncated=False,
            ),
            valid_host_guard,
        )
        self.assertIsNone(
            backup_drill._parse_bound_scratch_host_guard_diagnostic(
                other_site_report,
                valid_frame,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        )
        self.assertIsNone(
            backup_drill._parse_bound_scratch_host_guard_diagnostic(
                b"",
                valid_frame,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        )
        for forged_or_incomplete_stdout in (
            b"bounded child output\n" + failure_report,
            failure_report + b"untrusted trailing output",
            failure_report[:-1],
            failure_report + failure_report,
            b'{"ok":false,"ok":false,"error":"x"}\n',
            b'{"ok":false,"error":"x","scratch_host_guard_diagnostic":{}}\n',
        ):
            self.assertIsNone(
                backup_drill.parse_scratch_host_guard_failure_report(
                    forged_or_incomplete_stdout,
                    truncated=False,
                )
            )
        self.assertIsNone(
            backup_drill.parse_scratch_host_guard_failure_report(
                failure_report, truncated=True
            )
        )
        report_without_diagnostic = json.dumps(
            {"ok": False, "error": "ordinary worker failure"}
        ).encode("utf-8")
        self.assertIsNone(
            backup_drill.parse_scratch_host_guard_failure_report(
                report_without_diagnostic, truncated=False
            )
        )
        self.assertIsNone(
            backup_drill.parse_scratch_host_guard_frame(
                valid_frame + valid_frame, truncated=False
            )
        )
        self.assertIsNone(
            backup_drill.parse_scratch_host_guard_frame(
                valid_frame[:-1], truncated=False
            )
        )
        self.assertIsNone(
            backup_drill.parse_scratch_host_guard_frame(
                valid_frame, truncated=True
            )
        )
        duplicate_key_frame = (
            backup_drill.SCRATCH_HOST_GUARD_FRAME_MARKER
            + b'{"schema":2,"schema":2}\n'
        )
        self.assertIsNone(
            backup_drill.parse_scratch_host_guard_frame(
                duplicate_key_frame, truncated=False
            )
        )
        self.assertIsNone(
            backup_drill.parse_scratch_host_guard_failure_report(
                b"child-forged frame exists without helper error JSON\n",
                truncated=False,
            )
        )

        worker_private_stderr = io.BytesIO()
        worker_private_stdout = io.BytesIO()
        with mock.patch.object(
            backup_drill.sys,
            "stderr",
            types.SimpleNamespace(buffer=worker_private_stderr),
        ), mock.patch.object(
            backup_drill.sys,
            "stdout",
            types.SimpleNamespace(buffer=worker_private_stdout),
        ):
            backup_drill._write_scratch_worker_private_output(
                b"private child stdout", b"private child stderr"
            )
        self.assertEqual(worker_private_stdout.getvalue(), b"")
        self.assertEqual(
            worker_private_stderr.getvalue(),
            b"private child stdoutprivate child stderr",
        )

        public_error = RuntimeError("fixed failure")
        public_error.scratch_host_guard_diagnostic = valid_host_guard
        self.assertNotIn(
            "scratch_host_guard_diagnostic",
            backup_drill.failure_report_for_exception(
                public_error, private_scratch_diagnostics=False
            ),
        )
        self.assertEqual(
            backup_drill.failure_report_for_exception(
                public_error, private_scratch_diagnostics=True
            )["scratch_host_guard_diagnostic"],
            valid_host_guard,
        )
        self.assertIsNone(
            backup_drill.private_failure_frame_for_exception(
                public_error, enabled=False
            )
        )
        private_frame = backup_drill.private_failure_frame_for_exception(
            public_error, enabled=True
        )
        self.assertIsNotNone(private_frame)
        assert private_frame is not None
        self.assertIn(
            backup_drill.SCRATCH_HOST_GUARD_FRAME_MARKER.decode("ascii"),
            private_frame,
        )
        self.assertEqual(
            backup_drill.parse_scratch_host_guard_frame(
                private_frame.encode("ascii"), truncated=False
            ),
            valid_host_guard,
        )
        self.assertNotIn("/private/host/path", private_frame)

        def call_site_constants(function: Any, function_name: str) -> list[str]:
            tree = ast.parse(inspect.getsource(function))
            result: list[str] = []
            for node in sorted(
                ast.walk(tree), key=lambda candidate: getattr(candidate, "lineno", 0)
            ):
                if not isinstance(node, ast.Call):
                    continue
                called_name = (
                    node.func.id if isinstance(node.func, ast.Name) else None
                )
                if called_name != function_name:
                    continue
                if node.args and isinstance(node.args[0], ast.Constant):
                    result.append(node.args[0].value)
                else:
                    result.extend(
                        keyword.value.value
                        for keyword in node.keywords
                        if keyword.arg == "guard_site"
                        and isinstance(keyword.value, ast.Constant)
                    )
            return result

        self.assertEqual(
            call_site_constants(
                backup_drill._run_isolated_restore_drill, "_scratch_host_guard"
            ),
            [
                "parent_before_archive_open",
                "parent_before_unit_launch",
                "parent_active_poll",
                "parent_after_worker_cleanup",
            ],
        )
        self.assertEqual(
            call_site_constants(
                backup_drill._scratch_worker_main,
                "_raise_scratch_host_memory_reserve",
            ),
            ["worker_after_start_before_archive_open"],
        )
        self.assertEqual(
            call_site_constants(backup_drill._scratch_worker_main, "_scratch_resource_guard"),
            [
                "worker_after_initdb_before_postgres_start",
                "worker_after_postgres_start",
                "worker_pg_restore_resource_guard",
                "worker_after_restore_before_postgres_stop",
            ],
        )
        self.assertEqual(
            backup_drill._scratch_host_guard_frame({**valid_host_guard, "guard_site": "unknown"}),
            None,
        )

    def _assert_scratch_unit_diagnostic_caps_and_private_envelope(self) -> None:
        required_memory = backup_drill.SCRATCH_MEMORY_ADMISSION_BYTES
        available_memory = required_memory - 4096
        admission_diagnostic = {
            "schema": 1,
            "phase": "pre_create_admission",
            "guard_reason": "memory_reserve",
            "available_bytes": available_memory,
            "required_bytes": required_memory,
            "helper_rss_bytes": 8 * 1024 * 1024,
            "direct_parent_rss_bytes": 12 * 1024 * 1024,
            "observed_pair_rss_bytes": 20 * 1024 * 1024,
            "observed_process_count": 2,
        }
        self.assertTrue(backup_drill.valid_scratch_admission_diagnostic(admission_diagnostic))
        for key, value in (
            ("schema", True),
            ("phase", "unit_exited_before_confinement"),
            ("guard_reason", "other"),
            ("available_bytes", True),
            ("available_bytes", required_memory),
            ("required_bytes", required_memory - 1),
        ):
            malformed_admission = dict(admission_diagnostic)
            malformed_admission[key] = value
            self.assertFalse(backup_drill.valid_scratch_admission_diagnostic(malformed_admission))
        malformed_admission = {**admission_diagnostic, "unexpected": 1}
        self.assertFalse(backup_drill.valid_scratch_admission_diagnostic(malformed_admission))
        for key, value in (
            ("helper_rss_bytes", True),
            ("direct_parent_rss_bytes", -1),
            ("observed_pair_rss_bytes", 1),
            ("observed_process_count", True),
            ("observed_process_count", 1),
        ):
            malformed_admission = dict(admission_diagnostic)
            malformed_admission[key] = value
            self.assertFalse(backup_drill.valid_scratch_admission_diagnostic(malformed_admission))
        one_sample = {
            **admission_diagnostic,
            "direct_parent_rss_bytes": None,
            "observed_pair_rss_bytes": None,
            "observed_process_count": 1,
        }
        self.assertTrue(backup_drill.valid_scratch_admission_diagnostic(one_sample))
        no_samples = {
            **admission_diagnostic,
            "helper_rss_bytes": None,
            "direct_parent_rss_bytes": None,
            "observed_pair_rss_bytes": None,
            "observed_process_count": 0,
        }
        self.assertTrue(backup_drill.valid_scratch_admission_diagnostic(no_samples))

        with mock.patch.object(
            backup_drill,
            "_scratch_proc_status_sample",
            side_effect=[(10, 20, 1024), (20, 1, 2048)],
        ), mock.patch.object(backup_drill.os, "getpid", return_value=10), mock.patch.object(
            backup_drill.os, "getppid", return_value=20
        ):
            rss_sample = backup_drill._scratch_admission_rss_snapshot()
        self.assertEqual(
            rss_sample,
            {
                "helper_rss_bytes": 1024,
                "direct_parent_rss_bytes": 2048,
                "observed_pair_rss_bytes": 3072,
                "observed_process_count": 2,
            },
        )
        with mock.patch.object(
            backup_drill,
            "_scratch_proc_status_sample",
            side_effect=OSError("best-effort telemetry failure"),
        ), mock.patch.object(backup_drill.os, "getpid", return_value=10), mock.patch.object(
            backup_drill.os, "getppid", return_value=20
        ):
            unavailable_rss = backup_drill._scratch_admission_rss_snapshot()
        self.assertEqual(
            unavailable_rss,
            {
                "helper_rss_bytes": None,
                "direct_parent_rss_bytes": None,
                "observed_pair_rss_bytes": None,
                "observed_process_count": 0,
            },
        )

        with (
            mock.patch.object(backup_drill, "require_restore_headroom") as headroom,
            mock.patch.object(backup_drill, "_scratch_mem_available_bytes", return_value=available_memory),
            mock.patch.object(backup_drill, "_scratch_admission_rss_snapshot", return_value={
                "helper_rss_bytes": 8 * 1024 * 1024,
                "direct_parent_rss_bytes": 12 * 1024 * 1024,
                "observed_pair_rss_bytes": 20 * 1024 * 1024,
                "observed_process_count": 2,
            }),
        ):
            with self.assertRaisesRegex(
                RuntimeError, r"Scratch restore admission memory reserve is unavailable\."
            ) as admission_failure:
                backup_drill._scratch_host_guard()
        headroom.assert_called_once_with(pathlib.Path("/"))
        self.assertEqual(
            admission_failure.exception.scratch_admission_diagnostic,
            admission_diagnostic,
        )
        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(backup_drill, "_scratch_mem_available_bytes", return_value=available_memory),
            mock.patch.object(
                backup_drill,
                "_scratch_admission_rss_snapshot",
                side_effect=RuntimeError("telemetry must not replace admission"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError, r"Scratch restore admission memory reserve is unavailable\."
            ) as telemetry_failure:
                backup_drill._scratch_host_guard()
        self.assertEqual(
            telemetry_failure.exception.scratch_admission_diagnostic,
            {
                "schema": 1,
                "phase": "pre_create_admission",
                "guard_reason": "memory_reserve",
                "available_bytes": available_memory,
                "required_bytes": required_memory,
                "helper_rss_bytes": None,
                "direct_parent_rss_bytes": None,
                "observed_pair_rss_bytes": None,
                "observed_process_count": 0,
            },
        )

        # The guard is unchanged at the exact threshold and rejects before it
        # tries to open or stage the supplied archive.
        with (
            mock.patch.object(backup_drill.os, "geteuid", return_value=0),
            mock.patch.object(backup_drill, "require_commands") as require_commands,
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(backup_drill, "_scratch_mem_available_bytes", return_value=available_memory),
            mock.patch.object(backup_drill, "_scratch_admission_rss_snapshot", return_value={
                "helper_rss_bytes": None,
                "direct_parent_rss_bytes": None,
                "observed_pair_rss_bytes": None,
                "observed_process_count": 0,
            }),
        ):
            with self.assertRaisesRegex(
                RuntimeError, r"Scratch restore admission memory reserve is unavailable\."
            ):
                backup_drill._run_isolated_restore_drill(
                    pathlib.Path("/missing-archive-must-not-be-opened.dump"),
                    timestamp_slug="20261012T000000Z",
                    private_scratch_diagnostics=True,
                )
        require_commands.assert_called_once_with("systemd-run", "systemctl", "pg_config", "runuser")

        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(
                backup_drill,
                "_scratch_mem_available_bytes",
                return_value=required_memory,
            ),
        ):
            backup_drill._scratch_host_guard()

        stdout = b"o" * (backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES + 1)
        stderr = b"e" * (backup_drill.SCRATCH_PRIVATE_STDERR_BYTES + 1)
        diagnostic = backup_drill._scratch_unit_diagnostic(
            {
                "properties": {
                    "ActiveState": "failed",
                    "SubState": "failed",
                    "Result": "exit-code",
                    "ExecMainStatus": "203",
                }
            },
            launcher_exit_status=1,
            stdout_prefix=stdout,
            stdout_observed=len(stdout),
            stdout_complete=True,
            stderr_prefix=stderr,
            stderr_observed=len(stderr),
            stderr_complete=True,
        )
        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(diagnostic))
        self.assertEqual(diagnostic["stdout_bytes"], backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES)
        self.assertEqual(diagnostic["stderr_bytes"], backup_drill.SCRATCH_PRIVATE_STDERR_BYTES)
        self.assertTrue(diagnostic["stdout_truncated"])
        self.assertTrue(diagnostic["stderr_truncated"])
        self.assertEqual(
            diagnostic["stdout_sha256"],
            hashlib.sha256(stdout[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES]).hexdigest(),
        )
        envelope = backup_drill._scratch_private_output_envelope(
            stdout[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES],
            stderr[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES],
        )
        self.assertLessEqual(len(envelope), 48 * 1024)
        self.assertLessEqual(len(envelope), 65536)
        framed_payload_bytes = 2 + backup_drill.SCRATCH_PRIVATE_RAW_BYTES
        self.assertEqual(
            len(envelope),
            len(backup_drill.SCRATCH_PRIVATE_OUTPUT_MARKER)
            + 4 * ((framed_payload_bytes + 2) // 3)
            + 1,
        )
        with self.assertRaisesRegex(ValueError, "closed envelope bound"):
            backup_drill._scratch_private_output_envelope(
                b"x" * (backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES + 1), b""
            )
        self.assertTrue(envelope.startswith(backup_drill.SCRATCH_PRIVATE_OUTPUT_MARKER))
        encoded_line = envelope[len(backup_drill.SCRATCH_PRIVATE_OUTPUT_MARKER) :].rstrip(b"\n")
        decoded = backup_drill.base64.b64decode(encoded_line, validate=True)
        stdout_size = int.from_bytes(decoded[:2], "big")
        self.assertEqual(stdout_size, backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES)
        self.assertEqual(decoded[2 : 2 + stdout_size], stdout[:stdout_size])
        self.assertEqual(decoded[2 + stdout_size :], stderr[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES])
        overflow = backup_drill._scratch_unit_diagnostic(
            {
                "properties": {
                    "ActiveState": "failed",
                    "SubState": "failed",
                    "Result": "exit-code",
                    "ExecMainStatus": "203",
                }
            },
            launcher_exit_status=1,
            stdout_prefix=b"x" * backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES,
            stdout_observed=backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES,
            stdout_complete=False,
            stderr_prefix=b"y" * 7,
            stderr_observed=7,
            stderr_complete=True,
        )
        self.assertIsNotNone(overflow)
        assert overflow is not None
        self.assertTrue(overflow["stdout_truncated"])
        self.assertEqual(overflow["stdout_bytes"], backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES)

        malformed = dict(diagnostic)
        malformed["stdout_bytes"] = backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES + 1
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))
        malformed = dict(diagnostic)
        malformed["active_state"] = []
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))
        malformed = dict(diagnostic)
        malformed["stdout_truncated"] = 1
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))
        verification_failure = dict(diagnostic)
        verification_failure.update(
            phase="confinement_verification_failed",
            active_state="active",
            verification_error="os_error",
            verification_errno=2,
        )
        self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(verification_failure))
        terminal = {
            "load_state": "loaded",
            "active_state": "inactive",
            "sub_state": "dead",
            "result": "success",
            "exec_main_status": 0,
            "start_token_sent": False,
            "completion_token_sent": False,
        }
        schema2 = backup_drill._scratch_unit_diagnostic(
            {
                "properties": {
                    "ActiveState": "active",
                    "SubState": "running",
                    "Result": "success",
                    "ExecMainStatus": "0",
                }
            },
            launcher_exit_status=1,
            stdout_prefix=b"",
            stdout_observed=0,
            stdout_complete=True,
            stderr_prefix=b"",
            stderr_observed=0,
            stderr_complete=True,
            phase="confinement_verification_failed",
            verification_error="os_error",
            verification_errno=2,
            terminal_snapshot=terminal,
        )
        self.assertIsNotNone(schema2)
        assert schema2 is not None
        self.assertEqual(schema2["schema"], 2)
        self.assertEqual(schema2["active_state"], "active")
        self.assertEqual(schema2["terminal_snapshot"], terminal)
        malformed = dict(schema2)
        malformed["terminal_snapshot"] = {**terminal, "private_path": "/secret"}
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))
        malformed = dict(verification_failure)
        malformed["verification_errno"] = True
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))
        malformed = dict(verification_failure)
        malformed["verification_error"] = "other"
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))
        malformed = dict(verification_failure)
        malformed["active_state"] = "failed"
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))
        after_confinement = dict(diagnostic)
        after_confinement.update(
            phase="unit_exited_after_confinement",
            active_state="inactive",
            sub_state="dead",
            result="exit-code",
            verification_error="none",
            verification_errno=None,
        )
        self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(after_confinement))
        malformed = dict(after_confinement)
        malformed["active_state"] = "active"
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))

        private_failure = RuntimeError("fixed backup failure")
        private_failure.scratch_unit_diagnostic = diagnostic
        private_failure.scratch_admission_diagnostic = admission_diagnostic
        private_failure.scratch_unit_private_output = (
            stdout[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES],
            stderr[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES],
        )
        cli_args = argparse.Namespace(
            as_json=True,
            private_scratch_diagnostics=True,
            check_latest=False,
            verify_latest_existing=False,
            dump_only=False,
            verify_dump=None,
        )
        private_stdout = io.StringIO()
        private_stderr = io.StringIO()
        with (
            mock.patch.object(backup_drill, "parse_args", return_value=cli_args),
            mock.patch.object(backup_drill, "create_backup", side_effect=private_failure),
            mock.patch.object(backup_drill.sys, "stdout", private_stdout),
            mock.patch.object(backup_drill.sys, "stderr", private_stderr),
        ):
            self.assertEqual(backup_drill.main(), 1)
        private_report = json.loads(private_stdout.getvalue())
        self.assertEqual(private_report["scratch_unit_diagnostic"], diagnostic)
        self.assertEqual(private_report["scratch_admission_diagnostic"], admission_diagnostic)
        self.assertNotIn(stdout[:32].decode("ascii"), private_stdout.getvalue())
        self.assertEqual(
            private_stderr.getvalue().encode("ascii"),
            backup_drill._scratch_private_output_envelope(
                stdout[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES],
                stderr[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES],
            ),
        )

        # A collected transient unit can have no typed systemd properties.
        # Its bounded output still travels only through the private frame.
        private_failure.scratch_unit_diagnostic = None
        private_failure.scratch_admission_diagnostic = None
        missing_state_stdout = io.StringIO()
        missing_state_stderr = io.StringIO()
        with (
            mock.patch.object(backup_drill, "parse_args", return_value=cli_args),
            mock.patch.object(backup_drill, "create_backup", side_effect=private_failure),
            mock.patch.object(backup_drill.sys, "stdout", missing_state_stdout),
            mock.patch.object(backup_drill.sys, "stderr", missing_state_stderr),
        ):
            self.assertEqual(backup_drill.main(), 1)
        missing_state_report = json.loads(missing_state_stdout.getvalue())
        self.assertNotIn("scratch_unit_diagnostic", missing_state_report)
        self.assertNotIn("scratch_admission_diagnostic", missing_state_report)
        self.assertNotIn(stdout[:32].decode("ascii"), missing_state_stdout.getvalue())
        self.assertEqual(
            missing_state_stderr.getvalue().encode("ascii"),
            backup_drill._scratch_private_output_envelope(
                stdout[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES],
                stderr[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES],
            ),
        )

        public_args = argparse.Namespace(**{**vars(cli_args), "private_scratch_diagnostics": False})
        public_stdout = io.StringIO()
        public_stderr = io.StringIO()
        with (
            mock.patch.object(backup_drill, "parse_args", return_value=public_args),
            mock.patch.object(backup_drill, "create_backup", side_effect=private_failure),
            mock.patch.object(backup_drill.sys, "stdout", public_stdout),
            mock.patch.object(backup_drill.sys, "stderr", public_stderr),
        ):
            self.assertEqual(backup_drill.main(), 1)
        self.assertNotIn("scratch_unit_diagnostic", public_stdout.getvalue())
        self.assertNotIn("scratch_admission_diagnostic", public_stdout.getvalue())
        self.assertEqual(public_stderr.getvalue(), "")

    def _assert_scratch_early_exit_output_is_drained(self) -> None:
        class LauncherProcess:
            def __init__(self, stdout, stderr) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, *, timeout=None):
                return self.returncode

        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            pipe_capacities = []
            for read_fd in (stdout_read, stderr_read):
                pipe_capacities.append(fcntl.fcntl(read_fd, fcntl.F_SETPIPE_SZ, 4096))
            self.assertTrue(
                all(capacity < backup_drill.SCRATCH_PRIVATE_STDERR_BYTES for capacity in pipe_capacities)
            )
            stdout_reader = os.fdopen(stdout_read, "rb", buffering=0)
            stderr_reader = os.fdopen(stderr_read, "rb", buffering=0)
            stdout_payload = b"o" * (backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES + 1)
            stderr_payload = b"reason:" + b"e" * backup_drill.SCRATCH_PRIVATE_STDERR_BYTES

            def write_payload(fd, payload):
                try:
                    view = memoryview(payload)
                    while view:
                        view = view[os.write(fd, view) :]
                finally:
                    os.close(fd)

            stdout_writer = threading.Thread(target=write_payload, args=(stdout_write, stdout_payload))
            stderr_writer = threading.Thread(target=write_payload, args=(stderr_write, stderr_payload))
            stdout_writer.start()
            stderr_writer.start()
            launcher = LauncherProcess(stdout_reader, stderr_reader)
            snapshots = [
                {
                    "properties": {
                        "LoadState": "loaded",
                        "ActiveState": "failed",
                        "SubState": "failed",
                        "Result": "exit-code",
                        "ExecMainPID": "0",
                        "ExecMainStatus": "203",
                        "ControlGroup": "/system.slice/scratch-test.service",
                    }
                },
                {
                    "properties": {
                        "LoadState": "not-found",
                        "ActiveState": "inactive",
                        "SubState": "dead",
                        "Result": "success",
                        "ExecMainStatus": "0",
                        "ExecMainPID": "0",
                        "ControlGroup": "",
                    }
                },
                {
                    "properties": {
                        "LoadState": "not-found",
                        "ActiveState": "inactive",
                        "SubState": "dead",
                        "Result": "success",
                        "ExecMainStatus": "0",
                        "ExecMainPID": "0",
                        "ControlGroup": "",
                    }
                },
            ]

            def snapshot(_unit_name):
                value = snapshots.pop(0)
                return value

            def stop_unit(_unit_name):
                launcher.returncode = 1

            unit_command = mock.Mock(return_value=["systemd-run"])
            try:
                with (
                    mock.patch.object(backup_drill, "require_commands"),
                    mock.patch.object(backup_drill, "_scratch_host_guard"),
                    mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                    mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                    mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=snapshot),
                    mock.patch.object(backup_drill, "_scratch_stop_unit", side_effect=stop_unit),
                    mock.patch.object(backup_drill.subprocess, "Popen", return_value=launcher),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "failed before its confinement was verified"
                    ) as failure:
                        backup_drill._run_isolated_restore_drill(
                            archive_path,
                            timestamp_slug="20261012T010203Z",
                        )
                stdout_writer.join(timeout=2)
                stderr_writer.join(timeout=2)

                self.assertFalse(stdout_writer.is_alive())
                self.assertFalse(stderr_writer.is_alive())
                diagnostic = failure.exception.scratch_unit_diagnostic
                self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(diagnostic))
                self.assertEqual(diagnostic["exec_main_status"], 203)
                self.assertEqual(diagnostic["launcher_exit_status"], 1)
                self.assertEqual(diagnostic["stdout_bytes"], backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES)
                self.assertTrue(diagnostic["stdout_truncated"])
                self.assertEqual(diagnostic["stderr_bytes"], backup_drill.SCRATCH_PRIVATE_STDERR_BYTES)
                self.assertTrue(diagnostic["stderr_truncated"])
                raw_stdout, raw_stderr = failure.exception.scratch_unit_private_output
                self.assertEqual(raw_stdout, stdout_payload[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES])
                self.assertEqual(raw_stderr, stderr_payload[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES])
                self.assertEqual(diagnostic["terminal_snapshot"]["load_state"], "not-found")
                mountpoint = pathlib.Path(unit_command.call_args.kwargs["mountpoint"])
                self.assertFalse(mountpoint.exists())
            finally:
                stdout_reader.close()
                stderr_reader.close()
                stdout_writer.join(timeout=2)
                stderr_writer.join(timeout=2)

    def _assert_scratch_live_identity_failure_preserves_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            expected_stdout = b"worker reached restore preflight\n"
            expected_stderr = b"worker failure detail\n"
            child_code = (
                "import sys,time; "
                f"sys.stdout.buffer.write({expected_stdout!r}); sys.stdout.flush(); "
                f"sys.stderr.buffer.write({expected_stderr!r}); sys.stderr.flush(); "
                "time.sleep(10)"
            )
            command = [sys.executable, "-c", child_code]
            unit_command = mock.Mock(return_value=command)
            real_popen = subprocess.Popen
            launched: list[subprocess.Popen[bytes]] = []
            snapshots = [
                {
                    "properties": {
                        "LoadState": "loaded",
                        "ActiveState": "active",
                        "SubState": "running",
                        "Result": "success",
                        "ExecMainPID": "424242",
                        "InvocationID": "0123456789abcdef0123456789abcdef",
                        "ExecMainStatus": "0",
                        "ControlGroup": "/system.slice/scratch-identity-race.service",
                    }
                },
                {
                    "properties": {
                        "LoadState": "not-found",
                        "ActiveState": "inactive",
                        "SubState": "dead",
                        "Result": "success",
                        "ExecMainPID": "0",
                        "ControlGroup": "",
                    }
                },
                {
                    "properties": {
                        "LoadState": "not-found",
                        "ActiveState": "inactive",
                        "SubState": "dead",
                        "Result": "success",
                        "ExecMainPID": "0",
                        "ControlGroup": "",
                    }
                },
            ]

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                launched.append(process)
                return process

            def snapshot(_unit_name):
                return snapshots.pop(0)

            def stop_unit(_unit_name):
                if launched and launched[0].poll() is None:
                    launched[0].terminate()
                    launched[0].wait(timeout=2)

            vanished = FileNotFoundError(
                2, "synthetic worker executable disappeared", "/proc/424242/exe"
            )
            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=snapshot),
                mock.patch.object(backup_drill, "_scratch_expected_properties", side_effect=vanished),
                mock.patch.object(backup_drill, "_scratch_stop_unit", side_effect=stop_unit) as stop,
                mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
                mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
            ):
                with self.assertRaises(FileNotFoundError) as failure:
                    backup_drill._run_isolated_restore_drill(
                        archive_path,
                        timestamp_slug="20261012T010206Z",
                    )

            self.assertIs(failure.exception, vanished)
            self.assertEqual(failure.exception.errno, 2)
            stop.assert_called_once()
            self.assertEqual(len(launched), 1)
            self.assertIsNotNone(launched[0].returncode)
            diagnostic = failure.exception.scratch_unit_diagnostic
            self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(diagnostic))
            self.assertEqual(diagnostic["phase"], "confinement_verification_failed")
            self.assertEqual(diagnostic["active_state"], "active")
            self.assertEqual(diagnostic["verification_error"], "os_error")
            self.assertEqual(diagnostic["verification_errno"], 2)
            captured_stdout, captured_stderr = failure.exception.scratch_unit_private_output
            self.assertEqual(captured_stdout, expected_stdout)
            self.assertEqual(captured_stderr, expected_stderr)
            mountpoint = pathlib.Path(unit_command.call_args.kwargs["mountpoint"])
            self.assertFalse(mountpoint.exists())

    def _assert_scratch_identity_replacement_cannot_receive_authorization(self) -> None:
        for replacement in ("pid", "invocation"):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temporary_dir:
                archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
                archive_path.write_bytes(b"PGDMPsynthetic archive")
                archive_path.chmod(0o600)
                command = [
                    sys.executable,
                    "-c",
                    "import sys; "
                    f"token={backup_drill.SCRATCH_START_TOKEN!r}; "
                    "ack=sys.stdin.buffer.read(len(token)); "
                    "assert ack == token; print('authorized',flush=True)",
                ]
                unit_command = mock.Mock(return_value=command)
                first_properties = {
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "SubState": "running",
                    "Result": "success",
                    "ExecMainPID": "424242",
                    "InvocationID": "0123456789abcdef0123456789abcdef",
                    "ExecMainStatus": "0",
                    "ControlGroup": "/system.slice/scratch-identity-barrier.service",
                }
                second_properties = dict(first_properties)
                if replacement == "pid":
                    second_properties["ExecMainPID"] = "424243"
                else:
                    second_properties["InvocationID"] = "1123456789abcdef0123456789abcdef"
                terminal = {
                    "properties": {
                        "LoadState": "not-found",
                        "ActiveState": "inactive",
                        "SubState": "dead",
                        "Result": "success",
                        "ExecMainPID": "0",
                        "ExecMainStatus": "0",
                        "ControlGroup": "",
                    }
                }
                snapshots = [
                    {"properties": first_properties},
                    {"properties": second_properties},
                    terminal,
                    terminal,
                ]
                real_popen = subprocess.Popen
                launched = []

                def launch(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    launched.append(process)
                    return process

                with (
                    mock.patch.object(backup_drill, "require_commands"),
                    mock.patch.object(backup_drill, "_scratch_host_guard"),
                    mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                    mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                    mock.patch.object(
                        backup_drill,
                        "_scratch_service_snapshot",
                        side_effect=lambda _name: snapshots.pop(0) if snapshots else terminal,
                    ),
                    mock.patch.object(
                        backup_drill,
                        "_scratch_expected_properties",
                        return_value="/system.slice/scratch-identity-barrier.service",
                    ) as expected_properties,
                    mock.patch.object(backup_drill, "_scratch_control_group_snapshot", return_value={}),
                    mock.patch.object(backup_drill, "_scratch_send_worker_token") as authorize,
                    mock.patch.object(backup_drill, "_scratch_stop_unit"),
                    mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
                    mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
                ):
                    with self.assertRaisesRegex(RuntimeError, "identity changed before authorization") as failure:
                        backup_drill._run_isolated_restore_drill(
                            archive_path,
                            timestamp_slug="20261012T010209Z",
                            private_scratch_diagnostics=True,
                        )

                authorize.assert_not_called()
                self.assertEqual(expected_properties.call_count, 1)
                captured_stdout, _captured_stderr = failure.exception.scratch_unit_private_output
                self.assertEqual(captured_stdout, b"")
                diagnostic = failure.exception.scratch_unit_diagnostic
                self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(diagnostic))
                self.assertEqual(diagnostic["phase"], "confinement_verification_failed")
                self.assertEqual(diagnostic["active_state"], "active")
                self.assertFalse(diagnostic["terminal_snapshot"]["start_token_sent"])
                self.assertFalse(diagnostic["terminal_snapshot"]["completion_token_sent"])
                self.assertNotIn("ExecMainPID", json.dumps(diagnostic))

    def _assert_scratch_after_confinement_failure_preserves_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            stdout_payload = b"restore stdout:" + b"o" * 3000
            stderr_payload = b"pg_restore detail:" + b"e" * 40000
            child_code = (
                "import sys,time; "
                f"start={backup_drill.SCRATCH_START_TOKEN!r}; "
                "assert sys.stdin.buffer.read(len(start)) == start; "
                f"sys.stdout.buffer.write({stdout_payload!r}); sys.stdout.flush(); "
                f"sys.stderr.buffer.write({stderr_payload!r}); sys.stderr.flush(); "
                "time.sleep(0.35); sys.exit(17)"
            )
            command = [sys.executable, "-c", child_code]
            unit_command = mock.Mock(return_value=command)
            active = {
                "properties": {
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "SubState": "running",
                    "Result": "success",
                    "ExecMainPID": "424242",
                    "InvocationID": "0123456789abcdef0123456789abcdef",
                    "ExecMainStatus": "0",
                    "ControlGroup": "/system.slice/scratch-after-confinement.service",
                }
            }
            final = {
                "properties": {
                    "LoadState": "not-found",
                    "ActiveState": "failed",
                    "SubState": "dead",
                    "Result": "exit-code",
                    "ExecMainPID": "0",
                    "ExecMainStatus": "17",
                    "ControlGroup": "",
                }
            }
            snapshots = [active, active]
            launched: list[subprocess.Popen[bytes]] = []

            def service_snapshot(_unit_name: str) -> dict[str, object]:
                if snapshots:
                    return snapshots.pop(0)
                if launched and launched[0].poll() is None:
                    return active
                return final

            real_popen = subprocess.Popen

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                launched.append(process)
                return process

            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=service_snapshot),
                mock.patch.object(
                    backup_drill,
                    "_scratch_expected_properties",
                    return_value="/system.slice/scratch-after-confinement.service",
                ),
                mock.patch.object(backup_drill, "_scratch_control_group_snapshot", return_value={}),
                mock.patch.object(backup_drill, "_scratch_stop_unit"),
                mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
                mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "completion was not fully authorized and verified"
                ) as failure:
                    backup_drill._run_isolated_restore_drill(
                        archive_path,
                        timestamp_slug="20261012T010207Z",
                        private_scratch_diagnostics=True,
                    )

            diagnostic = failure.exception.scratch_unit_diagnostic
            self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(diagnostic))
            self.assertEqual(diagnostic["phase"], "authorization_failed")
            self.assertEqual(diagnostic["active_state"], "active")
            self.assertEqual(diagnostic["terminal_snapshot"]["load_state"], "not-found")
            self.assertTrue(diagnostic["terminal_snapshot"]["start_token_sent"])
            self.assertFalse(diagnostic["terminal_snapshot"]["completion_token_sent"])
            self.assertEqual(diagnostic["launcher_exit_status"], 17)
            self.assertEqual(diagnostic["verification_error"], "other")
            self.assertIsNone(diagnostic["verification_errno"])
            self.assertEqual(
                failure.exception.scratch_unit_private_output,
                (
                    stdout_payload[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES],
                    stderr_payload[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES],
                ),
            )
            self.assertTrue(diagnostic["stdout_truncated"])
            self.assertTrue(diagnostic["stderr_truncated"])
            self.assertTrue(unit_command.call_args.kwargs["private_scratch_diagnostics"])
            self.assertFalse(
                pathlib.Path(unit_command.call_args.kwargs["mountpoint"]).exists(),
                str(unit_command.call_args.kwargs["mountpoint"]),
            )

    def _assert_collected_unit_without_properties_preserves_output(self) -> None:
        class LauncherProcess:
            def __init__(self, stdin, stdout, stderr) -> None:
                self.stdin = stdin
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = None
                self.wait_calls = 0

            def poll(self):
                return self.returncode

            def wait(self, *, timeout=None):
                self.wait_calls += 1
                return self.returncode

        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            stdout_payload = b"restore completed with failure"
            stderr_payload = b"nested pg_restore failure detail"
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            for write_fd, payload in (
                (stdout_write, stdout_payload),
                (stderr_write, stderr_payload),
            ):
                os.write(write_fd, payload)
                os.close(write_fd)
            stdin_read, stdin_write = os.pipe()
            stdin_reader = os.fdopen(stdin_read, "rb", buffering=0)
            stdin_writer = os.fdopen(stdin_write, "wb", buffering=0)
            stdout_reader = os.fdopen(stdout_read, "rb", buffering=0)
            stderr_reader = os.fdopen(stderr_read, "rb", buffering=0)
            launcher = LauncherProcess(stdin_writer, stdout_reader, stderr_reader)
            command = ["systemd-run"]
            unit_command = mock.Mock(return_value=command)
            active = {
                "properties": {
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "SubState": "running",
                    "Result": "success",
                    "ExecMainPID": "424242",
                    "InvocationID": "0123456789abcdef0123456789abcdef",
                    "ExecMainStatus": "0",
                    "ControlGroup": "/system.slice/scratch-collected.service",
                }
            }
            collected = {"properties": {"LoadState": "not-found"}}
            snapshots = [active, active]
            snapshot_count = 0

            def snapshot(_unit_name: str) -> dict[str, object]:
                nonlocal snapshot_count
                snapshot_count += 1
                if snapshots:
                    return snapshots.pop(0)
                if snapshot_count == 3:
                    # START has been sent after two matching identity checks;
                    # now model the launcher exiting before unit properties
                    # can be collected by the completion monitor.
                    launcher.returncode = 17
                    return active
                return collected

            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=snapshot),
                mock.patch.object(
                    backup_drill,
                    "_scratch_expected_properties",
                    return_value="/system.slice/scratch-collected.service",
                ),
                mock.patch.object(backup_drill, "_scratch_control_group_snapshot", return_value={}),
                mock.patch.object(backup_drill, "_scratch_stop_unit") as stop_unit,
                mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
                mock.patch.object(backup_drill.subprocess, "Popen", return_value=launcher),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "completion was not fully authorized and verified"
                ) as failure:
                    backup_drill._run_isolated_restore_drill(
                        archive_path,
                        timestamp_slug="20261012T010208Z",
                        private_scratch_diagnostics=True,
                    )
                diagnostic = failure.exception.scratch_unit_diagnostic
                self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(diagnostic))
                self.assertEqual(diagnostic["phase"], "authorization_failed")
                self.assertEqual(diagnostic["terminal_snapshot"]["load_state"], "not-found")
                self.assertTrue(diagnostic["terminal_snapshot"]["start_token_sent"])
                self.assertFalse(diagnostic["terminal_snapshot"]["completion_token_sent"])
                self.assertEqual(
                    failure.exception.scratch_unit_private_output,
                    (stdout_payload, stderr_payload),
                )
                self.assertEqual(stdin_reader.read(len(backup_drill.SCRATCH_START_TOKEN)), backup_drill.SCRATCH_START_TOKEN)
                self.assertGreaterEqual(stop_unit.call_count, 1)
                self.assertTrue(
                    all(len(call.args) == 1 and call.args[0] for call in stop_unit.call_args_list)
                )
                self.assertGreater(launcher.wait_calls, 0)
                self.assertEqual(launcher.returncode, 17)
                self.assertFalse(pathlib.Path(unit_command.call_args.kwargs["mountpoint"]).exists())
            stdin_reader.close()
            stdout_reader.close()
            stderr_reader.close()

    def _assert_worker_authorization_barrier(self) -> None:
        scratch_root = pathlib.Path("/run") / f"oldsparky-backup-restore-{uuid.uuid4().hex[:24]}"
        scratch_root.mkdir(mode=0o700)
        args = argparse.Namespace(_scratch_root=str(scratch_root))
        try:
            for payload in (
                b"",
                backup_drill.SCRATCH_START_TOKEN[:-1],
                backup_drill.SCRATCH_START_TOKEN,
                backup_drill.SCRATCH_START_TOKEN + b"x",
            ):
                read_fd, write_fd = os.pipe()
                try:
                    if payload:
                        os.write(write_fd, payload)
                    os.close(write_fd)
                    stdin_stream = types.SimpleNamespace(
                        fileno=lambda: read_fd,
                        buffer=io.BytesIO(payload),
                    )
                    with (
                        mock.patch.dict(backup_drill.os.environ, {"PG_START_BARRIER_SENTINEL": "preserve"}),
                        mock.patch.object(backup_drill.sys, "stdin", stdin_stream),
                        mock.patch.object(backup_drill, "_scratch_mount_snapshot") as mount_snapshot,
                        mock.patch.object(backup_drill, "_scratch_cgroup_snapshot") as cgroup_snapshot,
                        mock.patch.object(backup_drill, "_scratch_mem_available_bytes") as memory_snapshot,
                        mock.patch.object(backup_drill, "_open_verified_scratch_archive") as open_archive,
                        mock.patch.object(backup_drill, "_scratch_create_application_role") as create_role,
                        mock.patch.object(backup_drill, "run_command") as run_command,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "authorization was not received"):
                            backup_drill._scratch_worker_main(args)
                        self.assertEqual(os.environ.get("PG_START_BARRIER_SENTINEL"), "preserve")
                    mount_snapshot.assert_not_called()
                    cgroup_snapshot.assert_not_called()
                    memory_snapshot.assert_not_called()
                    open_archive.assert_not_called()
                    create_role.assert_not_called()
                    run_command.assert_not_called()
                finally:
                    os.close(read_fd)
        finally:
            scratch_root.rmdir()

    def _assert_worker_token_raw_pipe_framing(self) -> None:
        cases = (
            (backup_drill.SCRATCH_START_TOKEN, False, True),
            (b"wrong", False, False),
            (b"X" * len(backup_drill.SCRATCH_START_TOKEN), False, False),
            (backup_drill.SCRATCH_START_TOKEN[:-1], False, False),
            (backup_drill.SCRATCH_START_TOKEN + b"x", False, False),
            (backup_drill.SCRATCH_DONE_TOKEN, True, True),
            (b"", True, False),
            (b"wrong", True, False),
            (b"X" * len(backup_drill.SCRATCH_DONE_TOKEN), True, False),
            (backup_drill.SCRATCH_DONE_TOKEN + b"x", True, False),
            (backup_drill.SCRATCH_DONE_TOKEN[:-1], True, False),
        )
        for payload, require_eof, succeeds in cases:
            with self.subTest(require_eof=require_eof, payload_suffix=payload[-3:]):
                read_fd, write_fd = os.pipe()
                reader = os.fdopen(read_fd, "rb", buffering=0)
                try:
                    os.write(write_fd, payload)
                    if require_eof or not succeeds:
                        os.close(write_fd)
                        write_fd = -1
                    expected = (
                        backup_drill.SCRATCH_DONE_TOKEN
                        if require_eof
                        else backup_drill.SCRATCH_START_TOKEN
                    )
                    if succeeds:
                        self.assertEqual(
                            backup_drill._scratch_read_worker_token(
                                reader,
                                expected,
                                require_eof=require_eof,
                                deadline=time.monotonic() + (
                                    backup_drill.SCRATCH_RUNTIME_SECONDS
                                    if require_eof
                                    else backup_drill.SCRATCH_STARTUP_SECONDS
                                ),
                            ),
                            expected,
                        )
                    else:
                        with self.assertRaisesRegex(RuntimeError, "authorization|completion"):
                            backup_drill._scratch_read_worker_token(
                                reader,
                                expected,
                                require_eof=require_eof,
                                deadline=time.monotonic() + (
                                    backup_drill.SCRATCH_RUNTIME_SECONDS
                                    if require_eof
                                    else backup_drill.SCRATCH_STARTUP_SECONDS
                                ),
                            )
                    self.assertTrue(os.get_blocking(reader.fileno()))
                finally:
                    reader.close()
                    if write_fd >= 0:
                        os.close(write_fd)

        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        try:
            os.write(write_fd, backup_drill.SCRATCH_START_TOKEN)
            os.close(write_fd)
            with self.assertRaisesRegex(RuntimeError, "authorization was not received"):
                backup_drill._scratch_read_worker_token(
                    reader,
                    backup_drill.SCRATCH_START_TOKEN,
                    require_eof=False,
                    deadline=time.monotonic() + backup_drill.SCRATCH_STARTUP_SECONDS,
                )
        finally:
            reader.close()

        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        try:
            with (
                mock.patch.object(backup_drill, "SCRATCH_STARTUP_SECONDS", 0.02),
                self.assertRaisesRegex(RuntimeError, "authorization was not received"),
            ):
                backup_drill._scratch_read_worker_token(
                    reader,
                    backup_drill.SCRATCH_START_TOKEN,
                    require_eof=False,
                    deadline=time.monotonic() + backup_drill.SCRATCH_STARTUP_SECONDS,
                )
        finally:
            reader.close()
            os.close(write_fd)

        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        try:
            with (
                mock.patch.object(backup_drill, "SCRATCH_RUNTIME_SECONDS", 0.02),
                self.assertRaisesRegex(RuntimeError, "completion was not acknowledged"),
            ):
                backup_drill._scratch_read_worker_token(
                    reader,
                    backup_drill.SCRATCH_DONE_TOKEN,
                    require_eof=True,
                    deadline=time.monotonic() + backup_drill.SCRATCH_RUNTIME_SECONDS,
                )
        finally:
            reader.close()
            os.close(write_fd)

    def _assert_scratch_worker_forwards_output_only_when_private(self) -> None:
        class BinaryTextCapture:
            def __init__(self) -> None:
                self.buffer = io.BytesIO()

            def write(self, value: str) -> int:
                return self.buffer.write(value.encode("utf-8"))

            def flush(self) -> None:
                return None

        for private in (True, False):
            scratch_root = pathlib.Path("/run") / f"oldsparky-backup-restore-{uuid.uuid4().hex[:24]}"
            scratch_root.mkdir(mode=0o700)
            try:
                archive_path = scratch_root / "archive.dump"
                archive_bytes = b"PGDMPsynthetic worker archive"
                archive_path.write_bytes(archive_bytes)
                archive_path.chmod(0o600)
                archive_stat = archive_path.stat()
                args = argparse.Namespace(
                    _scratch_root=str(scratch_root),
                    _scratch_archive=str(archive_path),
                    _scratch_archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
                    _scratch_archive_dev=archive_stat.st_dev,
                    _scratch_archive_ino=archive_stat.st_ino,
                    _scratch_archive_size=archive_stat.st_size,
                    _scratch_timestamp_slug="20261012T010208Z",
                    private_scratch_diagnostics=private,
                )
                worker_failure = backup_drill.RestoreGuardStop("command_failed")
                worker_failure.scratch_worker_private_output = (
                    b"known pg_restore stdout",
                    b"known pg_restore stderr",
                    False,
                    False,
                )
                command_results = [
                    subprocess.CompletedProcess([], 0, "/usr/lib/postgresql/16/bin\n", ""),
                    subprocess.CompletedProcess([], 0, "postgres (PostgreSQL) 16.10\n", ""),
                ]

                def fake_run_command(command, **_kwargs):
                    if command[:2] == ["pg_config", "--bindir"]:
                        return command_results[0]
                    if command[-1:] == ["--version"]:
                        return command_results[1]
                    return subprocess.CompletedProcess(command, 0, "", "")

                stdout_capture = BinaryTextCapture()
                stderr_capture = BinaryTextCapture()
                with (
                    mock.patch.object(backup_drill, "_scratch_mount_snapshot"),
                    mock.patch.object(backup_drill, "_scratch_cgroup_snapshot"),
                    mock.patch.object(backup_drill, "_scratch_mem_available_bytes", return_value=2**30),
                    mock.patch.object(backup_drill, "_scratch_resource_guard"),
                    mock.patch.object(backup_drill, "require_commands"),
                    mock.patch.object(
                        backup_drill.pwd,
                        "getpwnam",
                        return_value=types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()),
                    ),
                    mock.patch.object(backup_drill.os, "chown"),
                    mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                    mock.patch.object(backup_drill, "_scratch_create_application_role"),
                    mock.patch.object(
                        backup_drill, "perform_restore_drill", side_effect=worker_failure
                    ),
                    mock.patch.object(
                        backup_drill.sys,
                        "stdin",
                        types.SimpleNamespace(buffer=StagedWorkerInput()),
                    ),
                    mock.patch.object(backup_drill.sys, "stdout", stdout_capture),
                    mock.patch.object(backup_drill.sys, "stderr", stderr_capture),
                ):
                    with self.assertRaises(backup_drill.RestoreGuardStop) as raised:
                        backup_drill._scratch_worker_main(args)

                self.assertIs(raised.exception, worker_failure)
                if private:
                    self.assertEqual(stdout_capture.buffer.getvalue(), b"known pg_restore stdout")
                    self.assertEqual(stderr_capture.buffer.getvalue(), b"known pg_restore stderr")
                else:
                    self.assertEqual(stdout_capture.buffer.getvalue(), b"")
                    self.assertEqual(stderr_capture.buffer.getvalue(), b"")
            finally:
                shutil.rmtree(scratch_root)

    def test_run_disk_guarded_command_captures_only_opted_in_failure_output(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", "synthetic-secret", "platformdb"
        )
        stdout_payload = b"pg_restore stdout:" + b"o" * 9000
        stderr_payload = b"restore failure marker:" + b"e" * 40000
        child_code = (
            "import sys; "
            f"sys.stdout.buffer.write({stdout_payload!r}); sys.stdout.flush(); "
            f"sys.stderr.buffer.write({stderr_payload!r}); sys.stderr.flush(); sys.exit(23)"
        )
        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(backup_drill, "command_env", return_value={"PATH": os.environ["PATH"]}),
        ):
            with self.assertRaises(backup_drill.RestoreGuardStop) as failure:
                backup_drill.run_disk_guarded_command(
                    [sys.executable, "-c", child_code],
                    target=target,
                    stage="pg_restore",
                    capture_private_output=True,
                )
        self.assertEqual(failure.exception.reason, "command_failed")
        private_output = failure.exception.scratch_worker_private_output
        self.assertEqual(private_output[0], stdout_payload[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES])
        self.assertEqual(private_output[1], stderr_payload[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES])
        self.assertTrue(private_output[2])
        self.assertTrue(private_output[3])

        benign_code = (
            "import sys; sys.stdout.write('result'); sys.stderr.write('benign notice')"
        )
        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(backup_drill, "command_env", return_value={"PATH": os.environ["PATH"]}),
        ):
            backup_drill.run_disk_guarded_command(
                [sys.executable, "-c", benign_code],
                target=target,
                stage="pg_restore",
                capture_private_output=True,
            )

    def test_run_disk_guarded_command_preserves_primary_when_reap_fails(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", "synthetic-secret", "platformdb"
        )
        stdout_payload = b"restore reached child\n"
        stderr_payload = b"bounded private failure detail\n"
        child_code = (
            "import sys,time; "
            f"sys.stdout.buffer.write({stdout_payload!r}); sys.stdout.flush(); "
            f"sys.stderr.buffer.write({stderr_payload!r}); sys.stderr.flush(); "
            "time.sleep(30)"
        )
        resource_guard_calls = 0

        def fail_after_spawn() -> None:
            nonlocal resource_guard_calls
            resource_guard_calls += 1
            if resource_guard_calls == 2:
                time.sleep(0.1)
                raise RuntimeError("primary restore resource failure")

        cleanup_calls: list[int] = []

        def stop_then_report_failure(process, *, phase=None) -> None:
            cleanup_calls.append(process.pid)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise backup_drill.RestoreGuardStop("child_unstopped", phase=phase)

        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(backup_drill, "command_env", return_value={"PATH": os.environ["PATH"]}),
            mock.patch.object(backup_drill, "_stop_owned_child", side_effect=stop_then_report_failure),
        ):
            with self.assertRaisesRegex(RuntimeError, "primary restore resource failure") as failure:
                backup_drill.run_disk_guarded_command(
                    [sys.executable, "-c", child_code],
                    target=target,
                    stage="pg_restore",
                    resource_guard=fail_after_spawn,
                    capture_private_output=True,
                )

        self.assertEqual(len(cleanup_calls), 1)
        self.assertEqual(failure.exception.scratch_worker_cleanup_error, "child_unstopped")
        self.assertEqual(
            failure.exception.scratch_worker_private_output,
            (stdout_payload, stderr_payload, False, False),
        )

    def _assert_scratch_startup_inactive_race_is_handled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            command = [
                sys.executable,
                "-c",
                "import json,sys; "
                f"start={backup_drill.SCRATCH_START_TOKEN!r}; "
                f"done={backup_drill.SCRATCH_DONE_TOKEN!r}; "
                "ack=sys.stdin.buffer.read(len(start)); assert ack == start; "
                "sys.stdout.write(json.dumps({'ok':True,'restored_table_count':1})+'\\n'); sys.stdout.flush(); "
                "assert sys.stdin.buffer.read(len(done)) == done",
            ]
            unit_command = mock.Mock(return_value=command)
            real_popen = subprocess.Popen
            launched = []

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                launched.append(process)
                return process

            snapshots_seen = []

            def service_snapshot(_unit_name):
                if not snapshots_seen:
                    state = {
                        "properties": {
                            "LoadState": "not-found",
                            "ActiveState": "inactive",
                            "SubState": "dead",
                            "Result": "success",
                            "ExecMainPID": "0",
                            "ControlGroup": "",
                        }
                    }
                elif len(snapshots_seen) == 1:
                    state = {
                        "properties": {
                            "LoadState": "loaded",
                            "ActiveState": "inactive",
                            "SubState": "dead",
                            "Result": "success",
                            "ExecMainPID": "0",
                            "ControlGroup": "",
                        }
                    }
                elif launched and launched[0].poll() is None:
                    state = {
                        "properties": {
                            "LoadState": "loaded",
                            "ActiveState": "active",
                            "ExecMainPID": "123",
                            "InvocationID": "0123456789abcdef0123456789abcdef",
                            "ControlGroup": "/system.slice/scratch-startup-race.service",
                        }
                    }
                else:
                    state = {
                        "properties": {
                            "LoadState": "not-found",
                            "ActiveState": "inactive",
                            "ExecMainPID": "0",
                            "ControlGroup": "",
                        }
                    }
                snapshots_seen.append(state["properties"]["ActiveState"])
                return state

            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=service_snapshot),
                mock.patch.object(backup_drill, "_scratch_expected_properties", return_value="/system.slice/scratch-startup-race.service"),
                mock.patch.object(backup_drill, "_scratch_control_group_snapshot", return_value={}),
                mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
                mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
            ):
                result = backup_drill._run_isolated_restore_drill(
                    archive_path,
                    timestamp_slug="20261012T010203Z",
                )

            self.assertEqual(result, 1)
            self.assertEqual(snapshots_seen[0], "inactive")
            self.assertEqual(snapshots_seen[1], "inactive")
            self.assertIn("active", snapshots_seen)
            self.assertFalse(launched[0].poll() is None)
            mountpoint = pathlib.Path(unit_command.call_args.kwargs["mountpoint"])
            self.assertFalse(mountpoint.exists())

    def _assert_scratch_final_identity_loss_blocks_done(self) -> None:
        for replacement in (
            "pid", "invocation", "starttime", "cgroup", "missing", "late_start", "late_done"
        ):
            with self.subTest(final_identity=replacement), tempfile.TemporaryDirectory() as temporary_dir:
                archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
                archive_path.write_bytes(b"PGDMPsynthetic archive")
                archive_path.chmod(0o600)
                command = [
                    sys.executable,
                    "-c",
                    "import sys; "
                    f"start={backup_drill.SCRATCH_START_TOKEN!r}; "
                    f"done={backup_drill.SCRATCH_DONE_TOKEN!r}; "
                    "assert sys.stdin.buffer.read(len(start)) == start; "
                    "sys.stdout.write('{\\\"ok\\\":true,\\\"restored_table_count\\\":1}\\n'); sys.stdout.flush(); "
                    "assert sys.stdin.buffer.read(len(done)) == done; "
                    "sys.stderr.write('DONE-RECEIVED'); sys.stderr.flush()",
                ]
                launched: list[subprocess.Popen[bytes]] = []
                real_popen = subprocess.Popen

                def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
                    process = real_popen(*args, **kwargs)
                    launched.append(process)
                    return process

                snapshots_seen = 0
                base_properties = {
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "SubState": "running",
                    "Result": "success",
                    "ExecMainPID": "424242",
                    "InvocationID": "0123456789abcdef0123456789abcdef",
                    "ExecMainStatus": "0",
                    "ControlGroup": "/system.slice/scratch-final-identity.service",
                }

                def service_snapshot(_unit_name: str) -> dict[str, Any]:
                    nonlocal snapshots_seen
                    snapshots_seen += 1
                    if snapshots_seen == 1:
                        return {"properties": {**base_properties, "LoadState": "not-found", "ActiveState": "inactive", "ExecMainPID": "0", "ControlGroup": ""}}
                    if snapshots_seen == 2:
                        return {"properties": {**base_properties, "ActiveState": "inactive", "ExecMainPID": "0", "ControlGroup": ""}}
                    if snapshots_seen == 5 and replacement == "missing":
                        raise FileNotFoundError("synthetic final unit snapshot loss")
                    properties = dict(base_properties)
                    if snapshots_seen == 5:
                        if replacement == "pid":
                            properties["ExecMainPID"] = "424243"
                        elif replacement == "invocation":
                            properties["InvocationID"] = "1123456789abcdef0123456789abcdef"
                    if snapshots_seen >= 6:
                        properties.update({"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead", "ExecMainPID": "0", "ControlGroup": ""})
                    return {"properties": properties}

                expected_properties_calls = 0

                def expected_properties(snapshot: dict[str, Any], **_kwargs: Any) -> str:
                    nonlocal expected_properties_calls
                    expected_properties_calls += 1
                    if replacement == "cgroup" and expected_properties_calls == 3:
                        return "/system.slice/replaced-cgroup.service"
                    return snapshot["properties"]["ControlGroup"]

                start_tokens: list[bytes] = []
                send_token = backup_drill._scratch_send_worker_token

                def capture_token(process: subprocess.Popen[bytes], *, token: bytes, deadline: float, close_after: bool) -> None:
                    start_tokens.append(token)
                    send_token(process, token=token, deadline=deadline, close_after=close_after)
                    if replacement == "late_start" and token == backup_drill.SCRATCH_START_TOKEN:
                        raise backup_drill._ScratchTokenDeliveryError(
                            "synthetic deadline after complete START write", complete=True
                        )
                    if replacement == "late_done" and token == backup_drill.SCRATCH_DONE_TOKEN:
                        raise backup_drill._ScratchTokenDeliveryError(
                            "synthetic deadline after complete DONE write", complete=True
                        )

                def stop_unit(_unit_name: str) -> None:
                    if launched and launched[0].poll() is None:
                        launched[0].kill()
                        launched[0].wait(timeout=2)

                unit_command = mock.Mock(return_value=command)
                with (
                    mock.patch.object(backup_drill, "require_commands"),
                    mock.patch.object(backup_drill, "_scratch_host_guard"),
                    mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                    mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                    mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=service_snapshot),
                    mock.patch.object(backup_drill, "_scratch_expected_properties", side_effect=expected_properties),
                    mock.patch.object(backup_drill, "_scratch_control_group_snapshot", return_value={}),
                    mock.patch.object(backup_drill, "_scratch_process_start_time", side_effect=("123456", "123456", "999999" if replacement == "starttime" else "123456")),
                    mock.patch.object(backup_drill, "_scratch_send_worker_token", side_effect=capture_token),
                    mock.patch.object(backup_drill, "_scratch_stop_unit", side_effect=stop_unit),
                    mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
                    mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
                ):
                    with self.assertRaises(Exception) as failure:
                        backup_drill._run_isolated_restore_drill(
                            archive_path,
                            timestamp_slug="20261012T010206Z",
                            private_scratch_diagnostics=True,
                        )
                self.assertEqual(
                    start_tokens,
                    [backup_drill.SCRATCH_START_TOKEN]
                    + ([backup_drill.SCRATCH_DONE_TOKEN] if replacement == "late_done" else []),
                )
                self.assertTrue(launched)
                self.assertIsNotNone(launched[0].returncode)
                self.assertEqual(
                    getattr(failure.exception, "scratch_unit_diagnostic", {}).get("phase"),
                    "authorization_failed",
                )
                self.assertEqual(
                    getattr(failure.exception, "scratch_unit_diagnostic", {}).get("terminal_snapshot", {}).get(
                        "completion_token_sent", False
                    ),
                    replacement == "late_done",
                )
                self.assertFalse(pathlib.Path(unit_command.call_args.kwargs["mountpoint"]).exists())

    def _assert_worker_token_deadline_delivery_is_truthful(self) -> None:
        class WritableSelector:
            def __enter__(self):
                return self

            def __exit__(self, *_args: Any) -> None:
                return None

            def register(self, *_args: Any, **_kwargs: Any) -> None:
                return None

            def select(self, timeout: float | None = None) -> list[object]:
                return [object()]

        for token, close_after in (
            (backup_drill.SCRATCH_START_TOKEN, False),
            (backup_drill.SCRATCH_DONE_TOKEN, True),
        ):
            read_fd, write_fd = os.pipe()
            stream = os.fdopen(write_fd, "wb", buffering=0)
            process = types.SimpleNamespace(stdin=stream, poll=lambda: None)
            clock = [9.0]
            original_write = os.write

            def write_then_cross_deadline(fd: int, data: bytes | memoryview) -> int:
                written = original_write(fd, data)
                clock[0] = 11.0
                return written

            try:
                with (
                    mock.patch.object(backup_drill.selectors, "DefaultSelector", return_value=WritableSelector()),
                    mock.patch.object(backup_drill.time, "monotonic", side_effect=lambda: clock[0]),
                    mock.patch.object(backup_drill.os, "write", side_effect=write_then_cross_deadline),
                ):
                    with self.assertRaises(backup_drill._ScratchTokenDeliveryError) as failure:
                        backup_drill._scratch_send_worker_token(
                            process,
                            token=token,
                            deadline=10.0,
                            close_after=close_after,
                        )
                self.assertTrue(failure.exception.complete)
                self.assertEqual(os.read(read_fd, len(token)), token)
            finally:
                if not stream.closed:
                    stream.close()
                os.close(read_fd)

        read_fd, write_fd = os.pipe()
        stream = os.fdopen(write_fd, "wb", buffering=0)
        process = types.SimpleNamespace(stdin=stream, poll=lambda: None)
        original_write = os.write
        write_count = 0

        def partial_then_fail(fd: int, data: bytes | memoryview) -> int:
            nonlocal write_count
            write_count += 1
            if write_count == 1:
                return original_write(fd, data[:1])
            raise BrokenPipeError("synthetic partial token channel failure")

        try:
            with (
                mock.patch.object(backup_drill.selectors, "DefaultSelector", return_value=WritableSelector()),
                mock.patch.object(backup_drill.time, "monotonic", return_value=9.0),
                mock.patch.object(backup_drill.os, "write", side_effect=partial_then_fail),
            ):
                with self.assertRaises(backup_drill._ScratchTokenDeliveryError) as failure:
                    backup_drill._scratch_send_worker_token(
                        process,
                        token=backup_drill.SCRATCH_START_TOKEN,
                        deadline=10.0,
                        close_after=False,
                    )
            self.assertFalse(failure.exception.complete)
            self.assertEqual(os.read(read_fd, 1), backup_drill.SCRATCH_START_TOKEN[:1])
        finally:
            if not stream.closed:
                stream.close()
            os.close(read_fd)

    def _assert_scratch_fast_launcher_cannot_skip_active_confinement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            command = [sys.executable, "-c", "print('{\"ok\":true,\"restored_table_count\":1}')"]
            real_popen = subprocess.Popen
            launched = []

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                process.wait(timeout=2)
                launched.append(process)
                return process

            snapshots = []

            def service_snapshot(_unit_name):
                snapshots.append(True)
                return {
                    "properties": {
                        "LoadState": "not-found",
                        "ActiveState": "inactive",
                        "ExecMainPID": "0",
                        "ControlGroup": "",
                    }
                }

            unit_command = mock.Mock(return_value=command)
            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=service_snapshot),
                mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
                mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
            ):
                with self.assertRaisesRegex(RuntimeError, "exited before its confinement was verified"):
                    backup_drill._run_isolated_restore_drill(
                        archive_path,
                        timestamp_slug="20261012T010204Z",
                    )

            self.assertTrue(launched)
            self.assertTrue(snapshots)
            self.assertIsNotNone(launched[0].returncode)
            mountpoint = pathlib.Path(unit_command.call_args.kwargs["mountpoint"])
            self.assertFalse(mountpoint.exists())

    def _assert_scratch_absent_unit_has_bounded_startup_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            command = [sys.executable, "-c", "import time; time.sleep(10)"]
            real_popen = subprocess.Popen
            launched = []
            snapshots = []

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                launched.append(process)
                return process

            def service_snapshot(_unit_name):
                snapshots.append(True)
                return {
                    "properties": {
                        "LoadState": "not-found",
                        "ActiveState": "inactive",
                        "SubState": "dead",
                        "Result": "success",
                        "ExecMainPID": "0",
                        "ControlGroup": "",
                    }
                }

            def stop_unit(_unit_name):
                if launched and launched[0].poll() is None:
                    launched[0].kill()
                    launched[0].wait(timeout=2)

            unit_command = mock.Mock(return_value=command)
            started = time.monotonic()
            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=service_snapshot),
                mock.patch.object(backup_drill, "_scratch_stop_unit", side_effect=stop_unit),
                mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
                mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
                mock.patch.object(backup_drill, "SCRATCH_STARTUP_SECONDS", 0.05),
            ):
                with self.assertRaisesRegex(RuntimeError, "startup deadline"):
                    backup_drill._run_isolated_restore_drill(
                        archive_path,
                        timestamp_slug="20261012T010205Z",
                    )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 2)
            self.assertGreaterEqual(len(snapshots), 2)
            self.assertEqual(launched[0].returncode, -signal.SIGKILL)
            self.assertFalse(pathlib.Path(unit_command.call_args.kwargs["mountpoint"]).exists())
    def test_local_admin_commands_use_postgres_os_user(self) -> None:
        target = backup_drill.DatabaseTarget("127.0.0.1", 5432, "platform_user", None, "platformdb")

        self.assertEqual(
            backup_drill.local_postgres_admin_command("create", target, "platform_restore_drill_test"),
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "createdb",
                "--owner",
                "platform_user",
                "platform_restore_drill_test",
            ],
        )

    def test_restore_drill_captures_extension_output_for_json_callers(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", "secret", "platformdb"
        )
        admin_target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "postgres", None, "postgres"
        )
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "CREATE EXTENSION\n", ""),
            subprocess.CompletedProcess([], 0, "CREATE SCHEMA\n", ""),
            subprocess.CompletedProcess([], 0, "22\n", ""),
            subprocess.CompletedProcess([], 0, "1\n", ""),
            subprocess.CompletedProcess([], 0, "20260801_0036\n", ""),
            subprocess.CompletedProcess([], 0, "1\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "0\n", ""),
        ]

        archive = io.BytesIO(b"PGDMPsynthetic archive")
        resource_guard = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary_dir, mock.patch.object(
            backup_drill, "run_command", side_effect=responses
        ) as run_command, mock.patch.object(
            backup_drill, "run_restore_command", return_value=None
        ) as run_restore_command, mock.patch.object(
            backup_drill, "require_restore_headroom"
        ):
            table_count = backup_drill.perform_restore_drill(
                pathlib.Path(temporary_dir) / "backup.dump",
                app_target=target,
                admin_target=admin_target,
                timestamp_slug="20260720T120000Z",
                archive_stdin=archive,
                restore_as_postgres=True,
                restore_no_tablespaces=True,
                resource_guard=resource_guard,
            )

        self.assertEqual(table_count, 22)
        self.assertEqual(run_command.call_args_list[0].kwargs["target"], admin_target)
        self.assertIn("--owner", run_command.call_args_list[0].args[0])
        self.assertIn("platform_user", run_command.call_args_list[0].args[0])
        for index in (1, 2, 3, 4, 5, 6):
            self.assertEqual(run_command.call_args_list[index].kwargs["target"].username, "platform_user")
        self.assertEqual(run_command.call_args_list[7].kwargs["target"], admin_target)
        self.assertIn("postgres", run_command.call_args_list[7].args[0])
        self.assertTrue(run_command.call_args_list[1].kwargs["capture_output"])
        self.assertTrue(run_command.call_args_list[2].kwargs["capture_output"])
        self.assertIn("CREATE SCHEMA platform", run_command.call_args_list[2].args[0][-1])
        self.assertEqual(run_restore_command.call_count, 2)
        for index, schema in enumerate(("--schema=platform", "--schema=public")):
            command = run_restore_command.call_args_list[index].args[0]
            self.assertEqual(command[:4], ["runuser", "-u", "postgres", "--"])
            self.assertEqual(run_restore_command.call_args_list[index].kwargs["target"].username, "platform_user")
            self.assertIn(["--username", "platform_user"], [command[i : i + 2] for i in range(len(command) - 1)])
            self.assertIn(schema, command)
            self.assertIn("--no-tablespaces", command)
            self.assertNotIn(str(pathlib.Path(temporary_dir) / "backup.dump"), command)
            self.assertIs(run_restore_command.call_args_list[index].kwargs["stdin_handle"], archive)
            self.assertIs(run_restore_command.call_args_list[index].kwargs["resource_guard"], resource_guard)
        self.assertEqual(archive.tell(), 0)

        with mock.patch.object(backup_drill, "run_command") as role_command:
            backup_drill._scratch_create_application_role(admin_target)
        self.assertEqual(role_command.call_args.kwargs["target"], admin_target)
        self.assertEqual(
            role_command.call_args.args[0][-1],
            "CREATE ROLE platform_user LOGIN;",
        )

        # The maintenance entrypoint must reject an unprivileged caller before
        # inspecting an archive or starting any host-level scratch operation.
        with (
            mock.patch.object(backup_drill.os, "geteuid", return_value=1000),
            mock.patch.object(backup_drill, "require_commands") as require_commands,
            mock.patch.object(backup_drill, "_scratch_host_guard") as host_guard,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "requires the root maintenance owner"
            ):
                backup_drill._run_isolated_restore_drill(
                    pathlib.Path("/does-not-need-to-exist.dump"),
                    timestamp_slug="20261012T000000Z",
                )
        require_commands.assert_not_called()
        host_guard.assert_not_called()

    def test_isolated_restore_signal_cleanup_under_root(self) -> None:
        self._assert_low_space_abort_kills_owned_restore_and_drops_only_drill_db()
        self._assert_scratch_sigterm_stops_and_reaps_launched_unit()
        self._assert_scratch_worker_uses_pinned_archive_fd()
        self._assert_scratch_unit_diagnostic_caps_and_private_envelope()
        self._assert_scratch_early_exit_output_is_drained()
        self._assert_scratch_live_identity_failure_preserves_output()
        self._assert_scratch_identity_replacement_cannot_receive_authorization()
        self._assert_scratch_startup_inactive_race_is_handled()
        self._assert_scratch_final_identity_loss_blocks_done()
        self._assert_worker_token_deadline_delivery_is_truthful()
        self._assert_scratch_fast_launcher_cannot_skip_active_confinement()
        self._assert_scratch_absent_unit_has_bounded_startup_deadline()
        self._assert_scratch_after_confinement_failure_preserves_output()
        self._assert_collected_unit_without_properties_preserves_output()
        self._assert_scratch_worker_forwards_output_only_when_private()
        self._assert_worker_authorization_barrier()
        self._assert_worker_token_raw_pipe_framing()

        with tempfile.TemporaryDirectory() as temporary_dir:
            scratch_root = pathlib.Path(temporary_dir) / "scratch"
            scratch_root.mkdir(mode=0o700)
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            command = backup_drill._scratch_unit_command(
                mountpoint=scratch_root,
                archive_path=archive_path,
                archive_stat=archive_path.stat(),
                archive_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                timestamp_slug="20261012T000000Z",
                unit_name="oldsparky-backup-restore-0123456789abcdef01234567.service",
            )
        self.assertIn("--property=MemoryMax=671088640", command)
        self.assertIn("--property=MemorySwapMax=0", command)
        self.assertIn("--property=RestrictAddressFamilies=AF_UNIX", command)
        self.assertIn("--property=PrivateNetwork=yes", command)
        self.assertIn("--property=OOMPolicy=kill", command)
        self.assertIn("--property=TemporaryFileSystem=" + str(scratch_root) + ":rw,size=536870912,mode=0700", command)
        self.assertTrue(any(item.startswith("--property=BindReadOnlyPaths=") and str(archive_path) in item for item in command))
        separator = command.index("--")
        expected_python = pathlib.Path(
            getattr(backup_drill.sys, "_base_executable", backup_drill.sys.executable)
        ).resolve(strict=True)
        self.assertEqual(command[separator + 1], str(expected_python))
        self.assertTrue(any(
            item.startswith("--property=BindReadOnlyPaths=")
            and str(scratch_root / "platform_backup_restore_drill.py") in item
            for item in command
        ))
        self.assertFalse(any(
            item.startswith("--property=BindReadOnlyPaths=")
            and str(scratch_root / "python3") in item
            for item in command
        ))
        self.assertFalse(any("ReadWritePaths=" in item for item in command))
        self.assertFalse(any("PGPASSWORD" in item or "PLATFORM_DATABASE_URL" in item for item in command))

    def _assert_scratch_worker_uses_pinned_archive_fd(self) -> None:
        self.assertEqual(backup_drill.os.geteuid(), 0)
        scratch_root = pathlib.Path(
            "/run/oldsparky-backup-restore-" + uuid.uuid4().hex[:24]
        )
        scratch_root.mkdir(mode=0o700)
        archive_path = scratch_root / "archive.dump"
        archive_bytes = b"PGDMPsynthetic worker archive"
        archive_path.write_bytes(archive_bytes)
        archive_path.chmod(0o600)
        archive_stat = archive_path.stat()
        args = argparse.Namespace(
            _scratch_root=str(scratch_root),
            _scratch_archive=str(archive_path),
            _scratch_archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
            _scratch_archive_dev=archive_stat.st_dev,
            _scratch_archive_ino=archive_stat.st_ino,
            _scratch_archive_size=archive_stat.st_size,
            _scratch_timestamp_slug="20261012T010203Z",
        )
        command_results = [
            subprocess.CompletedProcess(["pg_config"], 0, "/usr/lib/postgresql/16/bin\n", ""),
            subprocess.CompletedProcess(["postgres"], 0, "postgres (PostgreSQL) 16.10\n", ""),
        ]
        command_calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_run_command(command, **kwargs):
            command_calls.append((command, kwargs))
            if command[:2] == ["pg_config", "--bindir"]:
                return command_results[0]
            if command[-1:] == ["--version"]:
                return command_results[1]
            return subprocess.CompletedProcess(command, 0, "", "")

        observed: dict[str, object] = {}
        stdout_buffer = io.BytesIO()
        stdout = types.SimpleNamespace(buffer=stdout_buffer)

        def fake_restore(dump_path, **kwargs):
            observed["dump_path"] = dump_path
            observed.update(kwargs)
            handle = kwargs["archive_stdin"]
            handle_stat = os.fstat(handle.fileno())
            observed["archive_identity"] = (handle_stat.st_dev, handle_stat.st_ino)
            observed["archive_offset"] = handle.tell()
            observed["archive_open_during_restore"] = not handle.closed
            observed["archive_mode"] = handle.mode
            return 7

        fake_postgres = types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        try:
            with (
                mock.patch.object(backup_drill, "_scratch_mount_snapshot"),
                mock.patch.object(backup_drill, "_scratch_cgroup_snapshot"),
                mock.patch.object(backup_drill, "_scratch_mem_available_bytes", return_value=2**30),
                mock.patch.object(backup_drill, "_scratch_resource_guard"),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill.pwd, "getpwnam", return_value=fake_postgres),
                mock.patch.object(backup_drill.os, "chown"),
                mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                mock.patch.object(backup_drill, "_scratch_create_application_role") as create_role,
                mock.patch.object(backup_drill, "perform_restore_drill", side_effect=fake_restore),
                mock.patch.object(
                    backup_drill.sys,
                    "stdin",
                    types.SimpleNamespace(buffer=StagedWorkerInput()),
                ),
                mock.patch.object(backup_drill.sys, "stdout", stdout),
            ):
                self.assertEqual(backup_drill._scratch_worker_main(args), 0)

            self.assertEqual(
                json.loads(stdout_buffer.getvalue().decode("ascii")),
                {"ok": True, "restored_table_count": 7},
            )
            self.assertEqual(observed["dump_path"], scratch_root / "archive.dump")
            self.assertEqual(
                observed["archive_identity"],
                (archive_stat.st_dev, archive_stat.st_ino),
            )
            self.assertEqual(observed["archive_offset"], 0)
            self.assertTrue(observed["archive_open_during_restore"])
            self.assertEqual(observed["archive_mode"], "rb")
            self.assertEqual(observed["app_target"].username, backup_drill.SCRATCH_APP_ROLE)
            self.assertEqual(observed["admin_target"].username, "postgres")
            self.assertTrue(observed["restore_as_postgres"])
            self.assertTrue(observed["restore_no_tablespaces"])
            self.assertTrue(callable(observed["resource_guard"]))
            create_role.assert_called_once()
            self.assertEqual(create_role.call_args.args[0].username, "postgres")
            self.assertTrue(any(command[:2] == ["pg_config", "--bindir"] for command, _ in command_calls))
            self.assertTrue(any(command[-1:] == ["start"] for command, _ in command_calls))
            self.assertTrue(any(command[-1:] == ["stop"] for command, _ in command_calls))
            self.assertFalse((scratch_root / "data" / "postmaster.pid").exists())
        finally:
            shutil.rmtree(scratch_root)

    def _assert_scratch_sigterm_stops_and_reaps_launched_unit(self) -> None:
        class LauncherProcess:
            def __init__(self) -> None:
                self.returncode: int | None = None
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO()
                self.wait_calls = 0

            def poll(self) -> int | None:
                return self.returncode

            def wait(self, *, timeout: float | None = None) -> int:
                self.wait_calls += 1
                if self.returncode is None:
                    raise subprocess.TimeoutExpired(["systemd-run"], timeout)
                return self.returncode

            def kill(self) -> None:
                self.returncode = -signal.SIGKILL

        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            launcher = LauncherProcess()
            prior_handlers = {
                signum: signal.getsignal(signum)
                for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
            }

            def launch(*_args, **_kwargs):
                # The implementation blocks these signals until it has stored
                # the Popen handle and marked the unique unit as possibly live.
                signal.raise_signal(signal.SIGTERM)
                return launcher

            unit_command = mock.Mock(return_value=["systemd-run"])

            def stop_unit(_unit_name: str) -> None:
                launcher.returncode = 0

            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(
                    backup_drill,
                    "_scratch_service_snapshot",
                    return_value={
                        "properties": {
                            "LoadState": "not-found",
                            "ActiveState": "inactive",
                            "ExecMainPID": "0",
                            "ControlGroup": "",
                        }
                    },
                ),
                mock.patch.object(backup_drill, "_scratch_stop_unit", side_effect=stop_unit) as stop,
                mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch) as popen,
            ):
                with backup_drill._termination_signal_scope():
                    with self.assertRaisesRegex(backup_drill.BackupOperationInterrupted, "SIGTERM"):
                        backup_drill._run_isolated_restore_drill(
                            archive_path,
                            timestamp_slug="20261012T010203Z",
                        )

            self.assertEqual(popen.call_count, 1)
            self.assertEqual(stop.call_count, 1)
            self.assertEqual(launcher.wait_calls, 1)
            mountpoint = unit_command.call_args.kwargs["mountpoint"]
            self.assertFalse(mountpoint.exists())
            self.assertEqual(
                {signum: signal.getsignal(signum) for signum in prior_handlers},
                prior_handlers,
            )

            # A stop failure must not bypass the independent launcher reap.
            failed_stop_launcher = LauncherProcess()

            def launch_then_term(*_args, **_kwargs):
                signal.raise_signal(signal.SIGHUP)
                return failed_stop_launcher

            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(
                    backup_drill,
                    "_scratch_service_snapshot",
                    return_value={
                        "properties": {
                            "LoadState": "loaded",
                            "ActiveState": "active",
                            "ExecMainPID": "123",
                            "InvocationID": "0123456789abcdef0123456789abcdef",
                            "ControlGroup": "/synthetic/scratch.service",
                        }
                    },
                ),
                mock.patch.object(
                    backup_drill,
                    "_scratch_stop_unit",
                    side_effect=RuntimeError("synthetic stop failure"),
                ),
                mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch_then_term),
            ):
                with backup_drill._termination_signal_scope():
                    with self.assertRaisesRegex(
                        backup_drill.BackupOperationInterrupted, "SIGHUP"
                    ) as interrupted:
                        backup_drill._run_isolated_restore_drill(
                            archive_path,
                            timestamp_slug="20261012T010204Z",
                        )
            self.assertEqual(failed_stop_launcher.wait_calls, 2)
            self.assertIsNotNone(failed_stop_launcher.returncode)
            self.assertTrue(
                any("RuntimeError" in note for note in interrupted.exception.__notes__)
            )
            self.assertEqual(
                {signum: signal.getsignal(signum) for signum in prior_handlers},
                prior_handlers,
            )
            failed_mountpoint = unit_command.call_args.kwargs["mountpoint"]
            self.assertTrue(failed_mountpoint.exists())
            mountpoint_stat = failed_mountpoint.lstat()
            self.assertEqual(mountpoint_stat.st_mode & 0o777, 0o700)
            self.assertEqual((mountpoint_stat.st_uid, mountpoint_stat.st_gid), (0, 0))
            self.assertEqual(list(failed_mountpoint.iterdir()), [])
            failed_mountpoint.rmdir()

    def _assert_low_space_abort_kills_owned_restore_and_drops_only_drill_db(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", "synthetic-secret", "platformdb"
        )
        total_bytes = 100 * 1024**3
        floor = backup_drill._required_disk_floor(total_bytes)
        self.assertEqual(floor, 15 * 1024**3)
        self.assertEqual(
            backup_drill._required_disk_floor(20 * 1024**3), 5 * 1024**3
        )
        self.assertEqual(
            backup_drill._required_disk_floor(40 * 1024**3 + 1),
            6 * 1024**3 + 1,
        )
        high = type(
            "Disk",
            (),
            {
                "valid": True,
                "total_bytes": total_bytes,
                "free_bytes": floor + backup_drill.RESTORE_DISK_LEAD_BYTES,
            },
        )()
        low = type(
            "Disk",
            (),
            {
                "valid": True,
                "total_bytes": total_bytes,
                "free_bytes": floor + backup_drill.RESTORE_DISK_LEAD_BYTES - 1,
            },
        )()

        class RestoreChild:
            def __init__(self) -> None:
                self.stopped = False
                self.wait_calls = 0
                self.wait_timeouts: list[float | None] = []
                self.terminated = False
                self.killed = False

            def poll(self) -> int | None:
                return -signal.SIGKILL if self.stopped else None

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True
                self.stopped = True

            def wait(self, *, timeout: float | None = None) -> int:
                self.wait_calls += 1
                self.wait_timeouts.append(timeout)
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired("pg_restore", timeout)
                self.stopped = True
                return -signal.SIGKILL

        child = RestoreChild()
        command_results = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "0\n", ""),
        ]
        with (
            tempfile.TemporaryDirectory() as temporary_dir,
            mock.patch.object(backup_drill, "snapshot_for_path", side_effect=(high, high, low)),
            mock.patch.object(backup_drill.subprocess, "Popen", return_value=child) as popen,
            mock.patch.object(backup_drill.time, "sleep"),
            mock.patch.object(
                backup_drill, "run_command", side_effect=command_results
            ) as run_command,
        ):
            with self.assertRaisesRegex(backup_drill.RestoreGuardStop, "low-disk safety guard") as failure:
                backup_drill.perform_restore_drill(
                    pathlib.Path(temporary_dir) / "backup.dump",
                    app_target=target,
                    admin_target=None,
                    timestamp_slug="20260720T120000Z",
                )

        popen.assert_called_once()
        self.assertIs(popen.call_args.kwargs["stdout"], backup_drill.subprocess.DEVNULL)
        self.assertIs(popen.call_args.kwargs["stderr"], backup_drill.subprocess.DEVNULL)
        self.assertTrue(child.terminated)
        self.assertTrue(child.killed)
        self.assertEqual(child.wait_calls, 2)
        self.assertEqual(child.wait_timeouts, [1.0, None])
        drop_command = run_command.call_args_list[-2].args[0]
        self.assertIn("dropdb", drop_command)
        self.assertEqual(
            drop_command[-1],
            f"platform_restore_drill_20260720t120000z_{os.getpid()}",
        )
        diagnostic = failure.exception.restore_diagnostic
        self.assertEqual(diagnostic["restore_stage"], "restore_platform")
        self.assertEqual(diagnostic["guard_reason"], "disk_floor")
        self.assertEqual(diagnostic["free_bytes"], low.free_bytes)
        self.assertEqual(
            diagnostic["required_free_bytes"],
            floor + backup_drill.RESTORE_DISK_LEAD_BYTES,
        )
        self.assertTrue(diagnostic["temporary_database_created"])
        self.assertEqual(diagnostic["drop_outcome"], "confirmed_absent")
        self.assertIs(diagnostic["temporary_database_absent"], True)

        before_create = backup_drill.RestoreGuardStop(
            "disk_floor", free_bytes=10, required_free_bytes=20
        )
        with (
            mock.patch.object(
                backup_drill, "require_restore_headroom", side_effect=before_create
            ),
            mock.patch.object(backup_drill, "run_command") as preflight_commands,
        ):
            with self.assertRaises(backup_drill.RestoreGuardStop) as preflight_failure:
                backup_drill.perform_restore_drill(
                    pathlib.Path("/unused.dump"),
                    app_target=target,
                    admin_target=None,
                    timestamp_slug="20260720T120000Z",
                )
        preflight_commands.assert_not_called()
        preflight_diagnostic = preflight_failure.exception.restore_diagnostic
        self.assertEqual(preflight_diagnostic["restore_stage"], "pre_create_admission")
        self.assertEqual(preflight_diagnostic["guard_reason"], "disk_floor")
        self.assertIs(preflight_diagnostic["temporary_database_created"], False)
        self.assertEqual(preflight_diagnostic["drop_outcome"], "not_required")
        self.assertIsNone(preflight_diagnostic["temporary_database_absent"])

        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(
                backup_drill,
                "run_command",
                side_effect=subprocess.CalledProcessError(1, ["createdb"]),
            ) as create_command,
        ):
            with self.assertRaises(subprocess.CalledProcessError) as create_failure:
                backup_drill.perform_restore_drill(
                    pathlib.Path("/unused.dump"),
                    app_target=target,
                    admin_target=None,
                    timestamp_slug="20260720T120000Z",
                )
        create_command.assert_called_once()
        create_diagnostic = create_failure.exception.restore_diagnostic
        self.assertEqual(create_diagnostic["restore_stage"], "create_database")
        self.assertIsNone(create_diagnostic["temporary_database_created"])
        self.assertEqual(create_diagnostic["drop_outcome"], "not_required")
        self.assertIsNone(create_diagnostic["temporary_database_absent"])

        drop_failure_commands = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CalledProcessError(1, ["dropdb"]),
        ]
        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(
                backup_drill,
                "run_command",
                side_effect=drop_failure_commands,
            ),
            mock.patch.object(
                backup_drill,
                "run_restore_command",
                side_effect=backup_drill.RestoreGuardStop(
                    "disk_floor", free_bytes=10, required_free_bytes=20
                ),
            ),
        ):
            with self.assertRaises(backup_drill.RestoreGuardStop) as drop_failure:
                backup_drill.perform_restore_drill(
                    pathlib.Path("/unused.dump"),
                    app_target=target,
                    admin_target=None,
                    timestamp_slug="20260720T120000Z",
                )
        drop_diagnostic = drop_failure.exception.restore_diagnostic
        self.assertEqual(drop_diagnostic["restore_stage"], "restore_platform")
        self.assertEqual(drop_diagnostic["guard_reason"], "disk_floor")
        self.assertEqual(drop_diagnostic["drop_outcome"], "drop_failed")
        self.assertIsNone(drop_diagnostic["temporary_database_absent"])

        serialized_error = backup_drill.RestoreGuardStop("disk_floor")
        serialized_diagnostic = {
            **diagnostic,
            "archive_sha256": "a" * 64,
            "archive_size_bytes": 120,
        }
        serialized_error.restore_diagnostic = serialized_diagnostic
        cli_args = argparse.Namespace(
            check_latest=False,
            verify_latest_existing=True,
            dump_only=False,
            verify_dump=None,
            output_dir="/private/backups",
            env_file="/private/env",
            max_age_hours=24.0,
            admin_database_url=None,
            as_json=True,
        )
        output = io.StringIO()
        with (
            mock.patch.object(backup_drill, "parse_args", return_value=cli_args),
            mock.patch.object(
                backup_drill,
                "verify_latest_existing_backup",
                side_effect=serialized_error,
            ),
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(backup_drill.main(), 1)
        serialized = json.loads(output.getvalue())
        self.assertEqual(serialized["restore_diagnostic"], serialized_diagnostic)
        self._assert_unavailable_disk_fails_before_child_start(target)
        self._assert_normal_guarded_child_completes(target, high)
        self._assert_low_space_abort_stops_only_owned_dump()
        self._assert_backup_admission_reports_private_disk_phase()

    def _assert_unavailable_disk_fails_before_child_start(
        self, target: backup_drill.DatabaseTarget
    ) -> None:
        invalid = type(
            "Disk", (), {"valid": False, "total_bytes": 0, "free_bytes": 0}
        )()
        with (
            mock.patch.object(backup_drill, "snapshot_for_path", return_value=invalid),
            mock.patch.object(backup_drill.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(
                backup_drill.RestoreGuardStop, "disk space could not be verified"
            ):
                backup_drill.run_disk_guarded_command(
                    ["pg_dump", "--file", "/private/temp.dump"],
                    target=target,
                    stage="pg_dump",
                )
        popen.assert_not_called()

    def _assert_normal_guarded_child_completes(
        self, target: backup_drill.DatabaseTarget, disk_snapshot: object
    ) -> None:
        class CompletedChild:
            def __init__(self) -> None:
                self.poll_calls = 0

            def poll(self) -> int | None:
                self.poll_calls += 1
                return None if self.poll_calls == 1 else 0

            def wait(self, *, timeout: float | None = None) -> int:
                return 0

        child = CompletedChild()
        with (
            mock.patch.object(
                backup_drill,
                "snapshot_for_path",
                return_value=disk_snapshot,
            ),
            mock.patch.object(backup_drill.subprocess, "Popen", return_value=child) as popen,
            mock.patch.object(backup_drill.time, "sleep"),
        ):
            backup_drill.run_disk_guarded_command(
                ["pg_dump", "--file", "/private/temp.dump"],
                target=target,
                stage="pg_dump",
            )
        popen.assert_called_once()
        self.assertEqual(popen.call_args.kwargs["stdout"], backup_drill.subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs["stderr"], backup_drill.subprocess.DEVNULL)

    def _assert_low_space_abort_stops_only_owned_dump(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", "synthetic-secret", "platformdb"
        )
        total_bytes = 100 * 1024**3
        floor = backup_drill._required_disk_floor(total_bytes)
        high = type(
            "Disk",
            (),
            {
                "valid": True,
                "total_bytes": total_bytes,
                "free_bytes": floor + backup_drill.RESTORE_DISK_LEAD_BYTES,
            },
        )()
        low = type(
            "Disk",
            (),
            {
                "valid": True,
                "total_bytes": total_bytes,
                "free_bytes": floor + backup_drill.RESTORE_DISK_LEAD_BYTES - 1,
            },
        )()

        class DumpChild:
            def __init__(self) -> None:
                self.stopped = False
                self.terminated = False
                self.wait_calls = 0

            def poll(self) -> int | None:
                return 0 if self.stopped else None

            def terminate(self) -> None:
                self.terminated = True
                self.stopped = True

            def kill(self) -> None:
                raise AssertionError("graceful TERM should stop the fake pg_dump child")

            def wait(self, *, timeout: float | None = None) -> int:
                self.wait_calls += 1
                return 0

        child = DumpChild()
        with (
            mock.patch.object(backup_drill, "snapshot_for_path", side_effect=(high, high, low)),
            mock.patch.object(backup_drill.subprocess, "Popen", return_value=child) as popen,
            mock.patch.object(backup_drill.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                backup_drill.RestoreGuardStop, "low-disk safety guard"
            ) as raised:
                backup_drill.run_disk_guarded_command(
                    ["pg_dump", "--file", "/private/temp.dump"],
                    target=target,
                    stage="pg_dump",
                )
        failure = raised.exception

        popen.assert_called_once()
        self.assertIs(popen.call_args.kwargs["stdout"], backup_drill.subprocess.DEVNULL)
        self.assertIs(popen.call_args.kwargs["stderr"], backup_drill.subprocess.DEVNULL)
        self.assertTrue(child.terminated)
        self.assertEqual(child.wait_calls, 1)
        self.assertEqual(
            failure.private_backup_disk_diagnostic(),
            {
                "schema": 1,
                "phase": "pg_dump_monitor",
                "guard_reason": "disk_floor",
                "free_bytes": low.free_bytes,
                "required_free_bytes": floor + backup_drill.RESTORE_DISK_LEAD_BYTES,
            },
        )

    def _assert_backup_admission_reports_private_disk_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_dump, old_sidecar = self._write_backup_pair(
                output_dir, "platformdb-existing.dump", b"retained backup"
            )
            before = {
                path: (path.read_bytes(), path.lstat().st_ino, path.lstat().st_mode)
                for path in (old_dump, old_sidecar)
            }
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=1,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )
            guard = backup_drill.RestoreGuardStop(
                "disk_floor",
                free_bytes=100,
                required_free_bytes=200,
                phase="backup_admission",
            )
            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_restore_headroom", side_effect=guard),
                mock.patch.object(backup_drill, "require_commands") as require_commands,
                mock.patch.object(backup_drill.tempfile, "mkstemp") as mkstemp,
            ):
                with self.assertRaises(backup_drill.RestoreGuardStop) as raised:
                    backup_drill.create_backup(args)

            require_commands.assert_not_called()
            mkstemp.assert_not_called()
            self.assertEqual(
                raised.exception.private_backup_disk_diagnostic(),
                {
                    "schema": 1,
                    "phase": "backup_admission",
                    "guard_reason": "disk_floor",
                    "free_bytes": 100,
                    "required_free_bytes": 200,
                },
            )
            for path, expected in before.items():
                metadata = path.lstat()
                self.assertEqual(
                    (path.read_bytes(), metadata.st_ino, metadata.st_mode), expected
                )

    def test_check_latest_validates_restore_age_and_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            env_file = output_dir / ".env.platform"
            env_file.write_text(
                "PLATFORM_DATABASE_URL=postgresql://platform_user:synthetic@127.0.0.1/platformdb\n",
                encoding="utf-8",
            )
            dump_path = output_dir / "platformdb-20260714T120000Z.dump"
            dump_path.write_bytes(b"PGDMPcustom-format-backup")
            dump_path.chmod(0o600)
            metadata_path = dump_path.with_suffix(".json")
            original_created_at = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
            metadata_path.write_text(
                json.dumps(
                    {
                        "format_version": 2,
                        "database": "platformdb",
                        "schemas": ["platform", "public"],
                        "required_extensions": ["pg_trgm"],
                        "dump_file": dump_path.name,
                        "size_bytes": dump_path.stat().st_size,
                        "sha256": hashlib.sha256(dump_path.read_bytes()).hexdigest(),
                        "completed_at_utc": original_created_at,
                        "restore_verified": False,
                        "alembic_revision_verified": False,
                        "restored_table_count": 31,
                        "restore_error": "historical closed diagnostic",
                    }
                ),
                encoding="utf-8",
            )
            metadata_path.chmod(0o600)

            original_dump_stat = dump_path.stat()
            with mock.patch.dict(
                backup_drill.os.environ,
                {"PLATFORM_DATABASE_URL": "postgresql://platform_user:synthetic@127.0.0.1/platformdb"},
            ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                backup_drill,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as run_command, mock.patch.object(
                backup_drill, "_run_isolated_restore_drill", return_value=31
            ) as perform_restore:
                result = backup_drill.verify_latest_existing_backup(
                    output_dir, env_file, max_age_hours=24
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["restored_table_count"], 31)
            run_command.assert_not_called()
            perform_restore.assert_called_once()
            updated = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["completed_at_utc"], original_created_at)
            self.assertEqual(updated["restore_error"], "historical closed diagnostic")
            self.assertIs(updated["restore_verified"], True)
            self.assertIs(updated["alembic_revision_verified"], True)
            self.assertIsInstance(updated["restore_verified_at_utc"], str)
            self.assertEqual(
                backup_drill.check_latest_backup(output_dir, max_age_hours=24)["sha256"],
                hashlib.sha256(b"PGDMPcustom-format-backup").hexdigest(),
            )
            self.assertEqual(dump_path.read_bytes(), b"PGDMPcustom-format-backup")
            self.assertEqual(
                (dump_path.stat().st_ino, dump_path.stat().st_mtime_ns),
                (original_dump_stat.st_ino, original_dump_stat.st_mtime_ns),
            )

            updated_sidecar_bytes = metadata_path.read_bytes()
            with mock.patch.dict(
                backup_drill.os.environ,
                {"PLATFORM_DATABASE_URL": "postgresql://platform_user:synthetic@127.0.0.1/platformdb"},
            ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                backup_drill,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ), mock.patch.object(
                backup_drill,
                "_run_isolated_restore_drill",
                side_effect=backup_drill.RestoreGuardStop("command_failed"),
            ):
                with self.assertRaises(backup_drill.RestoreGuardStop):
                    backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )
            self.assertEqual(metadata_path.read_bytes(), updated_sidecar_bytes)
            self.assertEqual(dump_path.read_bytes(), b"PGDMPcustom-format-backup")

            with mock.patch.dict(
                backup_drill.os.environ,
                {"PLATFORM_DATABASE_URL": "postgresql://platform_user:synthetic@127.0.0.1/platformdb"},
            ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                backup_drill,
                "_run_isolated_restore_drill",
                side_effect=subprocess.CalledProcessError(1, ["systemd-run"]),
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )
            self.assertEqual(metadata_path.read_bytes(), updated_sidecar_bytes)
            self.assertEqual(dump_path.read_bytes(), b"PGDMPcustom-format-backup")

            crossing = dict(updated)
            start_now = dt.datetime.now(dt.UTC)
            crossing["completed_at_utc"] = (
                start_now - dt.timedelta(hours=23, minutes=59)
            ).isoformat().replace("+00:00", "Z")
            metadata_path.write_text(json.dumps(crossing), encoding="utf-8")
            metadata_path.chmod(0o600)
            crossing_bytes = metadata_path.read_bytes()
            with mock.patch.dict(
                backup_drill.os.environ,
                {"PLATFORM_DATABASE_URL": "postgresql://platform_user:synthetic@127.0.0.1/platformdb"},
            ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                backup_drill,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ), mock.patch.object(
                backup_drill, "_run_isolated_restore_drill", return_value=31
            ), mock.patch.object(
                backup_drill,
                "utc_now",
                side_effect=[start_now, start_now, start_now + dt.timedelta(hours=2)],
            ):
                with self.assertRaisesRegex(RuntimeError, "creation-age window"):
                    backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )
            self.assertEqual(metadata_path.read_bytes(), crossing_bytes)
            self.assertEqual(dump_path.read_bytes(), b"PGDMPcustom-format-backup")

            # A directory-fsync failure after rename must restore the exact old sidecar.
            stable_bytes = metadata_path.read_bytes()
            with mock.patch.dict(
                backup_drill.os.environ,
                {"PLATFORM_DATABASE_URL": "postgresql://platform_user:synthetic@127.0.0.1/platformdb"},
            ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                backup_drill,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ), mock.patch.object(
                backup_drill, "_run_isolated_restore_drill", return_value=31
            ), mock.patch.object(
                backup_drill.os,
                "fsync",
                side_effect=[None, OSError("synthetic directory fsync failure"), None, None],
            ):
                with self.assertRaisesRegex(OSError, "synthetic directory fsync failure"):
                    backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )
            self.assertEqual(metadata_path.read_bytes(), stable_bytes)
            self.assertEqual(dump_path.read_bytes(), b"PGDMPcustom-format-backup")

            def write_metadata(value: dict[str, object]) -> None:
                metadata_path.write_text(json.dumps(value), encoding="utf-8")
                metadata_path.chmod(0o600)

            def restore_hook(callback):
                with mock.patch.dict(
                    backup_drill.os.environ,
                    {
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user:synthetic@127.0.0.1/platformdb"
                        )
                    },
                ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                    backup_drill,
                    "run_command",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ), mock.patch.object(
                backup_drill, "_run_isolated_restore_drill", side_effect=callback
                ):
                    return backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )

            size_omitted_v1 = dict(updated)
            size_omitted_v1["format_version"] = 1
            size_omitted_v1.pop("size_bytes", None)
            write_metadata(size_omitted_v1)
            original_v1_bytes = metadata_path.read_bytes()
            failure = subprocess.CalledProcessError(1, ["createdb"])
            failure.restore_diagnostic = {
                "schema": 1,
                "restore_stage": "create_database",
                "guard_reason": "none",
                "free_bytes": None,
                "required_free_bytes": None,
                "temporary_database_created": None,
                "drop_outcome": "not_required",
                "temporary_database_absent": None,
            }
            with self.assertRaises(subprocess.CalledProcessError) as failed_v1:
                restore_hook(lambda *_args, **_kwargs: (_ for _ in ()).throw(failure))
            self.assertEqual(
                failed_v1.exception.restore_diagnostic["archive_size_bytes"],
                dump_path.stat().st_size,
            )
            self.assertEqual(
                failed_v1.exception.restore_diagnostic["archive_sha256"],
                hashlib.sha256(dump_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(metadata_path.read_bytes(), original_v1_bytes)

            baseline = dict(updated)
            baseline["completed_at_utc"] = original_created_at
            write_metadata(baseline)
            malformed = b'{"dump_file":"one","dump_file":"two"}'
            metadata_path.write_bytes(malformed)
            metadata_path.chmod(0o600)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "malformed"
            )

            metadata_path.write_bytes(b" " * (1_048_577))
            metadata_path.chmod(0o600)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "unsafe file metadata"
            )

            wrong_schema = dict(baseline)
            wrong_schema["schemas"] = ["platform", "sparkydb"]
            write_metadata(wrong_schema)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "unsupported schema set"
            )

            naive_timestamp = dict(baseline)
            naive_timestamp["completed_at_utc"] = dt.datetime.now(dt.UTC).replace(
                tzinfo=None
            ).isoformat()
            write_metadata(naive_timestamp)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "creation timestamp is invalid"
            )

            write_metadata(baseline)
            saved_metadata = output_dir / ".platformdb-metadata.saved"
            metadata_path.rename(saved_metadata)
            metadata_path.symlink_to(saved_metadata.name)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "Too many levels|unsafe file metadata"
            )
            metadata_path.unlink()
            saved_metadata.rename(metadata_path)

            hardlink_path = output_dir / ".platformdb-metadata.hardlink"
            os.link(metadata_path, hardlink_path)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "unsafe file metadata"
            )
            hardlink_path.unlink()

            dump_hardlink = output_dir / ".platformdb-dump.hardlink"
            os.link(dump_path, dump_hardlink)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "unsafe file metadata"
            )
            dump_hardlink.unlink()

            metadata_path.chmod(0o640)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "unsafe file metadata"
            )
            metadata_path.chmod(0o600)

            if os.geteuid() == 0:
                os.chown(metadata_path, 65534, metadata_path.stat().st_gid)
                self._assert_latest_verification_preflight_rejects(
                    output_dir, env_file, "unsafe file metadata"
                )
                os.chown(metadata_path, 0, 0)
                metadata_path.chmod(0o600)
                os.chown(metadata_path, 0, 65534)
                self._assert_latest_verification_preflight_rejects(
                    output_dir, env_file, "unsafe file metadata"
                )
                os.chown(metadata_path, 0, 0)

            original_archive_bytes = dump_path.read_bytes()
            original_archive_stat = dump_path.stat()

            def mutate_archive_during_restore(*_args, **_kwargs):
                dump_path.write_bytes(b"mutated same inode")
                os.utime(
                    dump_path,
                    ns=(original_archive_stat.st_atime_ns, original_archive_stat.st_mtime_ns),
                )
                return 31

            original_sidecar_bytes = metadata_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "checksum does not match"):
                restore_hook(mutate_archive_during_restore)
            self.assertEqual(metadata_path.read_bytes(), original_sidecar_bytes)
            self.assertEqual(dump_path.stat().st_ino, original_archive_stat.st_ino)
            dump_path.write_bytes(original_archive_bytes)
            dump_path.chmod(0o600)

            def replace_metadata_during_restore(*_args, **_kwargs):
                replacement = dict(baseline)
                replacement["restore_error"] = "external metadata replacement"
                replacement.pop("restore_verified_at_utc", None)
                replacement_path = output_dir / ".replacement.json"
                replacement_path.write_text(json.dumps(replacement), encoding="utf-8")
                replacement_path.chmod(0o600)
                os.replace(replacement_path, metadata_path)
                return 31

            with self.assertRaisesRegex(RuntimeError, "metadata changed during restore"):
                restore_hook(replace_metadata_during_restore)
            replacement_after = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(replacement_after["restore_error"], "external metadata replacement")
            self.assertNotIn("restore_verified_at_utc", replacement_after)

            write_metadata(baseline)

            def add_newer_pair_during_restore(*_args, **_kwargs):
                newer_dump = output_dir / "platformdb-newer.dump"
                newer_dump.write_bytes(b"newer archive")
                newer_dump.chmod(0o600)
                newer_metadata = newer_dump.with_suffix(".json")
                newer_metadata.write_text(
                    json.dumps(
                        {
                            "dump_file": newer_dump.name,
                            "sha256": hashlib.sha256(newer_dump.read_bytes()).hexdigest(),
                            "size_bytes": newer_dump.stat().st_size,
                            "format_version": 2,
                            "database": "platformdb",
                            "schemas": ["platform", "public"],
                            "required_extensions": ["pg_trgm"],
                            "completed_at_utc": original_created_at,
                            "restore_verified": False,
                            "alembic_revision_verified": False,
                        }
                    ),
                    encoding="utf-8",
                )
                newer_metadata.chmod(0o600)
                future_ns = metadata_path.stat().st_mtime_ns + 10_000_000
                os.utime(newer_metadata, ns=(future_ns, future_ns))
                return 31

            original_sidecar_bytes = metadata_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "metadata changed during restore"):
                restore_hook(add_newer_pair_during_restore)
            self.assertEqual(metadata_path.read_bytes(), original_sidecar_bytes)

            too_old = dict(updated)
            too_old["completed_at_utc"] = (
                dt.datetime.now(dt.UTC) - dt.timedelta(hours=25)
            ).isoformat().replace("+00:00", "Z")
            metadata_path.write_text(json.dumps(too_old), encoding="utf-8")
            metadata_path.chmod(0o600)
            rewritten_stat = metadata_path.stat()
            os.utime(
                metadata_path,
                ns=(rewritten_stat.st_atime_ns, rewritten_stat.st_mtime_ns + 1_000_000_000),
            )
            expired_bytes = metadata_path.read_bytes()
            with mock.patch.object(backup_drill, "require_commands") as require_commands:
                with self.assertRaisesRegex(RuntimeError, "creation-age window"):
                    backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )
            require_commands.assert_not_called()
            self.assertEqual(metadata_path.read_bytes(), expired_bytes)

    def test_check_latest_cli_rejects_unverified_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            dump_path = output_dir / "platformdb-20260714T120000Z.dump"
            dump_path.write_bytes(b"PGDMPcustom-format-backup")
            dump_path.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "dump_file": dump_path.name,
                        "sha256": hashlib.sha256(dump_path.read_bytes()).hexdigest(),
                        "completed_at_utc": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
                        "restore_verified": False,
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--output-dir",
                    str(output_dir),
                    "--check-latest",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("not restore-verified", result.stderr)

    def test_prune_unverified_backups_keeps_verified_archive(self) -> None:
        with mock.patch.object(backup_drill.sys, "argv", ["platform_backup_restore_drill.py"]):
            self.assertTrue(backup_drill.parse_args().rotate_existing)
        with mock.patch.object(
            backup_drill.sys,
            "argv",
            ["platform_backup_restore_drill.py", "--preserve-existing"],
        ):
            self.assertFalse(backup_drill.parse_args().rotate_existing)

        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            verified_dump = output_dir / "platformdb-verified.dump"
            failed_dump = output_dir / "platformdb-failed.dump"
            verified_dump.write_bytes(b"verified")
            failed_dump.write_bytes(b"failed")
            verified_metadata = verified_dump.with_suffix(".json")
            failed_metadata = failed_dump.with_suffix(".json")
            verified_metadata.write_text(
                json.dumps({"dump_file": verified_dump.name, "restore_verified": True}),
                encoding="utf-8",
            )
            failed_metadata.write_text(
                json.dumps({"dump_file": failed_dump.name, "restore_verified": False}),
                encoding="utf-8",
            )

            removed = backup_drill.prune_unverified_backups(
                output_dir,
                preserve_metadata=verified_metadata,
            )

            self.assertTrue(verified_dump.exists())
            self.assertTrue(verified_metadata.exists())
            self.assertFalse(failed_dump.exists())
            self.assertFalse(failed_metadata.exists())
            self.assertEqual(len(removed), 2)

    def test_restore_failure_does_not_prune_existing_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_dump, old_metadata = self._write_backup_pair(
                output_dir, "platformdb-old.dump", b"old-backup"
            )
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=1,
                admin_database_url=None,
                dump_only=False,
                rotate_existing=False,
            )
            old_entries = []
            for index in range(15):
                dump, metadata = self._write_backup_pair(
                    output_dir,
                    f"platformdb-old-{index:02}.dump",
                    f"old-{index}".encode(),
                )
                old_entries.extend((dump, metadata))
            unverified_dump, unverified_metadata = self._write_backup_pair(
                output_dir,
                "platformdb-unverified.dump",
                b"unverified-old",
                restore_verified=False,
            )
            old_entries.extend((unverified_dump, unverified_metadata))
            old_entries.extend((old_dump, old_metadata))
            before = {
                path: (path.read_bytes(), path.lstat().st_mode, path.lstat().st_ino)
                for path in old_entries
            }

            with tempfile.TemporaryDirectory() as owner_dir:
                owner_probe = pathlib.Path(owner_dir) / "owner-probe.dump"
                owner_probe.write_bytes(b"bounded owner check")
                owner_probe.chmod(0o600)
                owner_probe_stat = owner_probe.lstat()
                owner_identity = (owner_probe_stat.st_dev, owner_probe_stat.st_ino)
                backup_drill._fsync_backup_dump(
                    owner_probe,
                    expected_identity=owner_identity,
                    expected_uid=os.geteuid(),
                )
                with (
                    mock.patch.object(backup_drill.os, "fsync") as rejected_fsync,
                    self.assertRaisesRegex(
                        RuntimeError, "Temporary backup archive identity is unsafe before fsync"
                    ),
                ):
                    backup_drill._fsync_backup_dump(
                        owner_probe,
                        expected_identity=owner_identity,
                        expected_uid=os.geteuid() + 1,
                    )
                rejected_fsync.assert_not_called()

            initial_inventory = backup_drill.backup_inventory(output_dir)
            fixed_backup_time = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
            operation_order: list[str] = []
            real_fsync_dump = backup_drill._fsync_backup_dump

            def record_dump_fsync(
                path: pathlib.Path,
                *,
                expected_identity: tuple[int, int],
                expected_uid: int,
            ) -> None:
                operation_order.append("dump_fsync")
                real_fsync_dump(
                    path, expected_identity=expected_identity, expected_uid=expected_uid
                )

            def fail_restore_after_fsync(*_args: object, **_kwargs: object) -> int:
                operation_order.append("restore")
                self.assertEqual(operation_order, ["dump_fsync", "restore"])
                raise RuntimeError("restore failed")

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                    clear=False,
                ),
                mock.patch.object(
                    backup_drill,
                    "load_env",
                    return_value={
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                ),
                mock.patch.object(backup_drill, "utc_now", return_value=fixed_backup_time),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(
                    backup_drill,
                    "_fsync_backup_dump",
                    side_effect=record_dump_fsync,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(
                    backup_drill,
                    "_run_isolated_restore_drill",
                    side_effect=fail_restore_after_fsync,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "restore failed") as raised:
                    backup_drill.create_backup(args)
            self.assertEqual(operation_order, ["dump_fsync", "restore"])
            output = io.StringIO()
            main_args = argparse.Namespace(
                check_latest=False,
                dump_only=False,
                verify_dump=None,
                output_dir=str(output_dir),
                max_age_hours=24,
                as_json=True,
            )
            with (
                mock.patch.object(backup_drill, "parse_args", return_value=main_args),
                mock.patch.object(backup_drill, "create_backup", side_effect=raised.exception),
                mock.patch.object(backup_drill.sys, "stdout", output),
            ):
                self.assertEqual(backup_drill.main(), 1)
            self.assertEqual(json.loads(output.getvalue())["error"], "restore failed")

            self.assertTrue(old_dump.exists())
            self.assertTrue(old_metadata.exists())
            self.assertEqual(backup_drill.backup_inventory(output_dir), initial_inventory)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {path.name for path in old_entries},
            )
            for path, expected in before.items():
                metadata = path.lstat()
                self.assertEqual((path.read_bytes(), metadata.st_mode, metadata.st_ino), expected)

            restore_after_fsync_failure = mock.Mock()
            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                    clear=False,
                ),
                mock.patch.object(
                    backup_drill,
                    "load_env",
                    return_value={
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                ),
                mock.patch.object(
                    backup_drill,
                    "utc_now",
                    return_value=fixed_backup_time + dt.timedelta(seconds=1),
                ),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(
                    backup_drill, "_run_isolated_restore_drill", restore_after_fsync_failure
                ),
                mock.patch.object(
                    backup_drill.os, "fsync", side_effect=OSError("synthetic dump fsync failure")
                ),
            ):
                with self.assertRaisesRegex(OSError, "synthetic dump fsync failure"):
                    backup_drill.create_backup(args)

            restore_after_fsync_failure.assert_not_called()
            self.assertEqual(backup_drill.backup_inventory(output_dir), initial_inventory)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {path.name for path in old_entries},
            )

            dump_only_args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=1,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )
            dump_only_events: list[str] = []

            def record_dump_only_fsync(
                path: pathlib.Path,
                *,
                expected_identity: tuple[int, int],
                expected_uid: int,
            ) -> None:
                dump_only_events.append("dump_fsync")
                real_fsync_dump(
                    path, expected_identity=expected_identity, expected_uid=expected_uid
                )

            dump_only_restore = mock.Mock()
            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                    clear=False,
                ),
                mock.patch.object(
                    backup_drill,
                    "load_env",
                    return_value={
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                ),
                mock.patch.object(
                    backup_drill,
                    "utc_now",
                    side_effect=[
                        fixed_backup_time + dt.timedelta(seconds=2),
                        fixed_backup_time + dt.timedelta(seconds=3),
                        fixed_backup_time + dt.timedelta(seconds=4),
                    ],
                ),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(
                    backup_drill,
                    "_fsync_backup_dump",
                    side_effect=record_dump_only_fsync,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(
                    backup_drill, "_run_isolated_restore_drill", dump_only_restore
                ),
            ):
                dump_only_result = backup_drill.create_backup(dump_only_args)

            self.assertEqual(dump_only_events, ["dump_fsync"])
            dump_only_restore.assert_not_called()
            self.assertTrue(dump_only_result["ok"])
            self.assertIs(dump_only_result["restore_verified"], False)
            initial_inventory = backup_drill.backup_inventory(output_dir)
            old_entries = list(output_dir.iterdir())

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                    clear=False,
                ),
                mock.patch.object(
                    backup_drill,
                    "load_env",
                    return_value={
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                ),
                mock.patch.object(
                    backup_drill,
                    "utc_now",
                    return_value=fixed_backup_time + dt.timedelta(seconds=5),
                ),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(
                    backup_drill,
                    "_run_isolated_restore_drill",
                    side_effect=backup_drill.BackupOperationInterrupted(signal.SIGTERM),
                ),
            ):
                with self.assertRaises(backup_drill.BackupOperationInterrupted):
                    backup_drill.create_backup(args)
            self.assertEqual(backup_drill.backup_inventory(output_dir), initial_inventory)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {path.name for path in old_entries},
            )

            self._assert_postpublication_inventory_failure_rolls_back_only_new_pair()
            self._assert_directory_fsync_failure_rolls_back_only_new_pair()

            self._assert_preserve_existing_keeps_all_archives_after_success()
            self._assert_destination_collision_fails_before_dump_and_preserves_sidecar()
            self._assert_explicit_rotation_retains_keep_policy()

    def _assert_postpublication_inventory_failure_rolls_back_only_new_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_dump, old_metadata = self._write_backup_pair(
                output_dir, "platformdb-old.dump", b"old-backup"
            )
            before = {
                path: (path.read_bytes(), path.lstat().st_mode, path.lstat().st_ino)
                for path in (old_dump, old_metadata)
            }
            initial_inventory = backup_drill.backup_inventory(output_dir)
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=False,
                rotate_existing=False,
            )
            real_inventory = backup_drill.backup_inventory
            inventory_calls = 0

            def fail_after_publication(path: pathlib.Path) -> tuple[object, ...]:
                nonlocal inventory_calls
                inventory_calls += 1
                if inventory_calls == 3:
                    raise RuntimeError("synthetic post-publication inventory failure")
                return real_inventory(path)

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill, "_run_isolated_restore_drill", return_value=1),
                mock.patch.object(backup_drill, "backup_inventory", side_effect=fail_after_publication),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "synthetic post-publication inventory failure"
                ):
                    backup_drill.create_backup(args)

            self.assertEqual(inventory_calls, 4)
            self.assertEqual(backup_drill.backup_inventory(output_dir), initial_inventory)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {old_dump.name, old_metadata.name},
            )
            for path, expected in before.items():
                metadata = path.lstat()
                self.assertEqual((path.read_bytes(), metadata.st_mode, metadata.st_ino), expected)

    def _assert_directory_fsync_failure_rolls_back_only_new_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_entries: list[pathlib.Path] = []
            for index in range(20):
                dump, sidecar = self._write_backup_pair(
                    output_dir,
                    f"platformdb-existing-{index:02}.dump",
                    f"prior-{index}".encode(),
                )
                old_entries.extend((dump, sidecar))
            initial_inventory = backup_drill.backup_inventory(output_dir)
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )
            real_fsync_directory = backup_drill._fsync_directory
            fsync_calls = 0

            def fail_publication_fsync(path: pathlib.Path) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 1:
                    raise OSError("synthetic directory fsync failure")
                real_fsync_directory(path)

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(
                    backup_drill, "_fsync_directory", side_effect=fail_publication_fsync
                ),
            ):
                with self.assertRaisesRegex(OSError, "synthetic directory fsync failure"):
                    backup_drill.create_backup(args)

            self.assertEqual(fsync_calls, 2)
            self.assertEqual(backup_drill.backup_inventory(output_dir), initial_inventory)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {path.name for path in old_entries},
            )

            fsync_calls = 0

            def interrupt_after_publication(path: pathlib.Path) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 1:
                    signal.raise_signal(signal.SIGTERM)
                real_fsync_directory(path)

            cli_args = argparse.Namespace(
                check_latest=False,
                verify_latest_existing=False,
                dump_only=True,
                verify_dump=None,
                output_dir=str(output_dir),
                env_file=str(output_dir / ".env.platform"),
                max_age_hours=24,
                admin_database_url=None,
                keep=14,
                rotate_existing=False,
                as_json=True,
            )
            signal_output = io.StringIO()

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(
                    backup_drill, "_fsync_directory", side_effect=interrupt_after_publication
                ),
                mock.patch.object(backup_drill, "parse_args", return_value=cli_args),
                mock.patch.object(backup_drill.sys, "stdout", signal_output),
            ):
                self.assertEqual(backup_drill.main(), 1)

            self.assertGreaterEqual(fsync_calls, 2)
            self.assertIn("Backup operation interrupted by SIGTERM.", signal_output.getvalue())
            self.assertEqual(backup_drill.backup_inventory(output_dir), initial_inventory)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {path.name for path in old_entries},
            )

    def _assert_preserve_existing_keeps_all_archives_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_entries: list[pathlib.Path] = []
            for index in range(16):
                dump, sidecar = self._write_backup_pair(
                    output_dir,
                    f"platformdb-old-{index:02}.dump",
                    f"verified-{index}".encode(),
                )
                old_entries.extend((dump, sidecar))
            unverified_dump, unverified_sidecar = self._write_backup_pair(
                output_dir,
                "platformdb-old-unverified.dump",
                b"old-unverified",
                restore_verified=False,
            )
            old_entries.extend((unverified_dump, unverified_sidecar))
            before = {
                path: (path.read_bytes(), path.lstat().st_mode, path.lstat().st_ino)
                for path in old_entries
            }
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=1,
                admin_database_url=None,
                dump_only=False,
                rotate_existing=False,
            )

            def fake_run_command(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "pg_dump":
                    pathlib.Path(command[command.index("--file") + 1]).write_bytes(b"new-archive")
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[0] == "pg_restore":
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise AssertionError(f"Unexpected command: {command[0]}")

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill, "_run_isolated_restore_drill", return_value=16),
                mock.patch.object(
                    backup_drill,
                    "utc_now",
                    side_effect=[dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)] * 3,
                ),
            ):
                result = backup_drill.create_backup(args)

            self.assertTrue(result["restore_verified"])
            self.assertEqual(result["rotation_mode"], "preserve-existing")
            self.assertEqual(result["preexisting_archive_count"], 17)
            self.assertEqual(result["preexisting_sidecar_count"], 17)
            self.assertEqual(result["preexisting_archives_preserved"], True)
            self.assertEqual(result["removed"], [])
            for path, expected in before.items():
                metadata = path.lstat()
                self.assertEqual((path.read_bytes(), metadata.st_mode, metadata.st_ino), expected)

    def test_preserve_existing_rejects_incomplete_or_mismatched_pairs(self) -> None:
        for malformed in ("orphan_dump", "orphan_sidecar", "wrong_digest", "wrong_dump_name"):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as temporary_dir:
                output_dir = pathlib.Path(temporary_dir)
                dump_path, sidecar_path = self._write_backup_pair(
                    output_dir, "platformdb-existing.dump", b"existing-archive"
                )
                if malformed == "orphan_dump":
                    sidecar_path.unlink()
                elif malformed == "orphan_sidecar":
                    dump_path.unlink()
                else:
                    metadata = json.loads(sidecar_path.read_text(encoding="utf-8"))
                    metadata["sha256" if malformed == "wrong_digest" else "dump_file"] = (
                        "0" * 64 if malformed == "wrong_digest" else "platformdb-other.dump"
                    )
                    sidecar_path.write_text(json.dumps(metadata), encoding="utf-8")
                args = argparse.Namespace(
                    env_file=str(output_dir / ".env.platform"),
                    output_dir=str(output_dir),
                    keep=1,
                    admin_database_url=None,
                    dump_only=True,
                    rotate_existing=False,
                )
                with (
                    mock.patch.dict(
                        backup_drill.os.environ,
                        {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                    ),
                    mock.patch.object(backup_drill, "load_env", return_value={}),
                    mock.patch.object(backup_drill, "require_commands"),
                    mock.patch.object(backup_drill, "run_disk_guarded_command") as dump_command,
                    self.assertRaisesRegex(RuntimeError, "incomplete|mismatched|invalid"),
                ):
                    backup_drill.create_backup(args)
                dump_command.assert_not_called()

    def test_preserve_existing_detects_same_inode_content_tamper_with_restored_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_dump, _ = self._write_backup_pair(
                output_dir, "platformdb-existing.dump", b"original-payload"
            )
            original_stat = old_dump.stat()
            original_inode = old_dump.lstat().st_ino
            original_mode = old_dump.lstat().st_mode
            original_entry = next(
                entry
                for entry in backup_drill.backup_inventory(output_dir)
                if entry[0] == old_dump.name
            )
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=1,
                admin_database_url=None,
                dump_only=False,
                rotate_existing=False,
            )

            def mutate_existing_archive(*_: object, **__: object) -> int:
                with old_dump.open("r+b") as handle:
                    handle.write(b"tampered-payload")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.utime(old_dump, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                mutated_entry = next(
                    entry
                    for entry in backup_drill.backup_inventory(output_dir)
                    if entry[0] == old_dump.name
                )
                self.assertEqual(original_entry[:7], mutated_entry[:7])
                self.assertNotEqual(original_entry[7], mutated_entry[7])
                return 1

            def fake_run_command(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "pg_dump":
                    pathlib.Path(command[command.index("--file") + 1]).write_bytes(b"new-archive")
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[0] == "pg_restore":
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise AssertionError(f"Unexpected command: {command[0]}")

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                mock.patch.object(backup_drill, "run_disk_guarded_command", side_effect=self._fake_guarded_dump),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill, "_run_isolated_restore_drill", side_effect=mutate_existing_archive),
                mock.patch.object(
                    backup_drill,
                    "utc_now",
                    side_effect=[dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)] * 3,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "mismatched existing backup pair"):
                    backup_drill.create_backup(args)

            self.assertEqual(old_dump.lstat().st_ino, original_inode)
            self.assertEqual(old_dump.lstat().st_mode, original_mode)
            self.assertEqual(old_dump.stat().st_mtime_ns, original_stat.st_mtime_ns)
            self.assertFalse((output_dir / "platformdb-20261006T120000Z.dump").exists())

    def _assert_destination_collision_fails_before_dump_and_preserves_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            timestamp = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
            dump_path = output_dir / "platformdb-20261006T120000Z.dump"
            dump_path, sidecar_path = self._write_backup_pair(
                output_dir, dump_path.name, b"pre-existing-target"
            )
            os.chmod(dump_path, 0o640)
            os.chmod(sidecar_path, 0o640)
            before = (sidecar_path.read_bytes(), sidecar_path.lstat().st_mode, sidecar_path.lstat().st_ino)
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )
            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "utc_now", return_value=timestamp),
                mock.patch.object(backup_drill, "run_command") as run_command,
            ):
                with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
                    backup_drill.create_backup(args)

            run_command.assert_not_called()
            metadata = sidecar_path.lstat()
            self.assertEqual((sidecar_path.read_bytes(), metadata.st_mode, metadata.st_ino), before)
            self.assertTrue(dump_path.exists())

        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            timestamp = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
            dump_path = output_dir / "platformdb-20261006T120000Z.dump"
            dump_path, sidecar_path = self._write_backup_pair(
                output_dir, dump_path.name, b"pre-existing-target"
            )
            os.chmod(dump_path, 0o640)
            os.chmod(sidecar_path, 0o640)
            before = (dump_path.read_bytes(), dump_path.lstat().st_mode, dump_path.lstat().st_ino)
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )
            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "utc_now", return_value=timestamp),
                mock.patch.object(backup_drill, "run_command") as run_command,
            ):
                with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
                    backup_drill.create_backup(args)
            run_command.assert_not_called()
            metadata = dump_path.lstat()
            self.assertEqual((dump_path.read_bytes(), metadata.st_mode, metadata.st_ino), before)
            self.assertTrue(sidecar_path.exists())

        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            timestamp = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
            dump_path = output_dir / "platformdb-20261006T120000Z.dump"
            sidecar_path = dump_path.with_suffix(".json")
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )

            def fake_run_command(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "pg_dump":
                    pathlib.Path(command[command.index("--file") + 1]).write_bytes(b"new-archive")
                return subprocess.CompletedProcess(command, 0, "", "")

            real_link = backup_drill.os.link
            link_count = 0

            def race_sidecar_collision(source: object, destination: object, **kwargs: object) -> None:
                nonlocal link_count
                link_count += 1
                if link_count == 1:
                    real_link(source, destination, **kwargs)
                    return
                sidecar_path.write_bytes(b"racing-sidecar")
                os.chmod(sidecar_path, 0o640)
                raise FileExistsError

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "utc_now", return_value=timestamp),
                mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill.os, "link", side_effect=race_sidecar_collision),
            ):
                with self.assertRaisesRegex(RuntimeError, "destination already exists") as raised:
                    backup_drill.create_backup(args)

            self.assertEqual(
                getattr(raised.exception, "_backup_preservation_integrity", None),
                "preexisting_inventory_changed",
            )
            self.assertEqual(link_count, 2)
            self.assertEqual(sidecar_path.read_bytes(), b"racing-sidecar")
            self.assertEqual(sidecar_path.stat().st_mode & 0o777, 0o640)
            self.assertFalse(dump_path.exists())

    def _assert_explicit_rotation_retains_keep_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_verified: list[pathlib.Path] = []
            base_time = dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp()
            for index in range(16):
                dump = output_dir / f"platformdb-verified-{index:02}.dump"
                sidecar = dump.with_suffix(".json")
                dump.write_bytes(f"old-{index}".encode())
                sidecar.write_text(
                    json.dumps({"dump_file": dump.name, "restore_verified": True}),
                    encoding="utf-8",
                )
                old_time = base_time + index
                os.utime(dump, (old_time, old_time))
                os.utime(sidecar, (old_time, old_time))
                old_verified.append(dump)
            failed_dump = output_dir / "platformdb-unverified.dump"
            failed_metadata = failed_dump.with_suffix(".json")
            failed_dump.write_bytes(b"old-unverified")
            failed_metadata.write_text(
                json.dumps({"dump_file": failed_dump.name, "restore_verified": False}),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=False,
                rotate_existing=True,
            )

            def fake_run_command(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "pg_dump":
                    pathlib.Path(command[command.index("--file") + 1]).write_bytes(b"new-archive")
                return subprocess.CompletedProcess(command, 0, "", "")

            events: list[str] = []
            real_fsync_directory = backup_drill._fsync_directory
            real_prune_unverified = backup_drill.prune_unverified_backups
            real_prune_backups = backup_drill.prune_backups

            def record_fsync(path: pathlib.Path) -> None:
                events.append("directory-fsync")
                real_fsync_directory(path)

            def record_prune_unverified(*args: object, **kwargs: object) -> list[str]:
                events.append("prune-unverified")
                return real_prune_unverified(*args, **kwargs)

            def record_prune_backups(*args: object, **kwargs: object) -> list[str]:
                events.append("prune-backups")
                return real_prune_backups(*args, **kwargs)

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill, "_run_isolated_restore_drill", return_value=20),
                mock.patch.object(backup_drill, "_fsync_directory", side_effect=record_fsync),
                mock.patch.object(
                    backup_drill, "prune_unverified_backups", side_effect=record_prune_unverified
                ),
                mock.patch.object(backup_drill, "prune_backups", side_effect=record_prune_backups),
            ):
                result = backup_drill.create_backup(args)

            self.assertEqual(result["rotation_mode"], "rotate-existing")
            self.assertEqual(
                events, ["directory-fsync", "prune-unverified", "prune-backups"]
            )
            self.assertEqual(len(result["removed"]), 8)
            self.assertFalse(failed_dump.exists())
            self.assertFalse(failed_metadata.exists())
            self.assertEqual(
                len(tuple(output_dir.glob("platformdb-verified-*.dump"))),
                13,
            )
            self.assertEqual(
                len(tuple(output_dir.glob("platformdb-*.dump"))),
                14,
            )

    @staticmethod
    def _fake_guarded_dump(
        command: list[str], *, target: object, stage: str, disk_paths: tuple[pathlib.Path, ...]
    ) -> None:
        del target, disk_paths
        if stage != "pg_dump":
            raise AssertionError("unexpected guarded command in backup unit test")
        dump_path = pathlib.Path(command[command.index("--file") + 1])
        dump_path.write_bytes(b"PGDMPnew-archive")


if __name__ == "__main__":
    unittest.main()
