from __future__ import annotations

import io
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path
import tempfile
import textwrap
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from tools import platform_actionlint
from tools.platform_test_catalog import (
    BACKEND_CONTOURS,
    CONTOUR_METADATA,
    CONTOUR_TIMEOUT_SECONDS,
    EXPECTED_SNAPSHOT,
    VERIFICATION_CONTOUR,
    cases_for_contour,
    discover_test_cases,
    test_id_digest,
)
from tools.platform_load import (
    LoadProfileError,
    get_profile,
    load_profiles,
    profile_digest,
    run_profile,
    validate_profile,
)
from tools.platform_verify import (
    CI_GATE_IDS,
    DETERMINISTIC_GATE_IDS,
    GATES_BY_ID,
    VerificationError,
    _verification_contract_commands,
    dispatch,
    registry_payload,
)
from tools.platform_test_runner import (
    TestResourceConfigurationError,
    _integration_preflight_error,
    _require_integration_resources_ready,
    main as test_runner_main,
    validate_test_resource_configuration,
    verify_backend_components,
)
from tools.platform_verification_lock import (
    LOCK_FILE_MODE,
    LOCK_PATH,
    VerificationLockError,
    default_lock_path,
    provision_verification_lock,
    verification_resource_lock,
)
from tools.platform_verify_contract import (
    ALLOWED_ACTION_OWNERS,
    ACTIONLINT_CHECKSUM_FIXTURE,
    ACTIONLINT_TOOL,
    DRAFT_CLOUDFLARE_WORKFLOW,
    SECURITY_WORKFLOW,
    action_pin_issues,
    actionlint_release_fixture_issues,
    actionlint_tool_contract_issues,
    actionlint_workflow_issues,
    collect_issues,
    _ci_dependency_issues,
    draft_cloudflare_workflow_issues,
    extract_gate_invocations,
    host_tools_pin_verification_issues,
    release_runtime_workflow_issues,
    security_status_permission_issues,
    workflow_level_permission_issues,
)


# Independent release fixtures: these values are copied from the upstream
# v1.7.12 tarball/checksum asset rather than imported from the installer.
_EXPECTED_ACTIONLINT_ARCHIVE_MODES = {
    "LICENSE.txt": 0o644,
    "README.md": 0o644,
    "docs/README.md": 0o644,
    "docs/api.md": 0o644,
    "docs/checks.md": 0o644,
    "docs/config.md": 0o644,
    "docs/install.md": 0o644,
    "docs/reference.md": 0o644,
    "docs/usage.md": 0o644,
    "man/actionlint.1": 0o644,
    "actionlint": 0o755,
}
_EXPECTED_ACTIONLINT_ASSETS = {
    ("linux", "amd64"): (
        "actionlint_1.7.12_linux_amd64.tar.gz",
        "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
    ),
    ("linux", "arm64"): (
        "actionlint_1.7.12_linux_arm64.tar.gz",
        "325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6",
    ),
    ("darwin", "amd64"): (
        "actionlint_1.7.12_darwin_amd64.tar.gz",
        "5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644",
    ),
    ("darwin", "arm64"): (
        "actionlint_1.7.12_darwin_arm64.tar.gz",
        "aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f",
    ),
}
_EXPECTED_ACTIONLINT_CHECKSUM_FIXTURE_SHA256 = (
    "433028cf0ba3c42163ea1a668dedce30fcdbe84fe912b1a5e288c006eab8a4f5"
)
_EXPECTED_ACTIONLINT_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024


def _write_backend_component_fixture(root: Path) -> None:
    """Write complete synthetic component evidence for aggregate tamper tests."""

    all_cases = discover_test_cases()
    for contour in BACKEND_CONTOURS:
        cases = sorted(
            cases_for_contour(contour, all_cases),
            key=lambda case: case.test_id,
        )
        expected_ids = [case.test_id for case in cases]
        component_root = root / contour
        component_root.mkdir(parents=True, exist_ok=True)
        (component_root / "manifest.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "contour": contour,
                    "aggregate": False,
                    "serial": bool(CONTOUR_METADATA[contour]["serial_resources"]),
                    "tests": [
                        {
                            "id": case.test_id,
                            "module": case.module,
                            "class": case.class_name,
                            "method": case.method_name,
                            "line": case.line,
                            "async": case.is_async,
                            "owner": case.contour,
                        }
                        for case in cases
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (component_root / "summary.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "contour": contour,
                    "status": "passed",
                    "tests_selected": len(expected_ids),
                    "tests_run": len(expected_ids),
                    "failures": 0,
                    "errors": 0,
                    "expected_failures": 0,
                    "unexpected_successes": 0,
                    "skipped": [],
                    "skipped_count": 0,
                    "skip_reasons": {},
                    "expected_ids": expected_ids,
                    "executed_ids": expected_ids,
                    "missing_ids": [],
                    "duplicate_ids": [],
                    "unexpected_ids": [],
                    "unexpected_skips": [],
                    "execution_complete": True,
                    "elapsed_ms": float(len(expected_ids)),
                    "timeout_seconds": CONTOUR_TIMEOUT_SECONDS[contour],
                    "test_id_digest": test_id_digest(expected_ids),
                    "timings": [
                        {
                            "test_id": test_id,
                            "duration_ms": 1.0,
                            "outcome": "passed",
                        }
                        for test_id in expected_ids
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


def _draft_current_dev_parser_script() -> str:
    workflow = yaml.safe_load(
        DRAFT_CLOUDFLARE_WORKFLOW.read_text(encoding="utf-8")
    )
    resolver = workflow["jobs"]["detect-release"]["steps"][0]
    run = resolver["run"]
    marker = (
        "/usr/bin/python3 - \"$current_dev_ref\" \"$target_sha\" "
        '\"$CANONICAL_REPOSITORY\" <<\'PY\'\n'
    )
    return textwrap.dedent(run.split(marker, 1)[1].split("\nPY", 1)[0])


def _write_actionlint_archive(path: Path, mutation: str | None = None) -> bytes:
    """Build a tiny offline archive from independent upstream fixtures."""

    members = sorted(_EXPECTED_ACTIONLINT_ARCHIVE_MODES)
    with tarfile.open(path, "w:gz") as archive:
        for name in members:
            if mutation == "subset" and name == "docs/api.md":
                continue
            payload = (
                b"fake actionlint binary\n"
                if name == "actionlint"
                else f"fixture:{name}\n".encode("utf-8")
            )
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = _EXPECTED_ACTIONLINT_ARCHIVE_MODES[name]
            if mutation == "traversal" and name == "README.md":
                member.name = "../README.md"
            elif mutation == "symlink" and name == "actionlint":
                member.type = tarfile.SYMTYPE
                member.linkname = "README.md"
                member.size = 0
                payload = b""
            elif mutation == "setuid" and name == "actionlint":
                member.mode = 0o4755
            elif mutation == "mode" and name == "README.md":
                member.mode = 0o664
            elif mutation == "mode-0600" and name == "docs/api.md":
                member.mode = 0o600
            elif mutation == "binary-mode" and name == "actionlint":
                member.mode = 0o754
            archive.addfile(member, io.BytesIO(payload))
        if mutation == "duplicate":
            duplicate = tarfile.TarInfo("actionlint")
            duplicate.size = 1
            duplicate.mode = 0o755
            archive.addfile(duplicate, io.BytesIO(b"x"))
        if mutation == "evil":
            evil = tarfile.TarInfo("evil.txt")
            evil.size = 1
            evil.mode = 0o644
            archive.addfile(evil, io.BytesIO(b"x"))
    return path.read_bytes()


class PlatformVerificationContractTests(unittest.TestCase):
    @staticmethod
    def _test_settings(**overrides: object) -> SimpleNamespace:
        values: dict[str, object] = {
            "platform_environment": "test",
            "platform_db_schema": "platform",
            "platform_database_url": (
                "postgresql+asyncpg://platform_user:platform_password@127.0.0.1:5432/"
                "platformdb_test"
            ),
            "platform_redis_url": "redis://127.0.0.1:6379/15",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_test_resource_validator_rejects_ambiguous_targets(self) -> None:
        safe = validate_test_resource_configuration(self._test_settings())
        self.assertEqual(safe.database_host, "127.0.0.1")
        self.assertEqual(safe.database_name, "platformdb_test")
        self.assertEqual(safe.database_schema, "platform")
        self.assertEqual(safe.redis_host, "127.0.0.1")
        self.assertEqual(safe.redis_database, "15")

        invalid_settings = (
            {"platform_environment": "Test"},
            {"platform_db_schema": "public"},
            {"platform_database_url": "postgresql://u:p@localhost:5432/platformdb_test"},
            {"platform_database_url": "postgresql://u:p@192.0.2.10:5432/platformdb_test"},
            {
                "platform_database_url": (
                    "postgresql://u:p@127.0.0.1:5432/platformdb_test?dbname=platformdb_test"
                )
            },
            {
                "platform_database_url": (
                    "postgresql://u:p@127.0.0.1,127.0.0.1:5432/platformdb_test"
                )
            },
            {"platform_redis_url": "redis://localhost:6379/15"},
            {"platform_redis_url": "redis://127.0.0.1:6379/0"},
            {"platform_redis_url": "redis://127.0.0.1:6379/15?db=0"},
            {"platform_redis_url": None},
        )
        for overrides in invalid_settings:
            with self.subTest(overrides=overrides):
                with self.assertRaises(TestResourceConfigurationError):
                    validate_test_resource_configuration(self._test_settings(**overrides))

    def test_invalid_resource_config_fails_before_client_imports(self) -> None:
        unsafe_environment = {
            "PLATFORM_ENVIRONMENT": "test",
            "PLATFORM_DB_SCHEMA": "platform",
            "PLATFORM_DATABASE_URL": "postgresql+asyncpg://u:p@remote.invalid:5432/platformdb_test",
            "PLATFORM_REDIS_URL": "redis://127.0.0.1:6379/15",
        }
        imported: list[str] = []
        real_import = __import__

        def recording_import(name: str, *args: object, **kwargs: object) -> object:
            imported.append(name)
            return real_import(name, *args, **kwargs)

        with (
            patch.dict(os.environ, unsafe_environment, clear=True),
            patch("builtins.__import__", side_effect=recording_import),
        ):
            with self.assertRaises(SystemExit):
                _require_integration_resources_ready()
        self.assertFalse(any(name == "sqlalchemy" or name.startswith("redis") for name in imported))

    def test_db_free_contour_validates_without_resource_calls(self) -> None:
        safe_environment = {
            "PLATFORM_ENVIRONMENT": "test",
            "PLATFORM_DB_SCHEMA": "platform",
            "PLATFORM_DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:5432/platformdb_test",
            "PLATFORM_REDIS_URL": "redis://127.0.0.1:6379/15",
        }
        with (
            patch.dict(os.environ, safe_environment, clear=True),
            patch("tools.platform_test_runner._require_integration_resources_ready") as preflight,
            patch("tools.platform_test_runner._teardown_test_resources") as teardown,
            patch("builtins.print"),
        ):
            self.assertEqual(
                test_runner_main(["--contour", "backend-unit", "--list"]),
                0,
            )
        preflight.assert_not_called()
        teardown.assert_not_called()

    def test_privileged_contour_blocks_non_root_before_loading_tests_or_resources(self) -> None:
        """A non-root privileged launch must fail before unittest/resource imports."""

        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Keep this probe runnable when the test itself is root-owned (for
            # example in a local container): the non-root child must be able to
            # traverse an accessible synthetic checkout, while the catalog
            # still sees the complete AST test inventory.
            synthetic_platform = root / "platform"
            synthetic_tools = synthetic_platform / "tools"
            synthetic_tests = synthetic_platform / "tests"
            synthetic_tools.mkdir(parents=True)
            synthetic_tests.mkdir()
            for name in (
                "platform_test_catalog.py",
                "platform_test_runner.py",
                "platform_verification_lock.py",
            ):
                destination = synthetic_tools / name
                shutil.copy2(repo_root / "platform/tools" / name, destination)
                destination.chmod(0o755)
            for source in (repo_root / "platform/tests").glob("test_*.py"):
                destination = synthetic_tests / source.name
                shutil.copy2(source, destination)
                destination.chmod(0o644)
            root.chmod(0o755)
            synthetic_platform.chmod(0o755)
            synthetic_tools.chmod(0o755)
            synthetic_tests.chmod(0o755)

            marker_dir = root / "markers"
            marker_dir.mkdir(mode=0o733)
            marker = marker_dir / "imported"
            probe = root / "probe.py"
            runner = synthetic_tools / "platform_test_runner.py"
            probe.write_text(
                "import builtins\n"
                "from pathlib import Path\n"
                "import runpy\n"
                "import sys\n"
                f"marker = Path({str(marker)!r})\n"
                "real_import = builtins.__import__\n"
                "def record_import(name, *args, **kwargs):\n"
                "    if name == 'tests' or name.startswith('tests.') or name.split('.')[0] in {'sqlalchemy', 'redis', 'asyncpg', 'psycopg'}:\n"
                "        marker.write_text(name, encoding='utf-8')\n"
                "    return real_import(name, *args, **kwargs)\n"
                "builtins.__import__ = record_import\n"
                "import tools.platform_verification_lock as verification_lock\n"
                f"verification_lock.LOCK_PATH = Path({str(root / 'missing-global.lock')!r})\n"
                f"sys.argv = [{str(runner)!r}, '--contour', 'backend-privileged', '--quiet']\n"
                "runpy.run_path(str(sys.argv[0]), run_name='__main__')\n",
                encoding="utf-8",
            )

            environment = {
                **os.environ,
                "PLATFORM_ENVIRONMENT": "test",
                "PLATFORM_DB_SCHEMA": "platform",
                "PLATFORM_DATABASE_URL": (
                    "postgresql+asyncpg://u:p@127.0.0.1:5432/platformdb_test"
                ),
                "PLATFORM_REDIS_URL": "redis://127.0.0.1:6379/15",
                "PYTHONPATH": os.pathsep.join((str(root), str(synthetic_platform))),
                "XDG_RUNTIME_DIR": "",
            }
            if os.geteuid() == 0:
                launcher = [
                    shutil.which("runuser") or "/usr/bin/runuser",
                    "-u",
                    "nobody",
                    "--",
                    "/usr/bin/python3",
                    str(probe),
                ]
            else:
                launcher = [sys.executable, str(probe)]
            blocked = subprocess.run(
                launcher,
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
            self.assertIn("backend-privileged requires the root test user", blocked.stderr)
            self.assertFalse(
                marker.exists(),
                "non-root preflight imported tests/resources: "
                + (marker.read_text() if marker.exists() else "<unknown>"),
            )
            for contour in ("backend", "backend-integration", "backend-privileged"):
                with self.subTest(contour=contour):
                    with (
                        patch("tools.platform_test_runner.os.geteuid", return_value=1234),
                        patch("tools.platform_test_runner._require_test_environment") as validator,
                        patch("tools.platform_test_runner.verification_resource_lock") as lock,
                    ):
                        with self.assertRaisesRegex(
                            SystemExit,
                            rf"LOCAL GATE BLOCKED: {contour} requires the root test user",
                        ):
                            test_runner_main(["--contour", contour, "--list"])
                    validator.assert_not_called()
                    lock.assert_not_called()

    def test_aggregate_only_workflow_env_reaches_runner_boundary(self) -> None:
        """The aggregate job must provide every value the pure validator requires."""

        repo_root = Path(__file__).resolve().parents[2]
        workflow = (repo_root / ".github/workflows/platform-security.yml").read_text(
            encoding="utf-8"
        )
        aggregate_block = re.search(
            r"^  backend:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            workflow,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(aggregate_block)
        assert aggregate_block is not None
        aggregate_env = {
            name: value.strip().strip('"')
            for name, value in re.findall(
                r"^      (PLATFORM_(?:ENVIRONMENT|DATABASE_URL|DB_SCHEMA|REDIS_URL)):\s*(.+)$",
                aggregate_block.group("body"),
                re.MULTILINE,
            )
        }
        self.assertEqual(
            aggregate_env,
            {
                "PLATFORM_ENVIRONMENT": "test",
                "PLATFORM_DATABASE_URL": (
                    "postgresql+asyncpg://platform_user:platform_password@127.0.0.1:5432/"
                    "platformdb_test"
                ),
                "PLATFORM_DB_SCHEMA": "platform",
                "PLATFORM_REDIS_URL": "redis://127.0.0.1:6379/15",
            },
        )

        runner = repo_root / "platform/tools/platform_run_tests.sh"
        with tempfile.TemporaryDirectory() as directory:
            missing_env_file = str(Path(directory) / "missing.env")
            base_environment = os.environ.copy()
            base_environment.update(
                {
                    **aggregate_env,
                    "PLATFORM_ENV_FILE": missing_env_file,
                    "PLATFORM_PYTHON_BIN": "/usr/bin/python3",
                    "PLATFORM_TEST_AGGREGATE_ONLY": "1",
                }
            )
            command = [
                str(runner),
                "--contour",
                "backend",
                "--component-dir",
                str(Path(directory) / "missing-components"),
            ]
            valid = subprocess.run(
                command,
                cwd=repo_root / "platform",
                env=base_environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            self.assertNotEqual(valid.returncode, 0)
            self.assertIn("backend component result directory is unavailable", valid.stderr)

            for missing_name in ("PLATFORM_DB_SCHEMA", "PLATFORM_REDIS_URL"):
                with self.subTest(missing_name=missing_name):
                    incomplete_environment = dict(base_environment)
                    incomplete_environment.pop(missing_name)
                    blocked = subprocess.run(
                        command,
                        cwd=repo_root / "platform",
                        env=incomplete_environment,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=20,
                    )
                    self.assertNotEqual(blocked.returncode, 0)
                    self.assertIn("LOCAL GATE BLOCKED", blocked.stderr)
                    self.assertIn(missing_name, blocked.stderr)

    def test_shared_resource_lock_excludes_migration_and_integration(self) -> None:
        """A local migration cannot reset resources during integration tests."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            lock_path = root / "platformdb-test.lock"
            lock_path.touch(mode=LOCK_FILE_MODE)
            if os.geteuid() != 0:
                # A non-root test process cannot manufacture the root-owned
                # production identity.  Prove the validator fails on owner
                # before attempting to exercise flock instead of weakening it.
                with patch("tools.platform_verification_lock.LOCK_PATH", lock_path):
                    with self.assertRaisesRegex(VerificationLockError, "wrong owner"):
                        with verification_resource_lock("backend-integration"):
                            pass
                return
            holder_code = """
from pathlib import Path
import sys
import tools.platform_verification_lock as lock
lock.LOCK_PATH = Path(sys.argv[1])
with lock.verification_resource_lock("backend-integration"):
    print("ready", flush=True)
    sys.stdin.readline()
"""
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_code, str(lock_path)],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(holder.stdout.readline().strip(), "ready")
            contender_code = """
from pathlib import Path
import sys
import tools.platform_verification_lock as lock
lock.LOCK_PATH = Path(sys.argv[1])
try:
    with lock.verification_resource_lock("migration"):
        raise SystemExit("migration unexpectedly entered integration contour")
except lock.VerificationLockError as exc:
    print(exc)
    raise SystemExit(75)
"""
            contender = subprocess.run(
                [sys.executable, "-c", contender_code, str(lock_path)],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(contender.returncode, 75, contender.stderr)
            self.assertIn("contention", contender.stdout)
            assert holder.stdin is not None
            holder.stdin.write("release\n")
            holder.stdin.close()
            holder.wait(timeout=5)
            stdout = holder.stdout.read()
            stderr = holder.stderr.read()
            holder.stdout.close()
            holder.stderr.close()
            self.assertEqual(holder.returncode, 0, stderr or stdout)
            self.assertEqual(lock_path.read_text(encoding="utf-8"), "")

    def test_shared_resource_lock_rejects_wrong_mode_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            lock_path = root / "platformdb-test.lock"
            lock_path.touch(mode=0o600)
            if os.geteuid() != 0:
                with patch("tools.platform_verification_lock.LOCK_PATH", lock_path):
                    with self.assertRaisesRegex(VerificationLockError, "wrong owner"):
                        with verification_resource_lock("migration"):
                            pass
                return
            with self.assertRaisesRegex(VerificationLockError, "mode is unsafe"):
                with patch("tools.platform_verification_lock.LOCK_PATH", lock_path):
                    with verification_resource_lock("migration"):
                        pass
            lock_path.chmod(LOCK_FILE_MODE)
            link = root / "unsafe.lock"
            link.symlink_to(lock_path)
            with patch("tools.platform_verification_lock.LOCK_PATH", link):
                with self.assertRaises(VerificationLockError):
                    with verification_resource_lock("migration"):
                        pass

    def test_global_lock_serializes_root_and_non_root_on_same_inode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            lock_path = root / "platformdb-test.lock"
            lock_path.touch(mode=LOCK_FILE_MODE)
            if os.geteuid() != 0:
                with patch("tools.platform_verification_lock.LOCK_PATH", lock_path):
                    with self.assertRaisesRegex(VerificationLockError, "wrong owner"):
                        with verification_resource_lock("backend-integration"):
                            pass
                return
            if shutil.which("runuser") is None:
                with patch("tools.platform_verification_lock.LOCK_PATH", lock_path):
                    with verification_resource_lock("backend-integration"):
                        pass
                return
            module_dir = root / "module"
            module_dir.mkdir(mode=0o755)
            package_dir = module_dir / "tools"
            package_dir.mkdir(mode=0o755)
            (package_dir / "__init__.py").write_text("", encoding="utf-8")
            (package_dir / "platform_verification_lock.py").write_bytes(
                Path(__file__).resolve().parents[1].joinpath(
                    "tools/platform_verification_lock.py"
                ).read_bytes()
            )
            holder_code = """
from pathlib import Path
import sys
import tools.platform_verification_lock as lock
lock.LOCK_PATH = Path(sys.argv[1])
with lock.verification_resource_lock("backend-integration"):
    info = lock.os.stat(lock.LOCK_PATH, follow_symlinks=False)
    print(f"ready:{info.st_dev}:{info.st_ino}", flush=True)
    sys.stdin.readline()
"""
            holder = subprocess.Popen(
                [
                    shutil.which("runuser") or "/usr/bin/runuser",
                    "-u",
                    "nobody",
                    "--",
                    "/usr/bin/python3",
                    "-c",
                    holder_code,
                    str(lock_path),
                ],
                cwd=module_dir,
                env={"PYTHONPATH": str(module_dir), "PATH": os.environ.get("PATH", "")},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert holder.stdout is not None
            ready = holder.stdout.readline().strip()
            self.assertTrue(ready.startswith("ready:"), ready)
            _tag, holder_device, holder_inode = ready.split(":")
            contender_code = """
from pathlib import Path
import sys
import tools.platform_verification_lock as lock
lock.LOCK_PATH = Path(sys.argv[1])
try:
    with lock.verification_resource_lock("migration"):
        raise SystemExit("root unexpectedly entered non-root contour")
except lock.VerificationLockError as exc:
    print(exc)
    raise SystemExit(75)
"""
            contender = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    contender_code,
                    str(lock_path),
                ],
                cwd=module_dir,
                env={"PYTHONPATH": str(module_dir), "PATH": os.environ.get("PATH", "")},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(contender.returncode, 75, contender.stderr)
            self.assertIn("contention", contender.stdout)
            assert holder.stdin is not None
            holder.stdin.write("release\n")
            holder.stdin.close()
            holder.wait(timeout=5)
            holder_stderr = holder.stderr.read()
            holder.stdout.close()
            holder.stderr.close()
            self.assertEqual(holder.returncode, 0, holder_stderr)
            current = os.stat(lock_path)
            self.assertEqual(int(holder_device), current.st_dev)
            self.assertEqual(int(holder_inode), current.st_ino)

    def test_global_lock_fails_closed_on_missing_or_unsafe_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            missing = root / "missing" / "platformdb-test.lock"
            if os.geteuid() != 0:
                parent = root / "non-root-parent"
                parent.mkdir(mode=0o755)
                lock_path = parent / "platformdb-test.lock"
                lock_path.touch(mode=LOCK_FILE_MODE)
                with patch("tools.platform_verification_lock.LOCK_PATH", lock_path):
                    with self.assertRaisesRegex(VerificationLockError, "wrong owner"):
                        with verification_resource_lock("migration"):
                            pass
                return
            with patch("tools.platform_verification_lock.LOCK_PATH", missing):
                with self.assertRaisesRegex(VerificationLockError, "LOCAL GATE BLOCKED"):
                    with verification_resource_lock("migration"):
                        pass

            parent = root / "secure-parent"
            parent.mkdir(mode=0o755)
            lock_path = parent / "platformdb-test.lock"
            lock_path.touch(mode=LOCK_FILE_MODE)
            parent.chmod(0o775)
            with patch("tools.platform_verification_lock.LOCK_PATH", lock_path):
                with self.assertRaisesRegex(VerificationLockError, "writable"):
                    with verification_resource_lock("migration"):
                        pass
            parent.chmod(0o755)
            if os.geteuid() == 0:
                os.chown(parent, 65534, 65534)
                with patch("tools.platform_verification_lock.LOCK_PATH", lock_path):
                    with self.assertRaisesRegex(VerificationLockError, "wrong owner"):
                        with verification_resource_lock("migration"):
                            pass
                os.chown(parent, 0, 0)
            link = root / "parent-link"
            link.symlink_to(parent, target_is_directory=True)
            linked_lock = link / "platformdb-test.lock"
            with patch("tools.platform_verification_lock.LOCK_PATH", linked_lock):
                with self.assertRaisesRegex(VerificationLockError, "not a directory"):
                    with verification_resource_lock("migration"):
                        pass

            # A parent replacement between the path check and open is a
            # security failure even when the leaf itself still looks valid.
            stable = root / "stable"
            stable.mkdir(mode=0o755)
            stable_lock = stable / "platformdb-test.lock"
            stable_lock.touch(mode=LOCK_FILE_MODE)
            with (
                patch("tools.platform_verification_lock.LOCK_PATH", stable_lock),
                patch(
                    "tools.platform_verification_lock._parent_chain",
                    side_effect=[((stable, 1, 2),), ((stable, 1, 3),)],
                ),
            ):
                with self.assertRaisesRegex(VerificationLockError, "identity changed"):
                    with verification_resource_lock("migration"):
                        pass

    def test_verification_lock_is_fixed_and_ignores_sudo_inheritance(self) -> None:
        with patch.dict(
            os.environ,
            {
                "XDG_RUNTIME_DIR": "/runner-controlled/runtime",
                "RUNNER_TEMP": "/runner-controlled/temp",
                "PLATFORM_VERIFICATION_RUNTIME_DIR": "/runner-controlled/override",
            },
            clear=False,
        ):
            self.assertEqual(default_lock_path(), LOCK_PATH)
            self.assertEqual(default_lock_path().parent, Path("/run/lock/oldsparky-platform-verification"))

    def test_root_provisioning_has_explicit_identity_and_mode(self) -> None:
        if os.geteuid() != 0:
            with self.assertRaisesRegex(VerificationLockError, "requires root"):
                provision_verification_lock()
            return
        path = provision_verification_lock()
        info = os.stat(path, follow_symlinks=False)
        parent = os.stat(path.parent, follow_symlinks=False)
        self.assertEqual(info.st_uid, 0)
        self.assertEqual(info.st_nlink, 1)
        self.assertEqual(info.st_mode & 0o777, LOCK_FILE_MODE)
        self.assertEqual(parent.st_uid, 0)
        self.assertEqual(parent.st_mode & 0o777, 0o755)

    def test_ci_root_contours_provision_effective_user_runtime_lock_root(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[2] / ".github/workflows/platform-security.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(workflow.count("name: Provision fixed root-owned global verification lock"), 4)
        self.assertEqual(workflow.count("platform/tools/platform_verification_lock.py\" --provision"), 4)
        self.assertNotIn("RUNNER_TEMP/platform-verification-runtime", workflow)
        self.assertNotIn("PLATFORM_VERIFICATION_RUNTIME_DIR", workflow)
        self.assertNotIn("sudo install -d", workflow)
        self.assertEqual(workflow.count("sudo -EH env XDG_RUNTIME_DIR= bash -lc"), 4)
        verification_block = re.search(
            r"^  verification-contract:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            workflow,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(verification_block)
        assert verification_block is not None
        self.assertNotIn("Provision fixed root-owned global verification lock", verification_block.group("body"))

    def test_registry_exposes_deterministic_and_workflow_only_contours(self) -> None:
        conditional = {
            gate_id
            for gate_id in DETERMINISTIC_GATE_IDS
            if GATES_BY_ID[gate_id].conditional
        }
        self.assertEqual(set(CI_GATE_IDS), set(DETERMINISTIC_GATE_IDS) - conditional)
        self.assertIn("backend", CI_GATE_IDS)
        self.assertIn("verification-contract", CI_GATE_IDS)
        self.assertTrue(GATES_BY_ID["release-runtime"].conditional)
        self.assertFalse(GATES_BY_ID["release-runtime"].ci_required)
        self.assertFalse(GATES_BY_ID["external-load"].deterministic)
        self.assertFalse(GATES_BY_ID["external-load"].local_safe)
        self.assertEqual(registry_payload()["ci_gate_ids"], list(CI_GATE_IDS))

        verification_cases = cases_for_contour(
            VERIFICATION_CONTOUR,
            discover_test_cases(),
        )
        self.assertEqual(
            sum(case.module == "test_platform_ci_classifier" for case in verification_cases),
            int(EXPECTED_SNAPSHOT["verification_classifier_test_count"]),
        )
        with patch("tools.platform_verify._run", return_value=0) as run:
            self.assertEqual(dispatch(VERIFICATION_CONTOUR), 0)

        self.assertIsNone(
            _integration_preflight_error(
                migration_heads=["test-head"],
                role_slugs={
                    "authenticated_user",
                    "player",
                    "organizer",
                    "moderator",
                    "editor",
                    "admin",
                    "superadmin",
                },
                expected_head="test-head",
            )
        )
        missing_seed = _integration_preflight_error(
            migration_heads=["test-head"],
            role_slugs={"admin"},
            expected_head="test-head",
        )
        self.assertIsNotNone(missing_seed)
        self.assertIn("missing required seed roles", str(missing_seed))
        self.assertIn("authenticated_user", str(missing_seed))
        self.assertEqual(
            [call.args[1] for call in run.call_args_list],
            list(_verification_contract_commands()),
        )

    def test_production_contours_are_not_dispatchable_as_local_gates(self) -> None:
        with self.assertRaises(VerificationError):
            dispatch("external-load")

    def test_backend_and_unknown_workflow_gate_extraction(self) -> None:
        text = """
          run: python3 platform/tools/platform_verify.py backend
          run: python3 platform/tools/platform_verify.py no-such-gate
        """
        self.assertEqual(
            extract_gate_invocations(text),
            ["backend", "no-such-gate"],
        )

    def test_actionlint_pinned_installer_and_workflow_contract(self) -> None:
        self.assertEqual(actionlint_tool_contract_issues(), [])
        self.assertEqual(actionlint_release_fixture_issues(), [])
        self.assertEqual(ACTIONLINT_CHECKSUM_FIXTURE.name, "actionlint_1.7.12_checksums.txt")
        self.assertEqual(
            {
                key: (asset.filename, asset.sha256)
                for key, asset in platform_actionlint.ACTIONLINT_ASSETS.items()
            },
            _EXPECTED_ACTIONLINT_ASSETS,
        )
        self.assertEqual(
            platform_actionlint.ACTIONLINT_ARCHIVE_MODES,
            _EXPECTED_ACTIONLINT_ARCHIVE_MODES,
        )
        self.assertEqual(
            hashlib.sha256(ACTIONLINT_CHECKSUM_FIXTURE.read_bytes()).hexdigest(),
            _EXPECTED_ACTIONLINT_CHECKSUM_FIXTURE_SHA256,
        )

        supported = {
            ("Linux", "x86_64"): "actionlint_1.7.12_linux_amd64.tar.gz",
            ("Linux", "aarch64"): "actionlint_1.7.12_linux_arm64.tar.gz",
            ("Darwin", "x86_64"): "actionlint_1.7.12_darwin_amd64.tar.gz",
            ("Darwin", "arm64"): "actionlint_1.7.12_darwin_arm64.tar.gz",
        }
        for platform_key, expected_filename in supported.items():
            with self.subTest(platform=platform_key):
                self.assertEqual(
                    platform_actionlint.select_asset(*platform_key).filename,
                    expected_filename,
                )
        for platform_key in (("Windows", "amd64"), ("Linux", "armv7"), ("FreeBSD", "amd64")):
            with self.subTest(unsupported=platform_key):
                with self.assertRaises(platform_actionlint.ActionlintError):
                    platform_actionlint.select_asset(*platform_key)

        asset = platform_actionlint.select_asset("Linux", "x86_64")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / asset.filename
            with (
                patch.object(platform_actionlint.shutil, "which", return_value="/usr/bin/curl"),
                patch.object(
                    platform_actionlint.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0),
                ) as download,
            ):
                self.assertEqual(
                    platform_actionlint._download_archive(asset, destination),
                    destination,
                )
            download_command = download.call_args.args[0]
            self.assertEqual(download_command[0], "/usr/bin/curl")
            self.assertEqual(download_command[-1], asset.url)
            self.assertEqual(_EXPECTED_ACTIONLINT_MAX_ARCHIVE_BYTES, 8388608)
            self.assertEqual(download_command.count("--max-filesize"), 1)
            max_filesize_index = download_command.index("--max-filesize")
            self.assertLess(max_filesize_index + 1, len(download_command) - 1)
            self.assertEqual(
                download_command[max_filesize_index : max_filesize_index + 2],
                ["--max-filesize", "8388608"],
            )
            for flag in (
                ["--fail"],
                ["--location"],
                ["--connect-timeout", "5"],
                ["--max-time", "60"],
                ["--retry", "0"],
                ["--output", str(destination)],
            ):
                with self.subTest(curl_flag=flag):
                    start = download_command.index(flag[0])
                    self.assertEqual(download_command[start : start + len(flag)], flag)

        wrapper_source = ACTIONLINT_TOOL.read_text(encoding="utf-8")
        tool_mutations = {
            "implementation-one-gib": wrapper_source.replace(
                "MAX_ARCHIVE_BYTES = 8 * 1024 * 1024",
                "MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024",
                1,
            ),
            "curl-one-gib": wrapper_source.replace(
                "str(MAX_ARCHIVE_BYTES)",
                "str(1024 * 1024 * 1024)",
                1,
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            for mutation, mutated_source in tool_mutations.items():
                with self.subTest(actionlint_size_mutation=mutation):
                    mutated_tool = Path(directory) / f"{mutation}.py"
                    mutated_tool.write_text(mutated_source, encoding="utf-8")
                    with patch("tools.platform_verify_contract.ACTIONLINT_TOOL", mutated_tool):
                        self.assertTrue(actionlint_tool_contract_issues())

        workflow = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(actionlint_workflow_issues(workflow), [])
        missing_invocation = workflow.replace(
            "          .venv_platform/bin/python tools/platform_actionlint.py\n",
            "",
            1,
        )
        self.assertTrue(
            any("invoke platform_actionlint.py exactly once" in issue
                for issue in actionlint_workflow_issues(missing_invocation))
        )
        separate_job = workflow + "\n  actionlint:\n    runs-on: ubuntu-latest\n"
        self.assertTrue(
            any("separate CI job" in issue for issue in actionlint_workflow_issues(separate_job))
        )

        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "fixture.tar.gz"
            archive_bytes = _write_actionlint_archive(archive_path)
            fixture_asset = platform_actionlint.ActionlintAsset(
                "linux",
                "amd64",
                archive_path.name,
                hashlib.sha256(archive_bytes).hexdigest(),
            )
            binary_path = Path(directory) / "actionlint"
            extracted = platform_actionlint.extract_verified_binary(
                archive_path,
                fixture_asset,
                binary_path,
            )
            self.assertEqual(extracted.read_bytes(), b"fake actionlint binary\n")
            self.assertTrue(extracted.stat().st_mode & stat.S_IXUSR)
            wrong_digest = platform_actionlint.ActionlintAsset(
                "linux", "amd64", archive_path.name, "0" * 64
            )
            with self.assertRaisesRegex(platform_actionlint.ActionlintError, "digest mismatch"):
                platform_actionlint.extract_verified_binary(
                    archive_path,
                    wrong_digest,
                    Path(directory) / "wrong",
                )
            self.assertFalse((Path(directory) / "wrong").exists())
            oversized_path = Path(directory) / "oversized.tar.gz"
            with oversized_path.open("wb") as stream:
                stream.truncate(_EXPECTED_ACTIONLINT_MAX_ARCHIVE_BYTES + 1)
            with self.assertRaisesRegex(platform_actionlint.ActionlintError, "bounded"):
                platform_actionlint.verify_archive_digest(oversized_path, fixture_asset)
            for mutation in (
                "traversal",
                "symlink",
                "setuid",
                "duplicate",
                "evil",
                "subset",
                "mode",
                "mode-0600",
                "binary-mode",
            ):
                with self.subTest(archive_mutation=mutation):
                    mutated_path = Path(directory) / f"{mutation}.tar.gz"
                    mutated_bytes = _write_actionlint_archive(mutated_path, mutation)
                    mutated_asset = platform_actionlint.ActionlintAsset(
                        "linux",
                        "amd64",
                        mutated_path.name,
                        hashlib.sha256(mutated_bytes).hexdigest(),
                    )
                    with self.assertRaises(platform_actionlint.ActionlintError):
                        platform_actionlint.extract_verified_binary(
                            mutated_path,
                            mutated_asset,
                            Path(directory) / f"{mutation}-binary",
                        )
                    self.assertFalse((Path(directory) / f"{mutation}-binary").exists())

        repository_root = Path(__file__).resolve().parents[2]
        independent_listing = subprocess.run(
            ["git", "-C", str(repository_root), "ls-files", "-z", "--", ".github/workflows"],
            check=True,
            capture_output=True,
        )
        expected_workflows = {
            Path(raw.decode("utf-8"))
            for raw in independent_listing.stdout.split(b"\0")
            if raw and Path(raw.decode("utf-8")).suffix.lower() in {".yml", ".yaml"}
        }
        tracked = platform_actionlint.tracked_workflow_paths(repository_root)
        tracked_relative = tuple(path.relative_to(repository_root) for path in tracked)
        self.assertEqual(set(tracked_relative), expected_workflows)
        self.assertEqual(len(tracked_relative), len(set(tracked_relative)))
        self.assertEqual(tracked_relative, tuple(sorted(tracked_relative)))
        self.assertEqual(len(tracked_relative), 29)

        def assert_exact_workflow_listing(candidate: tuple[Path, ...]) -> None:
            candidate_relative = tuple(path.relative_to(repository_root) for path in candidate)
            self.assertEqual(len(candidate_relative), len(set(candidate_relative)))
            self.assertEqual(set(candidate_relative), expected_workflows)
            self.assertEqual(candidate_relative, tuple(sorted(candidate_relative)))

        mutation_cases = {
            "duplicate": tracked + (tracked[0],),
            "subset": tracked[:-1],
            "addition": tracked + (repository_root / ".github/workflows/evil.yml",),
            "space": tracked + (repository_root / ".github/workflows/name with spaces.yml",),
            "newline": tracked + (repository_root / ".github/workflows/name\nwith.yml",),
        }
        for mutation, candidate in mutation_cases.items():
            with self.subTest(workflow_listing_mutation=mutation):
                with self.assertRaises(AssertionError):
                    assert_exact_workflow_listing(candidate)

        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            workflow_root = temporary_root / ".github" / "workflows"
            workflow_root.mkdir(parents=True)
            named_paths = (
                Path(".github/workflows/name with spaces.yml"),
                Path(".github/workflows/name\nwith.yml"),
            )
            for relative in named_paths:
                (temporary_root / relative).write_text("name: fixture\n", encoding="utf-8")
            listing = b"\0".join(str(path).encode("utf-8") for path in named_paths) + b"\0"
            with patch.object(
                platform_actionlint.subprocess,
                "run",
                return_value=SimpleNamespace(stdout=listing),
            ):
                parsed_named_paths = platform_actionlint.tracked_workflow_paths(temporary_root)
            self.assertEqual(
                tuple(path.relative_to(temporary_root) for path in parsed_named_paths),
                tuple(sorted(named_paths)),
            )
            duplicate_listing = listing + str(named_paths[0]).encode("utf-8") + b"\0"
            with patch.object(
                platform_actionlint.subprocess,
                "run",
                return_value=SimpleNamespace(stdout=duplicate_listing),
            ):
                with self.assertRaisesRegex(platform_actionlint.ActionlintError, "duplicate"):
                    platform_actionlint.tracked_workflow_paths(temporary_root)

        with (
            patch.object(platform_actionlint, "tracked_workflow_paths", return_value=tracked),
            patch.object(
                platform_actionlint.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=17),
            ) as runner,
        ):
            self.assertEqual(platform_actionlint.run_actionlint(Path("/tmp/actionlint"), repository_root), 17)
        command = runner.call_args.args[0]
        self.assertEqual(command[1:3], ["-shellcheck", ""])
        self.assertEqual(command[3:], [str(path) for path in tracked])

    def test_contract_self_test_is_clean(self) -> None:
        self.assertEqual(ALLOWED_ACTION_OWNERS, frozenset({"actions"}))
        self.assertEqual(action_pin_issues(), [])
        self.assertEqual(workflow_level_permission_issues(), [])
        workflow_text = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(
            security_status_permission_issues(workflow_text),
            [],
        )
        self.assertEqual(host_tools_pin_verification_issues(workflow_text), [])
        missing_pin_resolver = workflow_text.replace(
            "platform/tools/platform_host_tools_pin.py resolve",
            "platform/tools/platform_host_tools_pin.py inspect",
            1,
        )
        self.assertTrue(
            any(
                "canonical host-tools pin resolver exactly once" in issue
                for issue in host_tools_pin_verification_issues(missing_pin_resolver)
            )
        )
        missing_pin_history = workflow_text.replace(
            "          ref: ${{ github.sha }}\n"
            "          fetch-depth: 0\n"
            "          persist-credentials: false\n"
            "      - name: Resolve and verify canonical host-tools pin against full target history",
            "          ref: ${{ github.sha }}\n"
            "          fetch-depth: 1\n"
            "          persist-credentials: false\n"
            "      - name: Resolve and verify canonical host-tools pin against full target history",
            1,
        )
        self.assertTrue(
            any(
                "full target history" in issue
                for issue in host_tools_pin_verification_issues(missing_pin_history)
            )
        )
        self.assertEqual(release_runtime_workflow_issues(workflow_text), [])
        missing_manual_route = workflow_text.replace(
            "(github.event_name == 'push' || github.event_name == 'workflow_dispatch') &&",
            "(github.event_name == 'push') &&",
            1,
        )
        self.assertTrue(
            any(
                "manual route condition" in issue
                for issue in release_runtime_workflow_issues(missing_manual_route)
            )
        )
        missing_dev_route = workflow_text.replace(
            "github.ref == 'refs/heads/dev'",
            "github.ref == 'refs/heads/main'",
            1,
        )
        self.assertTrue(
            any(
                "canonical dev ref condition" in issue
                for issue in release_runtime_workflow_issues(missing_dev_route)
            )
        )
        missing_full_builder = workflow_text.replace(
            "$build_root/platform/tools/platform_build_release.sh",
            "$build_root/platform/tools/platform_build_live_qa_runtime.py",
            1,
        )
        self.assertTrue(
            any(
                "canonical release builder" in issue
                for issue in release_runtime_workflow_issues(missing_full_builder)
            )
        )
        release_publishing = workflow_text.replace(
            "        run: |\n          set -Eeuo pipefail\n          umask 077",
            "        uses: actions/upload-artifact@" + "a" * 40 + "\n"
            "        run: |\n          set -Eeuo pipefail\n          umask 077",
            1,
        )
        self.assertTrue(
            any(
                "must not publish" in issue
                for issue in release_runtime_workflow_issues(release_publishing)
            )
        )
        draft_text = DRAFT_CLOUDFLARE_WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(draft_cloudflare_workflow_issues(draft_text), [])
        self.assertEqual(
            yaml.safe_load(draft_text)["jobs"]["detect-release"]["if"].strip().startswith("${{"),
            True,
        )
        fork_dispatch_repository = draft_text.replace(
            "github.repository == 'StrayForest/old_sparky'",
            "github.repository == 'attacker/old_sparky'",
            1,
        )
        self.assertTrue(
            any(
                "exact canonical trigger identity expression" in issue
                for issue in draft_cloudflare_workflow_issues(fork_dispatch_repository)
            )
        )
        branch_or = draft_text.replace(
            "github.event.workflow_run.event == 'push' &&",
            "github.event.workflow_run.event == 'push' ||",
            1,
        )
        self.assertIsInstance(yaml.safe_load(branch_or), dict)
        self.assertTrue(
            any(
                "exact canonical trigger identity expression" in issue
                for issue in draft_cloudflare_workflow_issues(branch_or)
            )
        )
        resolver_or_true = draft_text.replace(
            'test "$DISPATCH_SHA" = "$GITHUB_SHA"',
            'test "$DISPATCH_SHA" = "$GITHUB_SHA" || true',
            1,
        )
        self.assertIsInstance(yaml.safe_load(resolver_or_true), dict)
        self.assertTrue(
            any(
                "exact dispatch SHA equality guard" in issue
                or "hide provenance failures" in issue
                for issue in draft_cloudflare_workflow_issues(resolver_or_true)
            )
        )
        build_always = draft_text.replace(
            "if: ${{ needs.detect-release.result == 'success' && needs.detect-release.outputs.changed == 'true' }}",
            "if: ${{ always() && needs.detect-release.result == 'success' && needs.detect-release.outputs.changed == 'true' }}",
            1,
        )
        self.assertIsInstance(yaml.safe_load(build_always), dict)
        self.assertTrue(
            any(
                "build-release if must require successful" in issue
                for issue in draft_cloudflare_workflow_issues(build_always)
            )
        )
        release_if = """      ${{
        needs.detect-release.result == 'success' &&
        needs.detect-release.outputs.changed == 'true' &&
        needs.build-release.result == 'success'
      }}"""
        release_always = draft_text.replace(
            release_if,
            """      ${{
        always() &&
        needs.detect-release.result == 'success' &&
        needs.detect-release.outputs.changed == 'true' &&
        needs.build-release.result == 'success'
      }}""",
            1,
        )
        self.assertIsInstance(yaml.safe_load(release_always), dict)
        self.assertTrue(
            any(
                "release if must require successful" in issue
                for issue in draft_cloudflare_workflow_issues(release_always)
            )
        )
        combined_critical_mutation = release_always.replace(
            "if: ${{ needs.detect-release.result == 'success' && needs.detect-release.outputs.changed == 'true' }}",
            "if: ${{ always() && needs.detect-release.result == 'success' && needs.detect-release.outputs.changed == 'true' }}",
            1,
        ).replace(
            "github.event.workflow_run.event == 'push' &&",
            "github.event.workflow_run.event == 'push' ||",
            1,
        ).replace(
            'test "$DISPATCH_SHA" = "$GITHUB_SHA"',
            'test "$DISPATCH_SHA" = "$GITHUB_SHA" || true',
            1,
        )
        self.assertIsInstance(yaml.safe_load(combined_critical_mutation), dict)
        combined_issues = draft_cloudflare_workflow_issues(combined_critical_mutation)
        self.assertTrue(any("exact canonical trigger identity expression" in issue for issue in combined_issues))
        self.assertTrue(any("build-release if must require successful" in issue for issue in combined_issues))
        self.assertTrue(any("release if must require successful" in issue for issue in combined_issues))
        self.assertTrue(any("exact dispatch SHA equality guard" in issue for issue in combined_issues))
        missing_current_dev_equality = draft_text.replace(
            "if current_sha != target_sha:",
            "if False:",
            1,
        )
        self.assertTrue(
            any(
                "current dev SHA equality" in issue
                for issue in draft_cloudflare_workflow_issues(missing_current_dev_equality)
            )
        )
        malformed_api_failure = draft_text.replace(
            "--fail-with-body --silent --show-error",
            "--silent --show-error",
            1,
        )
        self.assertTrue(
            any(
                "fail-closed GitHub ref request" in issue
                for issue in draft_cloudflare_workflow_issues(malformed_api_failure)
            )
        )
        unbounded_api = draft_text.replace(
            "--connect-timeout 5 --max-time 10",
            "--connect-timeout 5",
            1,
        )
        self.assertTrue(
            any(
                "bounded GitHub ref total timeout" in issue
                for issue in draft_cloudflare_workflow_issues(unbounded_api)
            )
        )
        retried_api = draft_text.replace(
            "--connect-timeout 5 --max-time 10",
            "--connect-timeout 5 --max-time 10 --retry 2",
            1,
        )
        self.assertTrue(
            any(
                "must not retry an unsafe provenance read" in issue
                for issue in draft_cloudflare_workflow_issues(retried_api)
            )
        )
        hidden_api_failure = draft_text.replace(
            '--output "$current_dev_ref"',
            '--output "$current_dev_ref" || true',
            1,
        )
        self.assertTrue(
            any(
                "hide provenance failures" in issue
                for issue in draft_cloudflare_workflow_issues(hidden_api_failure)
            )
        )
        missing_api_parse = draft_text.replace(
            'payload.get("ref") != "refs/heads/dev"',
            'payload.get("ref") != "refs/heads/main"',
            1,
        )
        self.assertTrue(
            any(
                "parsed dev ref identity" in issue
                for issue in draft_cloudflare_workflow_issues(missing_api_parse)
            )
        )
        parser = _draft_current_dev_parser_script()
        with tempfile.TemporaryDirectory() as directory:
            ref_path = Path(directory) / "dev.json"
            target_sha = "a" * 40
            ref_path.write_text(
                json.dumps(
                    {
                        "ref": "refs/heads/dev",
                        "object": {"type": "commit", "sha": target_sha},
                    }
                ),
                encoding="utf-8",
            )
            valid_api = subprocess.run(
                [sys.executable, "-", str(ref_path), target_sha, "StrayForest/old_sparky"],
                input=parser,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(valid_api.returncode, 0, valid_api.stderr)
            stale_api = json.loads(ref_path.read_text(encoding="utf-8"))
            stale_api["object"]["sha"] = "b" * 40
            ref_path.write_text(json.dumps(stale_api), encoding="utf-8")
            stale_result = subprocess.run(
                [sys.executable, "-", str(ref_path), target_sha, "StrayForest/old_sparky"],
                input=parser,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(stale_result.returncode, 0)
            self.assertIn("target SHA is not current dev", stale_result.stderr)
            ref_path.write_text("{malformed", encoding="utf-8")
            malformed_result = subprocess.run(
                [sys.executable, "-", str(ref_path), target_sha, "StrayForest/old_sparky"],
                input=parser,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(malformed_result.returncode, 0)
            self.assertIn("not valid JSON", malformed_result.stderr)
        split_draft_concurrency = draft_text.replace(
            "group: platform-draft-cloudflare-${{ github.event_name == 'workflow_run' && github.event.workflow_run.head_sha || github.sha }}",
            "group: platform-draft-cloudflare-${{ github.ref }}",
            1,
        )
        self.assertTrue(
            any(
                "immutable target SHA" in issue
                for issue in draft_cloudflare_workflow_issues(split_draft_concurrency)
            )
        )
        cancelling_draft_concurrency = draft_text.replace(
            "cancel-in-progress: false",
            "cancel-in-progress: true",
            1,
        )
        self.assertTrue(
            any(
                "must not cancel" in issue
                for issue in draft_cloudflare_workflow_issues(cancelling_draft_concurrency)
            )
        )
        missing_draft_timeout = draft_text.replace(
            "    timeout-minutes: 20\n",
            "",
            1,
        )
        self.assertTrue(
            any(
                "verify-pr" in issue and "timeout" in issue
                for issue in draft_cloudflare_workflow_issues(missing_draft_timeout)
            )
        )
        floating_draft_checkout = draft_text.replace(
            "ref: ${{ steps.resolve-target.outputs.target_sha }}",
            "ref: dev",
            1,
        )
        self.assertTrue(
            any(
                "immutable target SHA" in issue or "floating dev" in issue
                for issue in draft_cloudflare_workflow_issues(floating_draft_checkout)
            )
        )
        non_dev_dispatch = draft_text.replace(
            'test "$DISPATCH_REF" = "refs/heads/dev"',
            'test "$DISPATCH_REF" = "refs/heads/main"',
            1,
        )
        self.assertTrue(
            any(
                "dev-only dispatch guard" in issue
                for issue in draft_cloudflare_workflow_issues(non_dev_dispatch)
            )
        )
        identity_mutations = (
            (
                "github.event.workflow_run.status == 'completed'",
                "github.event.workflow_run.status == 'queued'",
                "completed workflow_run guard",
            ),
            (
                "github.event.workflow_run.event == 'push'",
                "github.event.workflow_run.event == 'workflow_dispatch'",
                "workflow_run push guard",
            ),
            (
                "github.event.workflow_run.head_branch == 'dev'",
                "github.event.workflow_run.head_branch == 'main'",
                "workflow_run dev branch guard",
            ),
            (
                "github.event.workflow_run.repository.full_name == 'StrayForest/old_sparky'",
                "github.event.workflow_run.repository.full_name == 'attacker/old_sparky'",
                "canonical workflow_run repository guard",
            ),
            (
                "github.event.workflow_run.head_repository.full_name == 'StrayForest/old_sparky'",
                "github.event.workflow_run.head_repository.full_name == 'attacker/old_sparky'",
                "canonical workflow_run head repository guard",
            ),
            (
                "github.event.workflow_run.workflow_id == 339062797",
                "github.event.workflow_run.workflow_id == 339062798",
                "canonical source workflow id guard",
            ),
            (
                "github.event.workflow_run.name == 'Platform security and build'",
                "github.event.workflow_run.name == 'Attacker workflow'",
                "canonical source workflow name guard",
            ),
            (
                "github.event.workflow_run.path == '.github/workflows/platform-security.yml'",
                "github.event.workflow_run.path == '.github/workflows/attacker.yml'",
                "canonical source workflow path guard",
            ),
            (
                "github.event.workflow_run.head_sha != ''",
                "github.event.workflow_run.head_sha == ''",
                "workflow_run SHA presence guard",
            ),
        )
        for original, replacement, description in identity_mutations:
            with self.subTest(identity=description):
                mutated_identity = draft_text.replace(original, replacement, 1)
                self.assertTrue(
                    any(
                        "exact canonical trigger identity expression" in issue
                        for issue in draft_cloudflare_workflow_issues(mutated_identity)
                    )
                )
        name_only_identity = draft_text
        for original, _, description in identity_mutations:
            if description == "canonical source workflow name guard":
                continue
            name_only_identity = name_only_identity.replace(original, "true", 1)
        self.assertTrue(
            any(
                "exact canonical trigger identity expression" in issue
                for issue in draft_cloudflare_workflow_issues(name_only_identity)
            )
        )
        unbounded_draft_curl = draft_text.replace(
            "--connect-timeout 5",
            "",
            1,
        )
        self.assertTrue(
            any(
                "--connect-timeout 5" in issue
                or "bounded GitHub ref connect timeout" in issue
                for issue in draft_cloudflare_workflow_issues(unbounded_draft_curl)
            )
        )
        hidden_draft_curl_failure = draft_text.replace(
            '"$url")"; then',
            '"$url" || true)"; then',
            1,
        )
        self.assertTrue(
            any(
                "hidden" in issue
                for issue in draft_cloudflare_workflow_issues(hidden_draft_curl_failure)
            )
        )
        missing_draft_retry_bound = draft_text.replace(
            "            local max_attempts=10\n",
            "",
            1,
        )
        self.assertTrue(
            any(
                "bounded retry count" in issue
                for issue in draft_cloudflare_workflow_issues(missing_draft_retry_bound)
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            action_file = Path(directory) / "action.yml"
            action_file.write_text(
                "\n".join(
                    (
                        "runs:",
                        "  using: composite",
                        "  steps:",
                        "    - uses: ./local-action",
                        "    - uses: actions/checkout@" + "a" * 40,
                        "    - uses: actions/setup-python@v6",
                        "    - uses: unapproved/action@" + "b" * 40,
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            action_issues = action_pin_issues([action_file])
            self.assertEqual(len(action_issues), 2)
            self.assertTrue(any("40-character commit SHA" in item for item in action_issues))
            self.assertTrue(any("owner 'unapproved'" in item for item in action_issues))
        self.assertEqual(collect_issues(), [])
        synthetic_setup_job = SECURITY_WORKFLOW.read_text(encoding="utf-8") + """
  synthetic-python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: platform/requirements-ci.lock.txt
"""
        dependency_issues = _ci_dependency_issues(synthetic_setup_job)
        self.assertTrue(
            any(
                "Python CI job synthetic-python must invoke the canonical installer exactly once"
                in issue
                for issue in dependency_issues
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            component_dir = Path(directory)
            _write_backend_component_fixture(component_dir)
            aggregate = verify_backend_components(component_dir)
            self.assertEqual(aggregate["status"], "passed")
            expected_backend_count = int(EXPECTED_SNAPSHOT["backend_test_count"])
            self.assertEqual(aggregate["tests_selected"], expected_backend_count)
            self.assertEqual(aggregate["tests_run"], expected_backend_count)
            expected_ids = aggregate["expected_ids"]
            self.assertEqual(aggregate["executed_ids"], expected_ids)
            self.assertEqual(len(aggregate["timings"]), expected_backend_count)

        mutations = (
            "duplicate executed ID",
            "out-of-order executed IDs",
            "unexpected executed ID",
            "missing executed ID",
            "empty timings",
            "invalid timing duration",
            "tests_run mismatch",
            "out-of-order manifest IDs",
            "duplicate manifest artifact",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                component_dir = Path(directory)
                _write_backend_component_fixture(component_dir)
                summary_path = (
                    component_dir / "backend-unit" / "summary.json"
                )
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                if mutation == "duplicate executed ID":
                    summary["executed_ids"] = [
                        summary["expected_ids"][0],
                        *summary["expected_ids"],
                    ]
                elif mutation == "out-of-order executed IDs":
                    summary["executed_ids"][0:2] = summary["executed_ids"][0:2][::-1]
                elif mutation == "unexpected executed ID":
                    summary["executed_ids"][0] = "tests.unexpected.TestCase.test_not_catalogued"
                elif mutation == "missing executed ID":
                    summary["executed_ids"] = summary["executed_ids"][:-1]
                elif mutation == "empty timings":
                    summary["timings"] = []
                elif mutation == "invalid timing duration":
                    summary["timings"][0]["duration_ms"] = -1
                elif mutation == "tests_run mismatch":
                    summary["tests_run"] -= 1
                elif mutation == "out-of-order manifest IDs":
                    manifest_path = component_dir / "backend-unit" / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["tests"][0:2] = manifest["tests"][0:2][::-1]
                    manifest_path.write_text(
                        json.dumps(manifest), encoding="utf-8"
                    )
                else:
                    manifest_path = component_dir / "backend-unit" / "manifest.json"
                    duplicate_path = component_dir / "duplicate" / "manifest.json"
                    duplicate_path.parent.mkdir(parents=True, exist_ok=True)
                    duplicate_path.write_text(
                        manifest_path.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                if mutation not in {"out-of-order manifest IDs", "duplicate manifest artifact"}:
                    summary_path.write_text(json.dumps(summary), encoding="utf-8")
                with self.assertRaises(ValueError):
                    verify_backend_components(component_dir)

    def test_load_profiles_are_unique_and_have_stable_digests(self) -> None:
        profiles = load_profiles()
        self.assertEqual(
            set(profiles),
            {
                "ready-vote-slo-v2",
                "ready-vote-capacity-ramp-v2",
                "ready-vote-saturation-ramp-v1",
                "ready-vote-saturation-ramp-v2",
                "ready-vote-saturation-ramp-v3",
                "ready-vote-saturation-ramp-v4",
                "ready-vote-stress-15k-v2",
                "ready-vote-stress-20k-v2",
                "ready-vote-spike-v1",
                "read-mix-human-v2",
                "read-mix-stress-v2",
                "read-mix-concurrency-ramp-v1",
                "authenticated-page-load-v1",
                "authenticated-page-load-v2",
                "tournament-lifecycle-capacity-v1",
                "tournament-lifecycle-scale-v1",
                "tournament-lifecycle-slo-v1",
            },
        )
        profile = get_profile("ready-vote-slo-v2")
        self.assertEqual(profile_digest(profile), profile_digest(profile))
        self.assertEqual(profile["execution"]["generator"], "GitHub-hosted external runner")
        self.assertEqual(profile["acceptance"]["kind"], "slo")
        self.assertEqual(
            profile["acceptance"]["accepted_request_latency"],
            {"p50_ms": 250, "p90_ms": 400, "p95_ms": 600, "p99_ms": 1000},
        )
        self.assertEqual(
            get_profile("authenticated-page-load-v1").get(
                "client_transport", "urllib-http1-close"
            ),
            "urllib-http1-close",
        )
        self.assertEqual(
            get_profile("authenticated-page-load-v2")["client_transport"],
            "http1-keepalive",
        )

    def test_stress_and_capacity_profiles_have_distinct_semantics(self) -> None:
        stress = get_profile("ready-vote-stress-15k-v2")
        capacity = get_profile("ready-vote-capacity-ramp-v2")
        self.assertEqual(stress["acceptance"]["kind"], "stress")
        self.assertNotIn("logical_final_failure_percent", stress["acceptance"])
        self.assertEqual(capacity["acceptance"]["kind"], "capacity")
        self.assertEqual(len(capacity["traffic"]["phases"]), 7)
        self.assertEqual(
            capacity["acceptance"]["capacity"]["target_logical_actions_per_second"],
            [20, 30, 40, 50, 60, 70, 80],
        )
        saturation = get_profile("ready-vote-saturation-ramp-v1")
        self.assertEqual(saturation["acceptance"]["kind"], "stress")
        self.assertEqual(
            [phase["target_logical_actions_per_second"] for phase in saturation["traffic"]["phases"]],
            [80, 90, 100, 110, 120],
        )
        saturation_v2 = get_profile("ready-vote-saturation-ramp-v2")
        self.assertEqual(
            [phase["target_logical_actions_per_second"] for phase in saturation_v2["traffic"]["phases"]],
            [120, 135, 150, 165],
        )
        saturation_v3 = get_profile("ready-vote-saturation-ramp-v3")
        self.assertEqual(
            [phase["target_logical_actions_per_second"] for phase in saturation_v3["traffic"]["phases"]],
            [105, 110, 115, 120],
        )
        saturation_v4 = get_profile("ready-vote-saturation-ramp-v4")
        self.assertEqual(
            [phase["target_logical_actions_per_second"] for phase in saturation_v4["traffic"]["phases"]],
            [120, 125, 130, 135],
        )
        read_ramp = get_profile("read-mix-concurrency-ramp-v1")
        self.assertEqual(
            read_ramp["traffic"]["concurrency_stages"],
            [16, 32, 48, 64, 80, 96, 112, 128],
        )

    def test_tournament_lifecycle_profiles_use_the_local_qa_harness(self) -> None:
        for profile_id in (
            "tournament-lifecycle-slo-v1",
            "tournament-lifecycle-scale-v1",
            "tournament-lifecycle-capacity-v1",
        ):
            profile = get_profile(profile_id)
            self.assertEqual(profile["mode"], "tournament-lifecycle")
            self.assertEqual(profile["fixture"]["tournament_count"], 20)
            self.assertEqual(profile["fixture"]["users_per_tournament"], 500)
            self.assertEqual(profile["execution"]["generator"], "platform_production_qa.py")
            self.assertTrue(profile["execution"]["external_runner_forbidden"])

        with self.assertRaisesRegex(LoadProfileError, "not dispatchable|external runner"):
            run_profile(
                get_profile("tournament-lifecycle-slo-v1"),
                Path("/tmp/unused-lifecycle-manifest.json"),
                Path("/tmp/unused-lifecycle-report.json"),
            )

    def test_load_profile_rejects_missing_cleanup_contract(self) -> None:
        profile = get_profile("ready-vote-slo-v2")
        invalid = dict(profile)
        invalid["correctness"] = dict(profile["correctness"])
        invalid["correctness"]["cleanup_required"] = False
        with self.assertRaises(LoadProfileError):
            validate_profile(invalid)

    def test_profile_dispatcher_records_contract_and_source_sha(self) -> None:
        profile = get_profile("ready-vote-slo-v2")
        fake_report = {
            "schema": 1,
            "mode": "ready-vote",
            "acceptance": {"passed": True},
            "raw_http": {"requests": 2},
            "logical": {"actions": 1},
            "phases": {"primary": {"logical": {"actions": 1}}},
        }
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            with (
                patch(
                    "tools.platform_external_load.load_manifest",
                    return_value=({}, [object()]),
                ),
                patch(
                    "tools.platform_external_load.run_load",
                    return_value=fake_report,
                ),
                patch(
                    "tools.platform_load._source_git_sha",
                    return_value="a" * 40,
                ),
                patch.dict(
                    os.environ,
                    {"SOURCE_GIT_SHA": "a" * 40, "GITHUB_RUN_ID": "123"},
                ),
            ):
                self.assertEqual(
                    run_profile(profile, Path(directory) / "manifest.json", report_path),
                    0,
                )
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["source_git_sha"], "a" * 40)
        self.assertTrue(report["authoritative"])
        self.assertTrue(report["dispatchable"])
        self.assertEqual(report["load_contract"]["profile_id"], "ready-vote-slo-v2")
        self.assertEqual(report["load_contract"]["http_attempts"], 2)


if __name__ == "__main__":
    unittest.main()
