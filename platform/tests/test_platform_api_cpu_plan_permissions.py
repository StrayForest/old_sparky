from __future__ import annotations

import errno
import grp
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from tools import platform_cpu_diagnostic_plan as diagnostic_plan_tool


def _diagnostic_payload() -> dict[str, object]:
    return {
        "schema": 1,
        "run_id": "a" * 32,
        "source_sha": "b" * 40,
        "release_slug": "release-bbbbbbbbbbbb",
        "workload": diagnostic_plan_tool.WORKLOAD,
        "off_start_ms": 1_000,
        "off_end_ms": 21_000,
        "on_start_ms": 26_000,
        "on_end_ms": 46_000,
        "expires_at_ms": 46_000,
        "targets": [],
    }


class ApiCpuPlanPermissionTests(unittest.TestCase):
    def test_api_plan_reader_accepts_root_plan_from_traversable_directory_as_service_user(self) -> None:
        self.assertEqual(os.geteuid(), 0, "this contract belongs to the privileged hosted contour")
        api_user = pwd.getpwnam("oldsparky-api")
        api_group = grp.getgrnam("oldsparky-api")
        setpriv = Path("/usr/bin/setpriv")
        self.assertTrue(setpriv.is_file())

        source_module = (
            Path(__file__).resolve().parents[1]
            / "python_packages" / "platform_infra" / "performance_diagnostic.py"
        )
        with tempfile.TemporaryDirectory(prefix="api-plan-opath-") as temporary_dir:
            fixture_root = Path(temporary_dir)
            fixture_root.chmod(0o755)
            release_root = fixture_root / "release" / "current"
            package_root = release_root / "python_packages"
            module_path = package_root / "platform_infra" / "performance_diagnostic.py"
            module_path.parent.mkdir(parents=True)
            for directory in (fixture_root / "release", release_root, package_root, module_path.parent):
                directory.chmod(0o755)
            module_path.write_bytes(source_module.read_bytes())
            source_sha = "b" * 40
            release_slug = "release-bbbbbbbbbbbb"
            (release_root / "RELEASE.json").write_text(
                json.dumps({
                    "source_git_commit": source_sha,
                    "release_slug": release_slug,
                }),
                encoding="ascii",
            )
            # A parent-level decoy makes the fixture sensitive to the real
            # deployed <release>/python_packages layout.
            (release_root.parent / "RELEASE.json").write_text(
                json.dumps({
                    "source_git_commit": "c" * 40,
                    "release_slug": "release-cccccccccccc",
                }),
                encoding="ascii",
            )
            (release_root / "RELEASE.json").chmod(0o644)
            (release_root.parent / "RELEASE.json").chmod(0o644)
            module_path.chmod(0o644)

            plan_directory = fixture_root / "plans"
            invocation_id = "d" * 32
            service_env = {
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG": "C",
                "INVOCATION_ID": invocation_id,
                "PLATFORM_RUNTIME_SERVICE": "api",
            }
            now_ms = time.time_ns() // 1_000_000
            off_start_ms = now_ms + 10_000
            child_code = textwrap.dedent("""
                import json
                import os
                from pathlib import Path
                import sys

                sys.path.insert(0, sys.argv[1])
                from platform_infra import performance_diagnostic as diagnostic

                diagnostic.PLAN_DIRECTORY = Path(sys.argv[2])
                diagnostic.PLAN_PATHS = {
                    "api": Path(sys.argv[2]) / "performance-diagnostic-plan.api.json",
                }
                sys.stdin.readline()
                legacy_errno = None
                try:
                    legacy_fd = os.open(
                        sys.argv[2], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    )
                except OSError as error:
                    legacy_errno = error.errno
                else:
                    os.close(legacy_fd)
                ticks = diagnostic._read_current_start_ticks()
                plan = diagnostic._read_process_plan_uncached(
                    "api", now_ms=int(sys.argv[3]), pid=os.getpid(),
                    start_ticks=ticks, invocation_id=os.environ.get("INVOCATION_ID"),
                    release_identity=None,
                )
                prearm_plan = diagnostic._read_process_plan_for_cpu_prearm(
                    now_ms=int(sys.argv[3]),
                )
                print(json.dumps({
                    "phase_reader_rejected_future": plan is None,
                    "prearm_accepted": prearm_plan is not None,
                    "legacy_errno": legacy_errno,
                    "release_bound": prearm_plan is not None and (
                        prearm_plan.source_sha, prearm_plan.release_slug,
                    ) == (sys.argv[4], sys.argv[5]),
                }, sort_keys=True))
            """)
            child = subprocess.Popen(
                [
                    str(setpriv), "--reuid", str(api_user.pw_uid),
                    "--regid", str(api_group.gr_gid), "--init-groups",
                    sys.executable, "-I", "-B", "-c", child_code,
                    str(package_root), str(plan_directory), str(now_ms),
                    source_sha, release_slug,
                ],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", env=service_env,
            )
            try:
                payload = {
                    **_diagnostic_payload(),
                    "source_sha": source_sha,
                    "release_slug": release_slug,
                    "workload": diagnostic_plan_tool.HTML_CAPACITY_WORKLOAD,
                    "off_start_ms": off_start_ms,
                    "off_end_ms": off_start_ms + 20_000,
                    "on_start_ms": off_start_ms + 25_000,
                    "on_end_ms": off_start_ms + 45_000,
                    "expires_at_ms": off_start_ms + 45_000,
                    "targets": [{
                        "service": "api",
                        "pid": child.pid,
                        "start_ticks": diagnostic_plan_tool._process_start_ticks(child.pid),
                        "invocation_id": invocation_id,
                    }],
                }
                with patch.object(diagnostic_plan_tool, "PLAN_DIRECTORY", plan_directory):
                    directory_fd = diagnostic_plan_tool._validate_plan_directory(create=True)
                    try:
                        diagnostic_plan_tool._publish_plan(
                            directory_fd, diagnostic_plan_tool.PLAN_NAMES["api"],
                            payload, "oldsparky-api",
                        )
                    finally:
                        os.close(directory_fd)
                plan_path = plan_directory / diagnostic_plan_tool.PLAN_NAMES["api"]
                directory_stat = plan_directory.stat()
                self.assertEqual(directory_stat.st_uid, 0)
                self.assertEqual(directory_stat.st_gid, 0)
                self.assertEqual(stat.S_IMODE(directory_stat.st_mode), 0o711)
                plan_stat = plan_path.stat()
                self.assertEqual(plan_stat.st_uid, 0)
                self.assertEqual(plan_stat.st_gid, api_group.gr_gid)
                self.assertEqual(stat.S_IMODE(plan_stat.st_mode), 0o440)
                self.assertEqual(plan_stat.st_nlink, 1)
                assert child.stdin is not None
                child.stdin.write("read\n")
                child.stdin.flush()
                stdout, stderr = child.communicate(timeout=10)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stderr)
            result = json.loads(stdout)
            self.assertEqual(result, {
                "phase_reader_rejected_future": True,
                "prearm_accepted": True,
                "legacy_errno": errno.EACCES,
                "release_bound": True,
            })
