from __future__ import annotations

import json
import io
import asyncio
import importlib
import os
import re
import runpy
import selectors
import signal
import shutil
import stat
import subprocess
import sys
import time
import unittest
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from tools.platform_test_catalog import (
    BACKEND_CONTOURS,
    CONTOUR_METADATA,
    CONTOUR_TIMEOUT_SECONDS,
    EXPECTED_SNAPSHOT,
    INTENTIONAL_SKIP_IDS,
    TEST_ENVIRONMENT_REQUIREMENTS,
    TEST_NONROOT_PID_NAMESPACE_REQUIREMENTS,
    TestCase as CatalogTestCase,
    VERIFICATION_CONTOUR,
    cases_for_contour,
    discover_test_cases,
    test_id_digest,
    test_environment_requirement_issues,
    test_execution_requirement_issues,
)
from tools.platform_load import (
    LoadProfileError,
    get_profile,
    load_profiles,
    profile_digest,
    run_profile_worker,
    validate_profile,
)
from tools.platform_migration_support import (
    MIGRATION_DIAGNOSTIC_ENV,
    _MIGRATION_DIAGNOSTIC_MAX_BYTES,
    MIGRATION_SUBPROCESS_TIMEOUT_SECONDS,
    MigrationCommandError,
    MigrationCommandTimeout,
    record_migration_progress,
    run_migration_subprocess,
    validate_disposable_migration_target,
)
from tools.platform_verify import (
    CI_GATE_IDS,
    DETERMINISTIC_GATE_IDS,
    GATES_BY_ID,
    RELEASE_RUNTIME_TEST_IDS,
    VerificationError,
    _cleanup_timed_out_backend_privileged_resources,
    _create_private_migration_diagnostic,
    _privileged_runner_python,
    _preflight_backend_test_environment,
    _run,
    _validated_privileged_environment,
    _verification_contract_commands,
    dispatch,
    impact_plan,
    dispatch_ci,
    registry_payload,
)
from tools.platform_test_runner import (
    ContourTimeout,
    TimingRunner,
    TestTiming,
    TestResourceConfigurationError,
    _clear_integration_auth_rate_limit_keys,
    _summary,
    _integration_preflight_error,
    _require_integration_resources_ready,
    _require_redis_db15_ready,
    _teardown_privileged_redis_resource,
    _write_progress_json,
    main as test_runner_main,
    _preflight_execution_environment,
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
    PRODUCTION_WORKFLOW,
    SECURITY_WORKFLOW,
    _production_secret_scope_issues,
    _backend_workflow_issues,
    actionlint_workflow_issues,
    action_pin_issues,
    collect_issues,
    _ci_dependency_issues,
    extract_gate_invocations,
    host_tools_pin_verification_issues,
    release_runtime_workflow_issues,
    security_status_permission_issues,
    source_bound_backup_dispatch_issues,
    workflow_level_permission_issues,
)

EXPECTED_LOCAL_CI_EXECUTION_ORDER = (
    "verification-contract",
    "docs",
    "python-quality",
    "security",
    "migration",
    "backend",
    "web-quality",
    "web-hermetic",
)


def _trusted_runuser() -> str:
    candidates = (
        shutil.which("runuser"),
        "/usr/sbin/runuser",
        "/usr/bin/runuser",
        "/sbin/runuser",
    )
    for candidate in candidates:
        if not candidate or not Path(candidate).is_absolute():
            continue
        path = Path(candidate)
        try:
            info = path.lstat()
        except OSError:
            continue
        if (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and info.st_gid == 0
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) & 0o022 == 0
            and os.access(path, os.X_OK)
        ):
            return str(path)
    raise AssertionError("trusted runuser binary is required for privileged contour tests")


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


_LOCK_HOLDER_TIMEOUT, _LOCK_HOLDER_DIAGNOSTIC_BYTES = 2.0, 4096
@contextmanager
def _lock_holder(command: list[str], *, cwd: Path, env: dict[str, str] | None = None):
    process = selector = None
    diagnostics = {"stdout": bytearray(), "stderr": bytearray()}
    pending = bytearray()
    state = SimpleNamespace(ready_line=None)
    def read_ready() -> None:
        for key, _ in selector.select(0):
            stream = key.fileobj
            name = str(key.data)
            try:
                chunk = os.read(stream.fileno(), 4096)
            except (BlockingIOError, InterruptedError):
                continue
            if not chunk:
                selector.unregister(stream)
                continue
            buffer = diagnostics[name]
            buffer.extend(chunk[: max(0, _LOCK_HOLDER_DIAGNOSTIC_BYTES - len(buffer))])
            if name != "stdout" or state.ready_line is not None:
                continue
            pending.extend(chunk)
            while b"\n" in pending and state.ready_line is None:
                line, _, remainder = pending.partition(b"\n")
                pending[:] = remainder
                line = line.rstrip(b"\r")
                if line == b"ready" or line.startswith(b"ready:"):
                    state.ready_line = line
            if len(pending) > _LOCK_HOLDER_DIAGNOSTIC_BYTES:
                pending[:] = pending[-_LOCK_HOLDER_DIAGNOSTIC_BYTES:]
    def cleanup() -> None:
        if process is None:
            return
        try:
            try:
                process.stdin.write(b"release\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError, AttributeError):
                pass
            finally:
                process.stdin.close()
            try:
                process.wait(timeout=0.15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=0.15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            if process.returncode != 0:
                raise RuntimeError(f"lock-holder exited {process.returncode}")
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        finally:
            if selector is not None:
                selector.close()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
        selector = selectors.DefaultSelector()
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        deadline = time.monotonic() + _LOCK_HOLDER_TIMEOUT
        while state.ready_line is None and time.monotonic() < deadline:
            read_ready()
            if process.poll() is not None:
                break
            remaining = deadline - time.monotonic()
            selector.select(max(0, min(0.05, remaining)))
        read_ready()
        if state.ready_line is None:
            raise RuntimeError(
                f"lock-holder did not publish ready: stderr={bytes(diagnostics['stderr'])!r}"
            )
        yield state
    except BaseException as exc:
        try:
            cleanup()
        except BaseException as cleanup_error:
            exc.add_note(f"lock-holder cleanup failed: {cleanup_error}")
        raise
    else:
        cleanup()


class PlatformVerificationContractTests(unittest.TestCase):
    def test_hosted_test_environment_blocks_before_privileged_resources(self) -> None:
        test_id = next(iter(TEST_ENVIRONMENT_REQUIREMENTS))
        required_environment = dict(TEST_ENVIRONMENT_REQUIREMENTS[test_id])
        self.assertEqual(
            registry_payload()["backend_test_catalog"]["test_environment_requirements"][test_id],
            required_environment,
        )
        execution_requirements = registry_payload()["backend_test_catalog"][
            "test_execution_requirements"
        ]
        self.assertEqual(
            set(execution_requirements),
            set(TEST_NONROOT_PID_NAMESPACE_REQUIREMENTS),
        )
        registered_ids = {case.test_id for case in discover_test_cases()}
        self.assertEqual(len(TEST_NONROOT_PID_NAMESPACE_REQUIREMENTS), 11)
        self.assertLessEqual(TEST_NONROOT_PID_NAMESPACE_REQUIREMENTS, registered_ids)
        negative_probe_id = (
            "tests.test_platform_load_runtime.LoadRuntimeBudgetTests."
            "test_namespace_probe_is_explicit_and_has_no_unsafe_fallback"
        )
        self.assertNotIn(negative_probe_id, execution_requirements)
        self.assertEqual(
            test_environment_requirement_issues([test_id], required_environment),
            [],
        )
        synthetic_restore_id = (
            "tests.test_platform_backup_restore_drill.PlatformBackupRestoreDrillTests."
            "test_synthetic_pg16_restore_diagnostic_under_root"
        )
        hosted_runner_environment = {
            "GITHUB_ACTIONS": "true",
            "RUNNER_OS": "Linux",
            "RUNNER_ENVIRONMENT": "github-hosted",
        }
        self.assertEqual(
            registry_payload()["backend_test_catalog"]["test_environment_requirements"][
                synthetic_restore_id
            ],
            hosted_runner_environment,
        )
        self.assertEqual(
            test_environment_requirement_issues(
                [synthetic_restore_id], hosted_runner_environment
            ),
            [],
        )
        self.assertEqual(
            test_environment_requirement_issues(
                [synthetic_restore_id], {"GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux"}
            ),
            [synthetic_restore_id + " requires RUNNER_ENVIRONMENT=github-hosted"],
        )

        pid_namespace_id = sorted(TEST_NONROOT_PID_NAMESPACE_REQUIREMENTS)[0]
        self.assertEqual(
            test_execution_requirement_issues(
                [pid_namespace_id],
                uid=1000,
                gid=1000,
                system="linux",
                namespace_probe=lambda: "/usr/bin/unshare",
            ),
            [],
        )
        self.assertEqual(
            test_execution_requirement_issues(
                [pid_namespace_id],
                uid=1000,
                gid=1000,
                system="linux",
                namespace_probe=lambda: (_ for _ in ()).throw(RuntimeError("unavailable")),
            ),
            [
                f"{pid_namespace_id} requires a non-root Linux runner with PID namespace capability"
            ],
        )
        root_probe_calls: list[bool] = []
        self.assertEqual(
            test_execution_requirement_issues(
                [pid_namespace_id],
                uid=0,
                gid=0,
                system="linux",
                namespace_probe=lambda: root_probe_calls.append(True) or "/usr/bin/unshare",
            ),
            [
                f"{pid_namespace_id} requires a non-root Linux runner with PID namespace capability"
            ],
        )
        self.assertEqual(root_probe_calls, [])

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(VerificationError, "LOCAL GATE BLOCKED"):
                _preflight_backend_test_environment("backend-privileged", ())
            _preflight_backend_test_environment(
                "backend-privileged",
                ("--focused", "tests.synthetic.Unrelated.test_case"),
            )
            _preflight_execution_environment(
                (
                    CatalogTestCase(
                        test_id="tests.synthetic.Unrelated.test_case",
                        module="test_synthetic_unrelated",
                        class_name="Unrelated",
                        method_name="test_case",
                        line=1,
                        is_async=False,
                        contour="backend-privileged",
                    ),
                )
            )
            self.assertEqual(
                test_environment_requirement_issues(
                    [test_id],
                    {"GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux"},
                ),
                [
                    test_id
                    + " requires RUNNER_ENVIRONMENT=github-hosted"
                ],
            )

            with (
                patch("tools.platform_verify._require_backend_privileged_root"),
                patch("tools.platform_verify._validated_privileged_environment") as env,
                patch("tools.platform_verify._run") as run,
            ):
                with self.assertRaisesRegex(VerificationError, "LOCAL GATE BLOCKED"):
                    dispatch("backend-privileged")
                env.assert_not_called()
                run.assert_not_called()

            with (
                patch("tools.platform_test_runner._require_root_identity") as root,
                patch("tools.platform_test_runner._require_test_environment") as env,
                patch("tools.platform_test_runner.verification_resource_lock") as lock,
            ):
                with self.assertRaisesRegex(SystemExit, "LOCAL GATE BLOCKED"):
                    test_runner_main(["--contour", "backend-privileged"])
                root.assert_called_once_with("backend-privileged")
                env.assert_not_called()
                lock.assert_not_called()

            root_owned_id = sorted(TEST_NONROOT_PID_NAMESPACE_REQUIREMENTS)[0]
            root_owned_case = CatalogTestCase(
                test_id=root_owned_id,
                module="test_platform_load_runtime",
                class_name="LoadRuntimeBudgetTests",
                method_name=root_owned_id.rsplit(".", 1)[1],
                line=1,
                is_async=False,
                contour="performance-contract",
            )
            unrelated_performance_case = CatalogTestCase(
                test_id="tests.test_platform_external_load.ExternalLoadTests.test_unrelated",
                module="test_platform_external_load",
                class_name="ExternalLoadTests",
                method_name="test_unrelated",
                line=1,
                is_async=False,
                contour="performance-contract",
            )
            with patch("tools.platform_verify.os.getuid", return_value=0), patch(
                "tools.platform_verify.os.getgid", return_value=0
            ), patch("tools.platform_verify._run") as run:
                with self.assertRaisesRegex(VerificationError, "LOCAL GATE BLOCKED"):
                    dispatch("performance-contract", ("--focused", root_owned_id))
                run.assert_not_called()
                _preflight_backend_test_environment(
                    "performance-contract",
                    ("--focused", unrelated_performance_case.test_id),
                )

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("tools.platform_test_runner.os.getuid", return_value=0),
                patch("tools.platform_test_runner.os.getgid", return_value=0),
                patch("tools.platform_test_runner._require_test_environment") as env,
                patch("tools.platform_test_runner.verification_resource_lock") as lock,
            ):
                with self.assertRaisesRegex(SystemExit, "LOCAL GATE BLOCKED"):
                    test_runner_main(
                        ["--contour", "performance-contract", "--focused", root_owned_id]
                    )
                env.assert_not_called()
                lock.assert_not_called()
                with self.assertRaisesRegex(SystemExit, "LOCAL GATE BLOCKED"):
                    _preflight_execution_environment((root_owned_case,))
                _preflight_execution_environment((unrelated_performance_case,))

    def test_failed_subtests_record_one_parent_execution_without_hiding_failures(self) -> None:
        class FailingSubtests(unittest.TestCase):
            def test_two_subtests(self) -> None:
                for exit_code in (4, 124):
                    with self.subTest(exit_code=exit_code):
                        self.assertEqual(exit_code, 0)

        class MixedSubtestOutcomes(unittest.TestCase):
            def test_failure_and_error(self) -> None:
                with self.subTest(kind="failure"):
                    self.fail("subtest failure remains visible")
                with self.subTest(kind="error"):
                    raise RuntimeError("subtest error remains visible")

        class LateParentError(unittest.TestCase):
            def test_late_parent_error(self) -> None:
                raise RuntimeError("late parent error remains visible")

        def run_case(
            test_case_type: type[unittest.TestCase], method_name: str
        ) -> tuple[unittest.TestResult, dict[str, object], str]:
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_case_type)
            runner = TimingRunner(stream=io.StringIO(), verbosity=0)
            result = runner.run(suite)
            parent_id = test_case_type(method_name).id()
            case = CatalogTestCase(
                test_id=parent_id,
                module="tests.synthetic_subtests",
                class_name=test_case_type.__name__,
                method_name=method_name,
                line=0,
                is_async=False,
                contour=VERIFICATION_CONTOUR,
            )
            summary = _summary(
                contour=VERIFICATION_CONTOUR,
                cases=(case,),
                result=result,
                elapsed_ms=1.0,
                status="failed",
            )
            return result, summary, parent_id

        scenarios = (
            (FailingSubtests, "test_two_subtests", 2, 0, "failed"),
            (MixedSubtestOutcomes, "test_failure_and_error", 1, 1, "error"),
            (LateParentError, "test_late_parent_error", 0, 1, "error"),
        )
        for test_case_type, method_name, failure_count, error_count, outcome in scenarios:
            with self.subTest(scenario=test_case_type.__name__):
                result, summary, parent_id = run_case(test_case_type, method_name)
                self.assertEqual(result.testsRun, 1)
                self.assertEqual(len(result.failures), failure_count)
                self.assertEqual(len(result.errors), error_count)
                self.assertFalse(result.wasSuccessful())
                self.assertEqual(summary["executed_ids"], [parent_id])
                self.assertEqual(summary["missing_ids"], [])
                self.assertEqual(summary["duplicate_ids"], [])
                self.assertTrue(summary["execution_complete"])
                self.assertEqual(summary["failures"], failure_count)
                self.assertEqual(summary["errors"], error_count)
                timings = summary["timings"]
                self.assertEqual(len(timings), 1)
                self.assertEqual(timings[0]["test_id"], parent_id)
                self.assertEqual(timings[0]["outcome"], outcome)

        with tempfile.TemporaryDirectory(prefix="platform-runner-progress-") as temp_dir:
            progress_path = Path(temp_dir) / "progress.json"
            observed_checkpoint: dict[str, object] = {}

            class ProgressCases(unittest.TestCase):
                def test_a_completed(self) -> None:
                    pass

                def test_b_active(self) -> None:
                    checkpoint = json.loads(progress_path.read_text(encoding="utf-8"))
                    observed_checkpoint.update(checkpoint)
                    raise ContourTimeout("backend-privileged", 1200)

            progress_cases = tuple(
                sorted(
                    (ProgressCases("test_a_completed"), ProgressCases("test_b_active")),
                    key=lambda case: case.id(),
                )
            )
            progress_runner = TimingRunner(stream=io.StringIO(), verbosity=0)
            progress_runner.progress_path = progress_path
            progress_runner.progress_contour = "backend-privileged"
            progress_runner.progress_expected_ids = tuple(case.id() for case in progress_cases)
            progress_result = progress_runner.run(unittest.TestSuite(progress_cases))

            self.assertEqual(observed_checkpoint["status"], "diagnostic_only")
            self.assertIs(observed_checkpoint["execution_complete"], False)
            self.assertEqual(observed_checkpoint["active_test_id"], progress_cases[1].id())
            self.assertEqual(
                [entry["test_id"] for entry in observed_checkpoint["completed_timings"]],
                [progress_cases[0].id()],
            )
            self.assertTrue(progress_result.timed_out)
            self.assertFalse(progress_result.wasSuccessful())
            final_checkpoint = json.loads(progress_path.read_text(encoding="utf-8"))
            self.assertIsNone(final_checkpoint["active_test_id"])
            self.assertEqual(
                [entry["test_id"] for entry in final_checkpoint["completed_timings"]],
                [case.id() for case in progress_cases],
            )
            self.assertEqual(stat.S_IMODE(progress_path.stat().st_mode), 0o600)

            duplicate_id = progress_cases[0].id()
            progress_result.timings.extend(
                (
                    TestTiming(duplicate_id, 5.0, "passed"),
                    TestTiming(duplicate_id, 19.0, "error"),
                )
            )
            progress_result._write_progress_checkpoint()
            projected = json.loads(progress_path.read_text(encoding="utf-8"))
            projected_timings = projected["completed_timings"]
            self.assertEqual(
                [entry["test_id"] for entry in projected_timings],
                [progress_cases[1].id(), duplicate_id],
            )
            collapsed = next(
                entry for entry in projected_timings if entry["test_id"] == duplicate_id
            )
            self.assertEqual(collapsed["duration_ms"], 19.0)
            self.assertEqual(collapsed["outcome"], "error")

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

        class FakeRedis:
            def __init__(self) -> None:
                self.keys = {b"platform:other-cache:keep"}
                self.patterns: list[str] = []
                self.closed = False

            async def ping(self) -> bool:
                return True

            async def scan(self, *, cursor: int, match: str, count: int):
                if match != "platform:auth-rate:v1:*" or count != 200:
                    raise AssertionError("cleanup used an unexpected Redis scan scope")
                self.patterns.append(match)
                matching = sorted(
                    key for key in self.keys if key.startswith(b"platform:auth-rate:v1:")
                )
                return 0, matching

            async def unlink(self, *keys: bytes) -> int:
                removed = 0
                for key in keys:
                    if key in self.keys:
                        self.keys.remove(key)
                        removed += 1
                return removed

            async def aclose(self) -> None:
                self.closed = True

        safe_environment = {
            "PLATFORM_ENVIRONMENT": "test",
            "PLATFORM_DB_SCHEMA": "platform",
            "PLATFORM_DATABASE_URL": (
                "postgresql+asyncpg://u:p@127.0.0.1:5432/platformdb_test"
            ),
            "PLATFORM_REDIS_URL": "redis://127.0.0.1:6379/15",
        }
        fake_redis = FakeRedis()

        for invalid in (
            {"PLATFORM_ENVIRONMENT": "production"},
            {"PLATFORM_REDIS_URL": "redis://127.0.0.1:6379/0"},
        ):
            with self.subTest(invalid=invalid):
                environment = dict(safe_environment)
                environment.update(invalid)
                imported: list[str] = []
                real_import = __import__

                def recording_import(name: str, *args: object, **kwargs: object) -> object:
                    imported.append(name)
                    return real_import(name, *args, **kwargs)

                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch("builtins.__import__", side_effect=recording_import),
                    self.assertRaises(RuntimeError),
                ):
                    _clear_integration_auth_rate_limit_keys()
                self.assertFalse(any(name.startswith("redis") for name in imported))

        events: list[str] = []

        def first_body() -> None:
            fake_redis.keys.update(
                {
                    b"platform:auth-rate:v1:register:actor-a:1",
                    b"platform:auth-rate:v1:register:actor-b:1",
                }
            )
            self.assertEqual(
                sum(key.startswith(b"platform:auth-rate:v1:") for key in fake_redis.keys),
                2,
            )
            events.append("first-body")

        def assert_case_cleanup_ran() -> None:
            self.assertTrue(
                any(key.startswith(b"platform:auth-rate:v1:") for key in fake_redis.keys)
            )
            events.append("first-cleanup")

        class FirstCase(unittest.TestCase):
            def runTest(self) -> None:
                first_body()

        first_case = FirstCase()
        first_case.addCleanup(assert_case_cleanup_ran)

        def second_body() -> None:
            self.assertFalse(
                any(key.startswith(b"platform:auth-rate:v1:") for key in fake_redis.keys)
            )
            events.append("second-body")

        class SecondCase(unittest.TestCase):
            def runTest(self) -> None:
                second_body()

        second_case = SecondCase()

        def between_case_cleanup() -> None:
            events.append("between-case-cleanup")
            _clear_integration_auth_rate_limit_keys()

        test_runner = TimingRunner(stream=io.StringIO(), verbosity=0)
        test_runner.between_case_cleanup = between_case_cleanup
        with (
            patch.dict(os.environ, safe_environment, clear=True),
            patch("redis.asyncio.from_url", return_value=fake_redis) as redis_factory,
        ):
            result = test_runner.run(unittest.TestSuite((first_case, second_case)))
        self.assertEqual(
            events,
            [
                "first-body",
                "first-cleanup",
                "between-case-cleanup",
                "second-body",
                "between-case-cleanup",
            ],
        )
        self.assertEqual(result.testsRun, 2)
        self.assertEqual(result.between_case_cleanup_failures, 0)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(redis_factory.call_count, 2)
        self.assertTrue(
            all(
                call.args[0] == safe_environment["PLATFORM_REDIS_URL"]
                for call in redis_factory.call_args_list
            )
        )
        self.assertEqual(
            fake_redis.patterns,
            ["platform:auth-rate:v1:*"] * 4,
        )
        self.assertEqual(fake_redis.keys, {b"platform:other-cache:keep"})
        self.assertTrue(fake_redis.closed)

        failing_redis = FakeRedis()

        async def fail_scan(*, cursor: int, match: str, count: int):
            raise OSError("private Redis failure detail")

        failing_redis.scan = fail_scan
        with (
            patch.dict(os.environ, safe_environment, clear=True),
            patch("redis.asyncio.from_url", return_value=failing_redis),
            self.assertRaises(OSError),
        ):
            _clear_integration_auth_rate_limit_keys()
        self.assertTrue(failing_redis.closed)

        failing_events: list[str] = []
        failing_case = unittest.FunctionTestCase(lambda: failing_events.append("case"))
        skipped_case = unittest.FunctionTestCase(lambda: failing_events.append("unexpected"))

        def fail_closed_cleanup() -> None:
            failing_events.append("between-case-cleanup")
            raise RuntimeError("sanitized by the result hook")

        failure_runner = TimingRunner(stream=io.StringIO(), verbosity=0)
        failure_runner.between_case_cleanup = fail_closed_cleanup
        failure_result = failure_runner.run(unittest.TestSuite((failing_case, skipped_case)))
        self.assertEqual(failing_events, ["case", "between-case-cleanup"])
        self.assertEqual(failure_result.testsRun, 1)
        self.assertEqual(failure_result.between_case_cleanup_failures, 1)
        self.assertFalse(failure_result.wasSuccessful())

        runner_environment = {
            "PLATFORM_ENVIRONMENT": "test",
            "PLATFORM_DB_SCHEMA": "platform",
            "PLATFORM_DATABASE_URL": (
                "postgresql+asyncpg://u:p@127.0.0.1:5432/platformdb_test"
            ),
            "PLATFORM_REDIS_URL": "redis://127.0.0.1:6379/15",
        }

        def run_profile(contour: str, *, fail_cleanup: bool = False) -> tuple[int, list[str], int]:
            profile_events: list[str] = []
            tests: list[unittest.TestCase] = []
            catalog_cases: list[CatalogTestCase] = []
            test_count = 2 if fail_cleanup else 1
            for index in range(test_count):
                def record_case(position: int = index) -> None:
                    profile_events.append(f"case-{position}")

                synthetic_test = unittest.FunctionTestCase(
                    record_case,
                    description=f"{contour}-case-{index}",
                )
                tests.append(synthetic_test)
                catalog_cases.append(
                    CatalogTestCase(
                        test_id=synthetic_test.id(),
                        module="synthetic_runner_contract",
                        class_name="SyntheticCase",
                        method_name=f"case_{index}",
                        line=index + 1,
                        is_async=False,
                        contour="backend-unit" if contour == "backend" else contour,
                    )
                )

            def cleanup_rate_limits() -> None:
                profile_events.append("cleanup")
                if fail_cleanup:
                    raise RuntimeError("synthetic cleanup failure")

            with (
                patch.dict(os.environ, runner_environment, clear=True),
                patch("tools.platform_test_runner._require_root_identity"),
                patch("tools.platform_test_runner._privileged_preflight"),
                patch("tools.platform_test_runner.verification_resource_lock", return_value=nullcontext()),
                patch("tools.platform_test_runner._require_integration_resources_ready") as resource_preflight,
                patch("tools.platform_test_runner._select_cases", return_value=tuple(catalog_cases)),
                patch(
                    "tools.platform_test_runner._load_suite",
                    return_value=unittest.TestSuite(tests),
                ),
                patch(
                    "tools.platform_test_runner._clear_integration_auth_rate_limit_keys",
                    side_effect=cleanup_rate_limits,
                ),
                patch("tools.platform_test_runner._teardown_test_resources"),
                patch("builtins.print"),
            ):
                status = test_runner_main(["--contour", contour, "--quiet"])
            return status, profile_events, resource_preflight.call_count

        for contour in ("backend", "backend-integration"):
            with self.subTest(contour=contour):
                status, profile_events, preflight_count = run_profile(contour)
                self.assertEqual(status, 0)
                self.assertEqual(profile_events, ["case-0", "cleanup"])
                self.assertEqual(preflight_count, 1)

        unrelated_status, unrelated_events, unrelated_preflight = run_profile("backend-unit")
        self.assertEqual(unrelated_status, 0)
        self.assertEqual(unrelated_events, ["case-0"])
        self.assertEqual(unrelated_preflight, 0)

        failure_status, failure_events, failure_preflight = run_profile(
            "backend", fail_cleanup=True
        )
        self.assertEqual(failure_status, 1)
        self.assertEqual(failure_events, ["case-0", "cleanup"])
        self.assertEqual(failure_preflight, 1)

    def test_migration_target_validator_is_loopback_test_only(self) -> None:
        target = validate_disposable_migration_target(
            self._test_settings().platform_database_url,
            environment="test",
            schema="platform",
        )
        self.assertEqual(target.database_name, "platformdb_test")
        self.assertEqual(target.schema, "platform")
        for database_url, environment, schema in (
            (
                "postgresql+asyncpg://u:p@127.0.0.1:5432/platformdb",
                "production",
                "platform",
            ),
            (
                "postgresql+asyncpg://u:p@127.0.0.1:5432/platformdb_test",
                "test",
                "public",
            ),
            (
                "postgresql+asyncpg://u:p@localhost:5432/platformdb_test",
                "test",
                "platform",
            ),
        ):
            with self.subTest(database_url=database_url, environment=environment, schema=schema):
                with self.assertRaisesRegex(RuntimeError, "migration target"):
                    validate_disposable_migration_target(
                        database_url,
                        environment=environment,
                        schema=schema,
                    )

    def test_migration_subprocess_failures_are_typed_and_bounded(self) -> None:
        with self.assertRaises(MigrationCommandError):
            result = run_migration_subprocess(
                [sys.executable, "-c", "raise SystemExit(7)"],
                label="migration test failure",
                check=True,
            )
            if result.returncode == 0:
                self.fail("migration failure fixture unexpectedly succeeded")
        with self.assertRaises(MigrationCommandTimeout) as timeout:
            run_migration_subprocess(
                [sys.executable, "-c", "import time; time.sleep(1)"],
                label="migration test timeout",
                timeout_seconds=0.01,
            )
        self.assertEqual(timeout.exception.timeout_seconds, 0.01)
        diagnostic_directory, diagnostic_file = _create_private_migration_diagnostic()
        try:
            directory_stat = diagnostic_directory.stat(follow_symlinks=False)
            report_stat = diagnostic_file.stat(follow_symlinks=False)
            self.assertEqual(stat.S_IMODE(directory_stat.st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(report_stat.st_mode), 0o600)
            self.assertTrue(stat.S_ISREG(report_stat.st_mode))
            self.assertEqual(report_stat.st_nlink, 1)
            with patch.dict(
                os.environ,
                {MIGRATION_DIAGNOSTIC_ENV: str(diagnostic_file)},
            ):
                record_migration_progress("alembic-connection-opened")
                record_migration_progress("not-an-allowlisted-stage")
            self.assertRegex(
                diagnostic_file.read_text(encoding="ascii"),
                r"^stage=alembic-connection-opened monotonic_ns=\d+\n$",
            )
            linked_file = diagnostic_directory / "linked-progress.log"
            linked_file.symlink_to(diagnostic_file)
            with patch.dict(
                os.environ,
                {MIGRATION_DIAGNOSTIC_ENV: str(linked_file)},
            ):
                record_migration_progress("alembic-migrations-started")
            hard_link = diagnostic_directory / "hard-linked-progress.log"
            os.link(diagnostic_file, hard_link)
            with patch.dict(
                os.environ,
                {MIGRATION_DIAGNOSTIC_ENV: str(hard_link)},
            ):
                record_migration_progress("alembic-migrations-started")
            self.assertEqual(
                diagnostic_file.read_text(encoding="ascii").count("stage="),
                1,
            )
            bounded_file = diagnostic_directory / "bounded-progress.log"
            bounded_descriptor = os.open(
                bounded_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.write(bounded_descriptor, b"x" * _MIGRATION_DIAGNOSTIC_MAX_BYTES)
            finally:
                os.close(bounded_descriptor)
            with patch.dict(
                os.environ,
                {MIGRATION_DIAGNOSTIC_ENV: str(bounded_file)},
            ):
                record_migration_progress("alembic-migrations-started")
            self.assertEqual(
                bounded_file.stat().st_size,
                _MIGRATION_DIAGNOSTIC_MAX_BYTES,
            )
        finally:
            diagnostic_file.unlink(missing_ok=True)
            (diagnostic_directory / "linked-progress.log").unlink(missing_ok=True)
            (diagnostic_directory / "hard-linked-progress.log").unlink(missing_ok=True)
            (diagnostic_directory / "bounded-progress.log").unlink(missing_ok=True)
            diagnostic_directory.rmdir()

        from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

        class SyncConnectionSentinel:
            pass

        sync_connection = SyncConnectionSentinel()
        engine = create_async_engine("postgresql+asyncpg://test:test@127.0.0.1/test")
        sync_connect = patch.object(
            engine.sync_engine,
            "connect",
            return_value=sync_connection,
        )
        async_run_sync = patch.object(
            AsyncConnection,
            "run_sync",
            new=unittest.mock.AsyncMock(return_value=None),
        )
        async_close = patch.object(
            AsyncConnection,
            "close",
            new=unittest.mock.AsyncMock(return_value=None),
        )
        async_dispose = patch.object(
            AsyncEngine,
            "dispose",
            new=unittest.mock.AsyncMock(return_value=None),
        )
        alembic_config = SimpleNamespace(
            config_file_name=None,
            config_ini_section="alembic",
            get_main_option=lambda _key: "postgresql+asyncpg://test:test@127.0.0.1/test",
            get_section=lambda _section, _defaults: {},
            set_main_option=lambda _key, _value: None,
        )
        alembic_context = SimpleNamespace(
            config=alembic_config,
            is_offline_mode=lambda: True,
            configure=lambda **_kwargs: None,
            begin_transaction=nullcontext,
            run_migrations=lambda: None,
        )
        with (
            patch.object(importlib.import_module("alembic"), "context", alembic_context),
            patch(
                "python_packages.platform_infra.config.get_settings",
                return_value=SimpleNamespace(platform_database_url="postgresql+asyncpg://test:test@127.0.0.1/test"),
            ),
            patch("python_packages.platform_infra.config.validate_platform_settings"),
            sync_connect as sync_connect_mock,
            async_run_sync as async_run_sync_mock,
            async_close as async_close_mock,
            async_dispose as async_dispose_mock,
        ):
            migration_env = runpy.run_path(
                str(Path(__file__).parents[1] / "alembic" / "env.py")
            )
            migration_globals = migration_env["run_async_migrations"].__globals__
            migration_globals["async_engine_from_config"] = lambda *_args, **_kwargs: engine
            migration_globals["record_migration_progress"] = lambda _stage: None
            asyncio.run(migration_env["run_async_migrations"]())
        sync_connect_mock.assert_called_once_with()
        async_run_sync_mock.assert_awaited_once()
        async_close_mock.assert_awaited_once()
        async_dispose_mock.assert_awaited_once()

        captured: dict[str, object] = {}

        def capture_migration_run(
            label: str,
            command: list[str],
            **kwargs: object,
        ) -> int:
            captured.update(label=label, command=command, **kwargs)
            return 124

        with (
            patch.dict(
                os.environ,
                {MIGRATION_DIAGNOSTIC_ENV: "/tmp/untrusted-caller-progress.log"},
            ),
            patch(
                "tools.platform_verify._create_private_migration_diagnostic",
                side_effect=OSError("synthetic private report setup failure"),
            ),
            patch("tools.platform_verify._run", side_effect=capture_migration_run),
        ):
            status = dispatch("migration")
        self.assertEqual(status, 124)
        self.assertEqual(captured["label"], "migration")
        self.assertEqual(captured["timeout_seconds"], MIGRATION_SUBPROCESS_TIMEOUT_SECONDS)
        self.assertNotIn(MIGRATION_DIAGNOSTIC_ENV, captured["env"])

    def test_verifier_timeout_reaps_only_its_owned_process_group(self) -> None:
        with tempfile.TemporaryDirectory(prefix="platform-verifier-timeout-") as temp_dir:
            temp_root = Path(temp_dir)
            marker = f"verifier-timeout-{os.getpid()}-{time.monotonic_ns()}"
            child_pid_path = temp_root / "grandchild.pid"
            parent_pid_path = temp_root / "child.pid"
            progress_report = temp_root / "migration-progress.log"
            progress_descriptor = os.open(
                progress_report,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(progress_descriptor)
            child_code = (
                "import signal,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(60)"
            )
            parent_code = "\n".join(
                (
                    "import os,pathlib,subprocess,sys,time; "
                    "from tools.platform_migration_support import record_migration_progress; "
                    "record_migration_progress('schema-reset-completed')",
                    f"pathlib.Path({str(parent_pid_path)!r}).write_text(str(os.getpid()))",
                    "child=subprocess.Popen([sys.executable, '-c', "
                    f"{child_code!r}, {marker!r}])",
                    f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))",
                    "time.sleep(60)",
                )
            )
            sentinel = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                close_fds=True,
                start_new_session=True,
            )
            child_pid: int | None = None

            def child_identity(pid: int) -> tuple[str, bytes] | None:
                proc_root = Path("/proc") / str(pid)
                try:
                    state = proc_root.joinpath("stat").read_text().split()[2]
                    command_line = proc_root.joinpath("cmdline").read_bytes()
                except (OSError, IndexError):
                    return None
                return state, command_line

            try:
                stdout = io.StringIO()
                stderr = io.StringIO()
                cleanup_observations: list[bool] = []
                timeout_lifecycle: list[str] = []

                real_popen = subprocess.Popen

                def wait_for_synthetic_group_ready(
                    command: object,
                    *args: object,
                    **kwargs: object,
                ) -> subprocess.Popen[bytes]:
                    process = real_popen(command, *args, **kwargs)  # type: ignore[arg-type]
                    if command != [sys.executable, "-c", parent_code]:
                        return process
                    deadline = time.monotonic() + 3.0
                    try:
                        while time.monotonic() < deadline:
                            if parent_pid_path.is_file() and child_pid_path.is_file():
                                return process
                            if process.poll() is not None:
                                break
                            time.sleep(0.01)
                    except BaseException:
                        if kwargs.get("start_new_session"):
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        process.wait()
                        raise

                    if kwargs.get("start_new_session"):
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        try:
                            process.wait(timeout=0.1)
                        except subprocess.TimeoutExpired:
                            pass
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
                    else:
                        process.kill()
                        process.wait()
                    self.fail("synthetic timeout process group did not publish both readiness markers")

                self.assertTrue(parent_code)

                def verify_timeout_cleanup_order() -> None:
                    timeout_lifecycle.append("resource_cleanup")
                    self.assertTrue(parent_pid_path.is_file())
                    owned_process_pid = int(parent_pid_path.read_text(encoding="ascii"))
                    identity = child_identity(owned_process_pid)
                    cleanup_observations.append(
                        identity is None
                        or identity[0] == "Z"
                        or marker.encode() not in identity[1]
                    )
                    cleanup_observations.append(sentinel.poll() is None)

                def verify_timeout_diagnostic_after_reap() -> None:
                    self.assertTrue(parent_pid_path.is_file())
                    owned_process_pid = int(parent_pid_path.read_text(encoding="ascii"))
                    identity = child_identity(owned_process_pid)
                    self.assertTrue(
                        identity is None or identity[0] == "Z",
                        "timeout diagnostic ran before the owned process group was reaped",
                    )
                    self.assertIsNone(sentinel.poll(), "diagnostic touched an unrelated process")
                    timeout_lifecycle.append("diagnostic")

                with (
                    patch("tools.platform_verify.subprocess.Popen", side_effect=wait_for_synthetic_group_ready),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    status = _run(
                        "timeout process group contract",
                        [sys.executable, "-c", parent_code],
                        env={
                            **os.environ,
                            MIGRATION_DIAGNOSTIC_ENV: str(
                                temp_root / "migration-progress.log"
                            ),
                        },
                        timeout_seconds=0.2,
                        timeout_cleanup=verify_timeout_cleanup_order,
                        timeout_diagnostic=verify_timeout_diagnostic_after_reap,
                    )
                self.assertEqual(status, 124)
                self.assertIn("[GATE START] timeout process group contract", stdout.getvalue())
                self.assertIn(
                    "[GATE TIMEOUT RESOURCE CLEANUP] timeout process group contract status=passed\n",
                    stdout.getvalue(),
                )
                self.assertEqual(
                    stderr.getvalue(),
                    "[GATE TIMEOUT] timeout process group contract exceeded 0.2s\n",
                )
                self.assertEqual(cleanup_observations, [True, True])
                self.assertEqual(timeout_lifecycle, ["diagnostic", "resource_cleanup"])
                self.assertTrue(progress_report.is_file())
                self.assertEqual(
                    stat.S_IMODE(progress_report.stat().st_mode),
                    0o600,
                )
                self.assertRegex(
                    progress_report.read_text(encoding="ascii"),
                    r"^stage=schema-reset-completed monotonic_ns=\d+\n$",
                )
                self.assertIsNone(sentinel.poll(), "an unrelated session was signalled")
                self.assertTrue(child_pid_path.is_file(), "timed-out parent did not start child")
                child_pid = int(child_pid_path.read_text(encoding="ascii"))
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    identity = child_identity(child_pid)
                    if identity is None or identity[0] == "Z":
                        break
                    time.sleep(0.02)
                identity = child_identity(child_pid)
                self.assertTrue(
                    identity is None or identity[0] == "Z",
                    "ordinary grandchild survived verifier process-group timeout cleanup",
                )
                success_stdout = io.StringIO()
                with redirect_stdout(success_stdout):
                    success_status = _run(
                        "success status contract",
                        [sys.executable, "-c", "pass"],
                        timeout_seconds=2,
                    )
                self.assertEqual(success_status, 0)
                self.assertIn("[GATE PASS] success status contract", success_stdout.getvalue())

                failed_cleanup_stdout = io.StringIO()
                failed_cleanup_stderr = io.StringIO()

                def fail_timeout_cleanup() -> None:
                    raise RuntimeError("private synthetic cleanup failure")

                with redirect_stdout(failed_cleanup_stdout), redirect_stderr(failed_cleanup_stderr):
                    failed_cleanup_status = _run(
                        "timeout cleanup failure contract",
                        [sys.executable, "-c", "import time; time.sleep(60)"],
                        timeout_seconds=0.05,
                        timeout_cleanup=fail_timeout_cleanup,
                    )
                self.assertEqual(failed_cleanup_status, 124)
                self.assertIn(
                    "[GATE TIMEOUT RESOURCE CLEANUP FAIL] "
                    "timeout cleanup failure contract class=RuntimeError\n",
                    failed_cleanup_stderr.getvalue(),
                )
                self.assertIn(
                    "[GATE TIMEOUT] timeout cleanup failure contract exceeded 0.05s\n",
                    failed_cleanup_stderr.getvalue(),
                )
                self.assertNotIn("private synthetic cleanup failure", failed_cleanup_stderr.getvalue())
            finally:
                if child_pid is not None:
                    identity = child_identity(child_pid)
                    if identity is not None and marker.encode() in identity[1] and identity[0] != "Z":
                        os.kill(child_pid, signal.SIGKILL)
                sentinel.kill()
                sentinel.wait()

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = _run(
                "nonzero status contract",
                [sys.executable, "-c", "raise SystemExit(7)"],
                timeout_seconds=2,
            )
        self.assertEqual(status, 7)
        self.assertIn("[GATE FAIL] nonzero status contract (exit 7)", stderr.getvalue())

    def test_verifier_term_cleans_owned_group_and_preserves_signal_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="platform-verifier-term-") as temp_dir:
            temp_root = Path(temp_dir)
            marker = f"verifier-term-{os.getpid()}-{time.monotonic_ns()}"
            child_pid_path = temp_root / "grandchild.pid"
            child_code = (
                "import signal,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(60)"
            )
            command_code = "\n".join(
                (
                    "import pathlib,subprocess,sys,time",
                    "child=subprocess.Popen([sys.executable, '-c', "
                    f"{child_code!r}, {marker!r}])",
                    f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))",
                    "time.sleep(60)",
                )
            )
            verifier_code = (
                "import sys; from tools.platform_verify import _run; "
                "sys.exit(_run('parent SIGTERM contract', "
                f"[sys.executable, '-c', {command_code!r}], timeout_seconds=30))"
            )
            verifier = subprocess.Popen(
                [sys.executable, "-c", verifier_code],
                cwd=Path.cwd(),
                close_fds=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            sentinel = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                close_fds=True,
                start_new_session=True,
            )
            child_pid: int | None = None

            def child_identity(pid: int) -> tuple[str, bytes] | None:
                proc_root = Path("/proc") / str(pid)
                try:
                    state = proc_root.joinpath("stat").read_text().split()[2]
                    command_line = proc_root.joinpath("cmdline").read_bytes()
                except (OSError, IndexError):
                    return None
                return state, command_line

            try:
                deadline = time.monotonic() + 3.0
                while not child_pid_path.exists() and time.monotonic() < deadline:
                    if verifier.poll() is not None:
                        self.fail("verifier exited before the nested child started")
                    time.sleep(0.02)
                self.assertTrue(child_pid_path.is_file(), "verifier did not start nested child")
                child_pid = int(child_pid_path.read_text(encoding="ascii"))
                identity = child_identity(child_pid)
                self.assertIsNotNone(identity, "nested child disappeared before cancellation")
                assert identity is not None
                self.assertIn(marker.encode(), identity[1])

                os.kill(verifier.pid, signal.SIGTERM)
                self.assertEqual(verifier.wait(timeout=5), 128 + signal.SIGTERM)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    identity = child_identity(child_pid)
                    if identity is None or identity[0] == "Z":
                        break
                    time.sleep(0.02)
                identity = child_identity(child_pid)
                self.assertTrue(
                    identity is None or identity[0] == "Z",
                    "nested child survived parent SIGTERM cleanup",
                )
                self.assertIsNone(sentinel.poll(), "an unrelated session was signalled")
            finally:
                if verifier.poll() is None:
                    verifier.kill()
                    verifier.wait()
                if child_pid is not None:
                    identity = child_identity(child_pid)
                    if identity is not None and marker.encode() in identity[1] and identity[0] != "Z":
                        os.kill(child_pid, signal.SIGKILL)
                sentinel.kill()
                sentinel.wait()

    def test_verifier_term_after_child_exit_cleans_before_reap(self) -> None:
        with tempfile.TemporaryDirectory(prefix="platform-verifier-late-term-") as temp_dir:
            temp_root = Path(temp_dir)
            marker = f"verifier-late-term-{os.getpid()}-{time.monotonic_ns()}"
            child_pid_path = temp_root / "grandchild.pid"
            child_code = (
                "import signal,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(60)"
            )
            command_code = "\n".join(
                (
                    "import pathlib,subprocess,sys",
                    "child=subprocess.Popen([sys.executable, '-c', "
                    f"{child_code!r}, {marker!r}])",
                    f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))",
                )
            )
            verifier_code = "\n".join(
                (
                    "import os,signal,sys",
                    "import tools.platform_verify as verifier_module",
                    "wait_for_exit=verifier_module._wait_for_owned_process_exit",
                    "def inject_term_before_reap(process, timeout):",
                    "    wait_for_exit(process, timeout)",
                    "    os.kill(os.getpid(), signal.SIGTERM)",
                    "verifier_module._wait_for_owned_process_exit=inject_term_before_reap",
                    "sys.exit(verifier_module._run('late parent SIGTERM contract', "
                    f"[sys.executable, '-c', {command_code!r}], timeout_seconds=5))",
                )
            )
            verifier = subprocess.Popen(
                [sys.executable, "-c", verifier_code],
                cwd=Path.cwd(),
                close_fds=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            sentinel = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                close_fds=True,
                start_new_session=True,
            )
            child_pid: int | None = None

            def child_identity(pid: int) -> tuple[str, bytes] | None:
                proc_root = Path("/proc") / str(pid)
                try:
                    state = proc_root.joinpath("stat").read_text().split()[2]
                    command_line = proc_root.joinpath("cmdline").read_bytes()
                except (OSError, IndexError):
                    return None
                return state, command_line

            try:
                self.assertEqual(verifier.wait(timeout=5), 128 + signal.SIGTERM)
                self.assertTrue(child_pid_path.is_file(), "gate child did not start its descendant")
                child_pid = int(child_pid_path.read_text(encoding="ascii"))
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    identity = child_identity(child_pid)
                    if identity is None or identity[0] == "Z":
                        break
                    time.sleep(0.02)
                identity = child_identity(child_pid)
                self.assertTrue(
                    identity is None or identity[0] == "Z",
                    "descendant survived cancellation after direct-child exit",
                )
                self.assertIsNone(sentinel.poll(), "an unrelated session was signalled")
            finally:
                if verifier.poll() is None:
                    verifier.kill()
                    verifier.wait()
                if child_pid is not None:
                    identity = child_identity(child_pid)
                    if identity is not None and marker.encode() in identity[1] and identity[0] != "Z":
                        os.kill(child_pid, signal.SIGKILL)
                sentinel.kill()
                sentinel.wait()
        self.assertEqual(MIGRATION_SUBPROCESS_TIMEOUT_SECONDS, 180.0)

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
            with self.assertRaises(SystemExit):
                _require_redis_db15_ready(contour="backend-privileged")
        self.assertFalse(any(name == "sqlalchemy" or name.startswith("redis") for name in imported))

        self.assertTrue(CONTOUR_METADATA["backend-privileged"]["requires_redis"])
        workflow = (
            Path(__file__).resolve().parents[2]
            / ".github/workflows/platform-security.yml"
        ).read_text(encoding="utf-8")
        privileged_job = re.search(
            r"^  backend-privileged:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            workflow,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(privileged_job)
        assert privileged_job is not None
        self.assertRegex(privileged_job.group("body"), r"(?m)^\s+redis:\n")
        self.assertIn("image: redis:7", privileged_job.group("body"))
        self.assertIn("- 6379:6379", privileged_job.group("body"))
        self.assertIn('--health-cmd="redis-cli ping"', privileged_job.group("body"))
        privileged_body = privileged_job.group("body")
        self.assertIn("Run synthetic restore diagnostic first on feature dispatches", privileged_body)
        self.assertNotIn("platform-backup-scratch-diagnostic", privileged_body)
        self.assertNotIn("Require focused scratch restore diagnostic", privileged_body)
        workflow_data = yaml.safe_load(workflow)
        workflow_triggers = workflow_data.get("on", workflow_data.get(True, {}))
        dispatch_inputs = workflow_triggers["workflow_dispatch"]["inputs"]
        self.assertEqual(
            dispatch_inputs["scratch_backend"],
            {
                "description": "Synthetic PG16 restore diagnostic backend (standard non-dev dispatch only)",
                "required": True,
                "default": "tmpfs_v1",
                "type": "choice",
                "options": ["tmpfs_v1", "disk_ext4_image_v1"],
            },
        )
        privileged_steps = workflow_data["jobs"]["backend-privileged"]["steps"]
        focused_step = next(
            step for step in privileged_steps
            if step.get("name") == "Run synthetic restore diagnostic first on feature dispatches"
        )
        self.assertEqual(
            focused_step["if"],
            "${{ github.event_name == 'workflow_dispatch' && inputs.proof_mode == 'standard' && github.ref != 'refs/heads/dev' }}",
        )
        self.assertIn(
            "--focused tests.test_platform_backup_restore_drill.PlatformBackupRestoreDrillTests.test_synthetic_pg16_restore_diagnostic_under_root",
            focused_step["run"],
        )
        backend_expression = (
            "${{ github.event_name == 'workflow_dispatch' && inputs.proof_mode == 'standard' "
            "&& github.ref != 'refs/heads/dev' && inputs.scratch_backend == 'disk_ext4_image_v1' "
            "&& 'disk_ext4_image_v1' || 'tmpfs_v1' }}"
        )
        self.assertEqual(focused_step["env"]["PLATFORM_SCRATCH_BACKEND"], backend_expression)
        self.assertIn('PLATFORM_SCRATCH_BACKEND="$PLATFORM_SCRATCH_BACKEND"', focused_step["run"])

        def effective_backend(event: str, proof_mode: str, ref: str, requested: str) -> str:
            if (
                event == "workflow_dispatch"
                and proof_mode == "standard"
                and ref != "refs/heads/dev"
                and requested == "disk_ext4_image_v1"
            ):
                return "disk_ext4_image_v1"
            return "tmpfs_v1"

        for event, proof_mode, ref, requested, expected_backend in (
            ("workflow_dispatch", "standard", "refs/heads/feature/probe", "disk_ext4_image_v1", "disk_ext4_image_v1"),
            ("workflow_dispatch", "standard", "refs/heads/feature/probe", "tmpfs_v1", "tmpfs_v1"),
            ("workflow_dispatch", "standard", "refs/heads/dev", "disk_ext4_image_v1", "tmpfs_v1"),
            ("workflow_dispatch", "baseline-runtime", "refs/heads/feature/probe", "disk_ext4_image_v1", "tmpfs_v1"),
            ("push", "standard", "refs/heads/dev", "disk_ext4_image_v1", "tmpfs_v1"),
            ("pull_request", "standard", "refs/pull/12/merge", "disk_ext4_image_v1", "tmpfs_v1"),
        ):
            with self.subTest(event=event, proof_mode=proof_mode, ref=ref):
                self.assertEqual(
                    effective_backend(event, proof_mode, ref, requested), expected_backend
                )
        self.assertNotIn("continue-on-error", focused_step)
        focused_cleanup = next(
            step for step in privileged_steps
            if step.get("name") == "Remove only the focused synthetic reports before the full contour"
        )
        self.assertIn(
            "synthetic-restore-diagnostic.json",
            focused_cleanup["run"],
        )
        self.assertIn(
            "synthetic-restore-diagnostic-failure.json",
            focused_cleanup["run"],
        )
        self.assertLess(privileged_steps.index(focused_step), privileged_steps.index(focused_cleanup))
        full_step = next(
            step for step in privileged_steps
            if step.get("name") == "Run serial privileged catalog contour exactly once as root"
        )
        self.assertLess(privileged_steps.index(focused_cleanup), privileged_steps.index(full_step))
        self.assertIn("success()", full_step["if"])
        self.assertIn("steps.synthetic_restore_first.outcome != 'failure'", full_step["if"])
        self.assertIn("platform_verify.py backend-privileged", full_step["run"])
        self.assertEqual(full_step["env"]["PLATFORM_SCRATCH_BACKEND"], backend_expression)
        self.assertIn('PLATFORM_SCRATCH_BACKEND="$PLATFORM_SCRATCH_BACKEND"', full_step["run"])
        self.assertIn("--manifest ci-backend/backend-privileged/manifest.json", full_step["run"])
        self.assertIn("--summary ci-backend/backend-privileged/summary.json", full_step["run"])
        self.assertIn("Upload backend privileged manifests", privileged_body)
        self.assertNotIn("continue-on-error", full_step)
        self.assertEqual(
            sum(step.get("name") == "Upload backend privileged manifests and synthetic restore diagnostics" for step in privileged_steps),
            1,
        )
        upload_step = next(
            step for step in privileged_steps
            if step.get("name") == "Upload backend privileged manifests and synthetic restore diagnostics"
        )
        self.assertEqual(upload_step["if"], "${{ always() }}")
        self.assertGreater(privileged_steps.index(upload_step), privileged_steps.index(full_step))
        restore_test = next(
            case for case in discover_test_cases()
            if case.module == "test_platform_backup_restore_drill"
            and case.method_name == "test_isolated_restore_signal_cleanup_under_root"
        )
        self.assertEqual(restore_test.contour, "backend-privileged")
        self.assertNotIn(restore_test.test_id, INTENTIONAL_SKIP_IDS)
        self.assertIn("backend-privileged", workflow)
        self.assertIn("Verify every backend component result and catalog union", workflow)
        self.assertIn("--component-dir", workflow)
        duplicate_full_gate = workflow.replace(
            "          .venv_platform/bin/python tools/platform_verify.py backend-privileged\n",
            "          .venv_platform/bin/python tools/platform_verify.py backend-privileged\n"
            "          .venv_platform/bin/python tools/platform_verify.py backend-privileged\n",
            1,
        )
        self.assertTrue(
            any(
                "backend job backend-privileged must invoke backend-privileged exactly once" in issue
                for issue in _backend_workflow_issues(duplicate_full_gate)
            )
        )
        resumable_full_gate = workflow.replace(
            "      - name: Run serial privileged catalog contour exactly once as root\n",
            "      - name: Run serial privileged catalog contour exactly once as root\n"
            "        continue-on-error: true\n",
            1,
        )
        self.assertTrue(
            any(
                "backend job backend-privileged must not continue on error" in issue
                for issue in _backend_workflow_issues(resumable_full_gate)
            )
        )
        safe_environment = {
            "PLATFORM_ENVIRONMENT": "test",
            "PLATFORM_DB_SCHEMA": "platform",
            "PLATFORM_DATABASE_URL": (
                "postgresql+asyncpg://u:p@127.0.0.1:5432/platformdb_test"
            ),
            "PLATFORM_REDIS_URL": "redis://127.0.0.1:6379/15",
        }

        class FakeRedis:
            def __init__(self) -> None:
                self.keys = {b"purge-test-key"}
                self.ping_count = 0
                self.closed = False

            async def ping(self) -> bool:
                self.ping_count += 1
                return True

            async def flushdb(self) -> bool:
                self.keys.clear()
                return True

            async def dbsize(self) -> int:
                return len(self.keys)

            async def aclose(self) -> None:
                self.closed = True

        fake_redis = FakeRedis()
        with (
            patch.dict(os.environ, safe_environment, clear=True),
            patch(
                "redis.asyncio.from_url",
                return_value=fake_redis,
            ) as from_url,
        ):
            _require_redis_db15_ready(contour="backend-privileged")
            _teardown_privileged_redis_resource()
        self.assertEqual(from_url.call_count, 2)
        self.assertEqual(
            from_url.call_args_list[0].args,
            ("redis://127.0.0.1:6379/15",),
        )
        self.assertEqual(from_url.call_args_list[0].kwargs, {"decode_responses": False})
        self.assertEqual(fake_redis.ping_count, 2)
        self.assertEqual(fake_redis.keys, set())
        self.assertTrue(fake_redis.closed)

        invalid_redis_environment = {
            **safe_environment,
            "PLATFORM_REDIS_URL": "redis://127.0.0.1:6379/0",
        }
        imported.clear()
        with (
            patch.dict(os.environ, invalid_redis_environment, clear=True),
            patch("builtins.__import__", side_effect=recording_import),
        ):
            with self.assertRaises(SystemExit):
                _require_redis_db15_ready(contour="backend-privileged")
            with self.assertRaises(RuntimeError):
                _teardown_privileged_redis_resource()
        self.assertFalse(any(name.startswith("redis") for name in imported))

        # The verifier resolves dotenv once and passes the same validated
        # mapping to both the child and post-timeout Redis cleanup. A file
        # value takes precedence over an inherited value, matching the shell
        # runner, while invalid file content blocks before process creation.
        with tempfile.TemporaryDirectory(prefix="platform-privileged-env-") as temp_dir:
            env_path = Path(temp_dir) / ".env.platform"
            env_path.write_text(
                "PLATFORM_ENVIRONMENT=test\n"
                "PLATFORM_DB_SCHEMA=platform\n"
                "PLATFORM_DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:5432/platformdb_test\n"
                "PLATFORM_REDIS_URL=redis://127.0.0.1:6379/15\n",
                encoding="utf-8",
            )
            env_path.chmod(0o600)
            inherited = {
                "PLATFORM_ENV_FILE": str(env_path),
                "PLATFORM_ENVIRONMENT": "test",
                "PLATFORM_DB_SCHEMA": "platform",
                "PLATFORM_DATABASE_URL": (
                    "postgresql+asyncpg://u:p@remote.invalid:5432/platformdb_test"
                ),
                "PLATFORM_REDIS_URL": "redis://127.0.0.1:6379/0",
                "GITHUB_ACTIONS": "true",
                "RUNNER_OS": "Linux",
                "RUNNER_ENVIRONMENT": "github-hosted",
            }
            checkout_root = Path(temp_dir) / "checkout"
            pinned_python = checkout_root / ".venv_platform/bin/python"
            pinned_python.parent.mkdir(parents=True)
            pinned_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            pinned_python.chmod(0o755)

            def fake_tool(name: str) -> str:
                return str(checkout_root / "tools" / name)

            observed: dict[str, object] = {}
            progress_capture = {"mode": "none"}

            def capture_run(label: str, command: object, **kwargs: object) -> int:
                observed["label"] = label
                observed["command"] = command
                observed["kwargs"] = kwargs
                callback = kwargs.get("timeout_cleanup")
                self.assertTrue(callable(callback))
                diagnostic = kwargs.get("timeout_diagnostic")
                mode = progress_capture["mode"]
                if mode != "none":
                    self.assertIsInstance(command, list)
                    assert isinstance(command, list)
                    self.assertIn("--progress", command)
                    progress_path = Path(command[command.index("--progress") + 1])
                    if "--summary" in command:
                        orphan_temp = progress_path.parent / f".{progress_path.name}.orphan.tmp"
                        orphan_temp.write_text("partial", encoding="ascii")
                        orphan_temp.chmod(0o600)
                        unrelated_sibling = progress_path.parent / "keep-unrelated.txt"
                        unrelated_sibling.write_text("keep", encoding="ascii")
                        unrelated_sibling.chmod(0o600)
                        observed["orphan_temp"] = orphan_temp
                        observed["unrelated_sibling"] = unrelated_sibling
                    cases = sorted(
                        cases_for_contour("backend-privileged"),
                        key=lambda case: case.test_id,
                    )
                    expected_ids = [case.test_id for case in cases]
                    active_test_id = expected_ids[1]
                    if mode == "invalid":
                        active_test_id = expected_ids[0]
                    _write_progress_json(
                        progress_path,
                        {
                            "schema": 1,
                            "contour": "backend-privileged",
                            "status": "diagnostic_only",
                            "execution_complete": False,
                            "selected_count": len(expected_ids),
                            "expected_ids": expected_ids,
                            "completed_timings": [
                                {
                                    "test_id": expected_ids[0],
                                    "duration_ms": 12.5,
                                    "outcome": "passed",
                                }
                            ],
                            "active_test_id": active_test_id,
                        },
                    )
                    observed["progress_path"] = progress_path
                    self.assertTrue(callable(diagnostic))
                    diagnostic()
                    callback()
                    return 124
                callback()
                return 0

            with (
                patch.dict(os.environ, inherited, clear=True),
                patch("tools.platform_verify._require_backend_privileged_root"),
                patch("tools.platform_verify.PLATFORM_ROOT", checkout_root),
                patch("tools.platform_verify._tool", side_effect=fake_tool),
                patch("tools.platform_verify._run", side_effect=capture_run),
                patch(
                    "tools.platform_verify._cleanup_timed_out_backend_privileged_resources"
                ) as cleanup,
            ):
                self.assertEqual(dispatch("release-runtime"), 0)
            kwargs = observed["kwargs"]
            assert isinstance(kwargs, dict)
            child_env = kwargs["env"]
            self.assertIsInstance(child_env, dict)
            assert isinstance(child_env, dict)
            self.assertEqual(
                child_env["PLATFORM_DATABASE_URL"],
                "postgresql+asyncpg://u:p@127.0.0.1:5432/platformdb_test",
            )
            self.assertEqual(
                observed["command"],
                [
                    str(pinned_python),
                    fake_tool("platform_test_runner.py"),
                    "--contour",
                    "backend-privileged",
                    "--focused",
                    *RELEASE_RUNTIME_TEST_IDS,
                ],
            )
            self.assertEqual(kwargs["timeout_seconds"], 600)
            cleanup.assert_called_once_with(
                {
                    "platform_environment": "test",
                    "platform_database_url": (
                        "postgresql+asyncpg://u:p@127.0.0.1:5432/platformdb_test"
                    ),
                    "platform_db_schema": "platform",
                    "platform_redis_url": "redis://127.0.0.1:6379/15",
                }
            )
            observed.clear()
            with (
                patch.dict(os.environ, inherited, clear=True),
                patch("tools.platform_verify._require_backend_privileged_root"),
                patch("tools.platform_verify.PLATFORM_ROOT", checkout_root),
                patch("tools.platform_verify._tool", side_effect=fake_tool),
                patch("tools.platform_verify._run", side_effect=capture_run),
                patch(
                    "tools.platform_verify._cleanup_timed_out_backend_privileged_resources"
                ) as cleanup,
            ):
                self.assertEqual(dispatch("backend-privileged"), 0)
            command = observed["command"]
            self.assertEqual(
                command[:4],
                [
                    str(pinned_python),
                    fake_tool("platform_test_runner.py"),
                    "--contour",
                    "backend-privileged",
                ],
            )
            self.assertEqual(command[4], "--progress")
            self.assertEqual(len(command), 6)
            backend_kwargs = observed["kwargs"]
            assert isinstance(backend_kwargs, dict)
            self.assertEqual(backend_kwargs["env"]["PLATFORM_DATABASE_URL"], safe_environment["PLATFORM_DATABASE_URL"])
            cleanup.assert_called_once_with(
                {
                    "platform_environment": "test",
                    "platform_database_url": safe_environment["PLATFORM_DATABASE_URL"],
                    "platform_db_schema": "platform",
                    "platform_redis_url": "redis://127.0.0.1:6379/15",
                }
            )

            observed.clear()
            with (
                patch.dict(os.environ, inherited, clear=True),
                patch("tools.platform_verify._require_backend_privileged_root"),
                patch("tools.platform_verify.PLATFORM_ROOT", checkout_root),
                patch("tools.platform_verify._tool", side_effect=fake_tool),
                patch("tools.platform_verify._run", side_effect=capture_run),
                patch(
                    "tools.platform_verify._cleanup_timed_out_backend_privileged_resources"
                ) as cleanup,
            ):
                self.assertEqual(
                    dispatch(
                        "backend-privileged",
                        ["--", "--focused", "tests.synthetic.Owner.test_case"],
                    ),
                    0,
                )
            command = observed["command"]
            self.assertEqual(command[0], str(pinned_python))
            self.assertEqual(
                command[4:6],
                ["--focused", "tests.synthetic.Owner.test_case"],
            )
            self.assertEqual(command[6], "--progress")
            self.assertEqual(len(command), 8)
            backend_kwargs = observed["kwargs"]
            assert isinstance(backend_kwargs, dict)
            self.assertEqual(backend_kwargs["timeout_seconds"], CONTOUR_TIMEOUT_SECONDS["backend-privileged"])
            cleanup.assert_called_once()

            progress_capture["mode"] = "valid"
            timeout_stderr = io.StringIO()
            with (
                redirect_stderr(timeout_stderr),
                patch.dict(os.environ, inherited, clear=True),
                patch("tools.platform_verify._require_backend_privileged_root"),
                patch("tools.platform_verify.PLATFORM_ROOT", checkout_root),
                patch("tools.platform_verify._tool", side_effect=fake_tool),
                patch("tools.platform_verify._run", side_effect=capture_run),
                patch(
                    "tools.platform_verify._cleanup_timed_out_backend_privileged_resources"
                ) as cleanup,
            ):
                self.assertEqual(dispatch("backend-privileged"), 124)
            progress_command = observed["command"]
            assert isinstance(progress_command, list)
            progress_path = Path(
                progress_command[progress_command.index("--progress") + 1]
            )
            progress_cases = sorted(
                cases_for_contour("backend-privileged"),
                key=lambda case: case.test_id,
            )
            self.assertIn(
                f"selected={len(progress_cases)} completed=1 "
                f"active_test_id={progress_cases[1].test_id} "
                f"last_completed_test_id={progress_cases[0].test_id}",
                timeout_stderr.getvalue(),
            )
            self.assertNotIn("duration_ms", timeout_stderr.getvalue())
            self.assertFalse(progress_path.exists())
            self.assertFalse(progress_path.parent.exists())
            cleanup.assert_called_once()

            progress_capture["mode"] = "invalid"
            unavailable_stderr = io.StringIO()
            with (
                redirect_stderr(unavailable_stderr),
                patch.dict(os.environ, inherited, clear=True),
                patch("tools.platform_verify._require_backend_privileged_root"),
                patch("tools.platform_verify.PLATFORM_ROOT", checkout_root),
                patch("tools.platform_verify._tool", side_effect=fake_tool),
                patch("tools.platform_verify._run", side_effect=capture_run),
                patch(
                    "tools.platform_verify._cleanup_timed_out_backend_privileged_resources"
                ),
            ):
                self.assertEqual(dispatch("backend-privileged"), 124)
            invalid_progress_path = observed["progress_path"]
            self.assertIsInstance(invalid_progress_path, Path)
            assert isinstance(invalid_progress_path, Path)
            self.assertIn(
                "backend-privileged progress=unavailable",
                unavailable_stderr.getvalue(),
            )
            self.assertFalse(invalid_progress_path.exists())
            self.assertFalse(invalid_progress_path.parent.exists())

            progress_capture["mode"] = "valid"
            explicit_summary = (
                checkout_root / "ci-backend" / "backend-privileged" / "summary.json"
            )
            with (
                patch.dict(os.environ, inherited, clear=True),
                patch("tools.platform_verify._require_backend_privileged_root"),
                patch("tools.platform_verify.PLATFORM_ROOT", checkout_root),
                patch("tools.platform_verify._tool", side_effect=fake_tool),
                patch("tools.platform_verify._run", side_effect=capture_run),
                patch(
                    "tools.platform_verify._cleanup_timed_out_backend_privileged_resources"
                ),
            ):
                self.assertEqual(
                    dispatch(
                        "backend-privileged",
                        ["--summary", str(explicit_summary)],
                    ),
                    124,
                )
            explicit_progress_path = observed["progress_path"]
            self.assertIsInstance(explicit_progress_path, Path)
            assert isinstance(explicit_progress_path, Path)
            self.assertTrue(explicit_progress_path.is_file())
            self.assertEqual(
                stat.S_IMODE(explicit_progress_path.stat().st_mode),
                0o600,
            )
            self.assertEqual(
                json.loads(explicit_progress_path.read_text(encoding="utf-8"))["status"],
                "diagnostic_only",
            )
            orphan_temp = observed["orphan_temp"]
            unrelated_sibling = observed["unrelated_sibling"]
            self.assertIsInstance(orphan_temp, Path)
            self.assertIsInstance(unrelated_sibling, Path)
            assert isinstance(orphan_temp, Path)
            assert isinstance(unrelated_sibling, Path)
            self.assertFalse(orphan_temp.exists())
            self.assertTrue(unrelated_sibling.is_file())

            progress_capture["mode"] = "invalid"
            invalid_summary = (
                checkout_root / "ci-backend" / "backend-privileged" / "invalid-summary.json"
            )
            with (
                redirect_stderr(io.StringIO()),
                patch.dict(os.environ, inherited, clear=True),
                patch("tools.platform_verify._require_backend_privileged_root"),
                patch("tools.platform_verify.PLATFORM_ROOT", checkout_root),
                patch("tools.platform_verify._tool", side_effect=fake_tool),
                patch("tools.platform_verify._run", side_effect=capture_run),
                patch(
                    "tools.platform_verify._cleanup_timed_out_backend_privileged_resources"
                ),
            ):
                self.assertEqual(
                    dispatch(
                        "backend-privileged",
                        ["--summary", str(invalid_summary)],
                    ),
                    124,
                )
            invalid_explicit_path = observed["progress_path"]
            invalid_orphan_temp = observed["orphan_temp"]
            self.assertIsInstance(invalid_explicit_path, Path)
            self.assertIsInstance(invalid_orphan_temp, Path)
            assert isinstance(invalid_explicit_path, Path)
            assert isinstance(invalid_orphan_temp, Path)
            self.assertFalse(invalid_explicit_path.exists())
            self.assertFalse(invalid_orphan_temp.exists())
            self.assertTrue(unrelated_sibling.is_file())

            pinned_python.chmod(0o600)
            with (
                patch.dict(os.environ, inherited, clear=True),
                patch("tools.platform_verify._require_backend_privileged_root"),
                patch("tools.platform_verify.PLATFORM_ROOT", checkout_root),
                patch("tools.platform_verify._tool", side_effect=fake_tool),
                patch("tools.platform_verify._run") as child_run,
            ):
                with self.assertRaisesRegex(VerificationError, "Python runtime is unavailable"):
                    dispatch("backend-privileged")
            child_run.assert_not_called()
            pinned_python.chmod(0o755)

            with (
                patch.dict(os.environ, inherited, clear=True),
                patch("tools.platform_verify._require_backend_privileged_root"),
                patch("tools.platform_verify.PLATFORM_ROOT", checkout_root),
                patch("tools.platform_verify._tool", side_effect=fake_tool),
                patch("tools.platform_verify._run") as child_run,
            ):
                with self.assertRaises(VerificationError):
                    dispatch("backend-privileged", ["--unknown"])
            child_run.assert_not_called()

            with (
                patch.dict(
                    os.environ,
                    {
                        "PLATFORM_ENV_FILE": str(env_path),
                        "PLATFORM_TEST_AGGREGATE_ONLY": "1",
                        "PLATFORM_PYTHON_BIN": "",
                        **safe_environment,
                    },
                    clear=True,
                ),
                patch("tools.platform_verify.PLATFORM_ROOT", checkout_root),
                patch("tools.platform_verify._run") as child_run,
            ):
                aggregate_env, aggregate_settings = _validated_privileged_environment()
                self.assertEqual(aggregate_env["PLATFORM_PYTHON_BIN"], "/usr/bin/python3")
                self.assertEqual(_privileged_runner_python(aggregate_env), "/usr/bin/python3")
                self.assertEqual(
                    aggregate_settings["platform_redis_url"],
                    "redis://127.0.0.1:6379/15",
                )
            with (
                patch.dict(
                    os.environ,
                    {"PLATFORM_ENV_FILE": str(env_path)},
                    clear=True,
                ),
                patch("tools.platform_verify.os.geteuid", return_value=1000),
                patch("tools.platform_verify._run") as child_run,
            ):
                with self.assertRaisesRegex(VerificationError, "root test user"):
                    dispatch("release-runtime")
            child_run.assert_not_called()

            env_path.write_text(
                "PLATFORM_ENVIRONMENT=test\n"
                "PLATFORM_DB_SCHEMA=platform\n"
                "PLATFORM_DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:5432/platformdb_test\n"
                "PLATFORM_REDIS_URL=redis://127.0.0.1:6379/0\n",
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, inherited, clear=True),
                patch("tools.platform_verify._require_backend_privileged_root"),
                patch("tools.platform_verify._run") as child_run,
            ):
                with self.assertRaises(VerificationError):
                    dispatch("release-runtime")
            child_run.assert_not_called()

        safe_resource_settings = {
            "platform_environment": "test",
            "platform_database_url": safe_environment["PLATFORM_DATABASE_URL"],
            "platform_db_schema": "platform",
            "platform_redis_url": "redis://127.0.0.1:6379/15",
        }
        invalid_resource_settings = {
            **safe_resource_settings,
            "platform_redis_url": "redis://127.0.0.1:6379/0",
        }
        imported.clear()
        with (
            patch.dict(os.environ, safe_environment, clear=True),
            patch("builtins.__import__", side_effect=recording_import),
        ):
            with self.assertRaises(RuntimeError):
                _teardown_privileged_redis_resource(invalid_resource_settings)
        self.assertFalse(any(name.startswith("redis") for name in imported))

        lock_events: list[str] = []

        @contextmanager
        def parent_resource_lock(contour: str):
            lock_events.append(f"lock:{contour}:enter")
            try:
                yield
            finally:
                lock_events.append(f"lock:{contour}:exit")

        with (
            patch.dict(os.environ, safe_environment, clear=True),
            patch(
                "tools.platform_verification_lock.verification_resource_lock",
                side_effect=parent_resource_lock,
            ) as parent_lock,
            patch(
                "tools.platform_test_runner._teardown_privileged_redis_resource",
                side_effect=lambda _settings: lock_events.append("redis-db15-teardown"),
            ) as parent_teardown,
        ):
            _cleanup_timed_out_backend_privileged_resources(safe_resource_settings)
        parent_lock.assert_called_once_with("backend-privileged")
        parent_teardown.assert_called_once_with(safe_resource_settings)
        self.assertEqual(
            lock_events,
            [
                "lock:backend-privileged:enter",
                "redis-db15-teardown",
                "lock:backend-privileged:exit",
            ],
        )

        lock_events.clear()
        imported.clear()
        with (
            patch.dict(os.environ, invalid_redis_environment, clear=True),
            patch(
                "tools.platform_verification_lock.verification_resource_lock",
                side_effect=parent_resource_lock,
            ),
            patch("builtins.__import__", side_effect=recording_import),
        ):
            with self.assertRaises(RuntimeError):
                _cleanup_timed_out_backend_privileged_resources(
                    invalid_resource_settings
                )
        self.assertEqual(lock_events, ["lock:backend-privileged:enter", "lock:backend-privileged:exit"])
        self.assertFalse(any(name.startswith("redis") for name in imported))

        with (
            patch.dict(os.environ, safe_environment, clear=True),
            patch(
                "tools.platform_test_runner.verification_resource_lock",
                return_value=nullcontext(),
            ) as resource_lock,
            patch("tools.platform_test_runner._select_cases", return_value=()),
            patch(
                "tools.platform_test_runner._load_suite",
                return_value=unittest.TestSuite(),
            ),
            patch("tools.platform_test_runner._require_root_identity"),
            patch("tools.platform_test_runner._require_redis_db15_ready") as runner_preflight,
            patch("tools.platform_test_runner._teardown_privileged_redis_resource") as runner_cleanup,
            patch("builtins.print"),
        ):
            self.assertEqual(
                test_runner_main(["--contour", "backend-privileged", "--focused", "probe"]),
                0,
            )
        resource_lock.assert_called_once_with("backend-privileged")
        runner_preflight.assert_called_once_with(contour="backend-privileged")
        runner_cleanup.assert_called_once_with()

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

        class ProgressCase(unittest.TestCase):
            def test_visible_progress(self) -> None:
                self.assertTrue(True)

        progress_test = ProgressCase("test_visible_progress")
        catalog_case = CatalogTestCase(
            test_id=progress_test.id(),
            module=ProgressCase.__module__,
            class_name="ProgressCase",
            method_name="test_visible_progress",
            line=1,
            is_async=False,
            contour=VERIFICATION_CONTOUR,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, safe_environment, clear=True),
            patch("tools.platform_test_runner._select_cases", return_value=(catalog_case,)),
            patch(
                "tools.platform_test_runner._load_suite",
                return_value=unittest.TestSuite((progress_test,)),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(
                test_runner_main(
                    ["--contour", "verification-contract", "--focused", catalog_case.test_id]
                ),
                0,
            )
        self.assertIn("test_visible_progress", stderr.getvalue())

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
            marker_dir.chmod(0o733)
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
            probe.chmod(0o644)

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
                    _trusted_runuser(),
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
            lock_path.chmod(LOCK_FILE_MODE)
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
            with _lock_holder(
                [sys.executable, "-c", holder_code, str(lock_path)],
                cwd=Path(__file__).resolve().parents[1],
            ):
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
                    timeout=5,
                )
                self.assertEqual(contender.returncode, 75, contender.stderr)
                self.assertIn("contention", contender.stdout)
                self.assertEqual(lock_path.read_text(encoding="utf-8"), "")
            for command, error in (
                ([str(root / "missing")], FileNotFoundError),
                ([sys.executable, "-c", "print('rea', flush=True)"], RuntimeError),
            ):
                with self.subTest(command=command[0]):
                    with self.assertRaises(error), _lock_holder(command, cwd=root):
                        pass
            ignored_term = "import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); sys.stdin.readline(); time.sleep(60)"
            with self.assertRaises(AssertionError), _lock_holder(
                [sys.executable, "-c", ignored_term], cwd=root
            ):
                raise AssertionError("body failure")

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
            lock_path.chmod(LOCK_FILE_MODE)
            if os.geteuid() != 0:
                with patch("tools.platform_verification_lock.LOCK_PATH", lock_path):
                    with self.assertRaisesRegex(VerificationLockError, "wrong owner"):
                        with verification_resource_lock("backend-integration"):
                            pass
                return
            module_dir = root / "module"
            module_dir.mkdir(mode=0o755)
            module_dir.chmod(0o755)
            package_dir = module_dir / "tools"
            package_dir.mkdir(mode=0o755)
            package_dir.chmod(0o755)
            init_file = package_dir / "__init__.py"
            init_file.write_text("", encoding="utf-8")
            init_file.chmod(0o644)
            lock_module = package_dir / "platform_verification_lock.py"
            lock_module.write_bytes(
                Path(__file__).resolve().parents[1].joinpath(
                    "tools/platform_verification_lock.py"
                ).read_bytes()
            )
            lock_module.chmod(0o644)
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
            with _lock_holder(
                [
                    _trusted_runuser(),
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
            ) as holder:
                ready = holder.ready_line.decode("utf-8")
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
                    timeout=5,
                )
                self.assertEqual(contender.returncode, 75, contender.stderr)
                self.assertIn("contention", contender.stdout)
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
                parent.chmod(0o755)
                lock_path = parent / "platformdb-test.lock"
                lock_path.touch(mode=LOCK_FILE_MODE)
                lock_path.chmod(LOCK_FILE_MODE)
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
            parent.chmod(0o755)
            lock_path = parent / "platformdb-test.lock"
            lock_path.touch(mode=LOCK_FILE_MODE)
            lock_path.chmod(LOCK_FILE_MODE)
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
            stable.chmod(0o755)
            stable_lock = stable / "platformdb-test.lock"
            stable_lock.touch(mode=LOCK_FILE_MODE)
            stable_lock.chmod(LOCK_FILE_MODE)
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
        self.assertEqual(workflow.count("sudo -EH env XDG_RUNTIME_DIR= bash -lc"), 3)
        workflow_data = yaml.safe_load(workflow)
        privileged_job = workflow_data["jobs"]["backend-privileged"]
        for step_name in (
            "Run serial privileged catalog contour exactly once as root",
        ):
            step = next(
                item
                for item in privileged_job["steps"]
                if item.get("name") == step_name
            )
            command = step["run"]
            self.assertIn("sudo -n env -i", command)
            self.assertIn("/bin/bash -c", command)
            self.assertNotIn("sudo -E", command)
            self.assertNotIn("bash -lc", command)
            for identity in (
                'GITHUB_ACTIONS="$GITHUB_ACTIONS"',
                'RUNNER_OS="$RUNNER_OS"',
                'RUNNER_ENVIRONMENT="$RUNNER_ENVIRONMENT"',
            ):
                self.assertIn(identity, command)
            for fixed_value in (
                "PLATFORM_ENVIRONMENT=test",
                "PLATFORM_DB_SCHEMA=platform",
                "PLATFORM_DATABASE_URL=postgresql+asyncpg://platform_user:platform_password@127.0.0.1:5432/platformdb_test",
                "PLATFORM_SECRET_KEY=example-ci-privileged-test-secret-no-production-credentials",
                "PLATFORM_REDIS_URL=redis://127.0.0.1:6379/15",
                "PLATFORM_CELERY_BROKER_URL=redis://127.0.0.1:6379/13",
                "PLATFORM_CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/14",
            ):
                self.assertIn(fixed_value, command)
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
        self.assertEqual(set(CI_GATE_IDS), set(EXPECTED_LOCAL_CI_EXECUTION_ORDER))
        self.assertEqual(len(CI_GATE_IDS), len(EXPECTED_LOCAL_CI_EXECUTION_ORDER))
        self.assertEqual(
            len(EXPECTED_LOCAL_CI_EXECUTION_ORDER),
            len(set(EXPECTED_LOCAL_CI_EXECUTION_ORDER)),
        )
        self.assertIn("backend", CI_GATE_IDS)
        self.assertIn("verification-contract", CI_GATE_IDS)
        self.assertTrue(GATES_BY_ID["release-runtime"].conditional)
        self.assertFalse(GATES_BY_ID["release-runtime"].ci_required)
        self.assertFalse(GATES_BY_ID["external-load"].deterministic)
        self.assertFalse(GATES_BY_ID["external-load"].local_safe)
        self.assertEqual(registry_payload()["ci_gate_ids"], list(CI_GATE_IDS))
        self.assertFalse(registry_payload()["impact_planning"]["skip_authority"])

    def test_impact_planner_routes_liveqa_producers_consumers_and_unknown_paths_broadly(self) -> None:
        live_qa = impact_plan(["platform/tools/platform_live_qa_runtime_install.py"])
        self.assertEqual(live_qa["status"], "routed")
        self.assertEqual(
            set(live_qa["owner_contours"]),
            {"backend-privileged", "backend-tool-contract"},
        )
        self.assertTrue(
            {
                "test_platform_live_qa_runtime_install",
                "test_platform_live_qa_wrappers",
                "test_platform_live_qa_mailbox_helper",
                "test_platform_maintenance_qa_evidence",
                "test_platform_remote_workflow_guards",
            }.issubset(set(live_qa["owner_modules"]))
        )
        self.assertFalse(live_qa["skip_authority"])

        report_consumer = impact_plan(["platform/tools/platform_live_user_qa_report.py"])
        self.assertIn("backend-tool-contract", report_consumer["owner_contours"])
        self.assertIn("test_platform_maintenance_qa_evidence", report_consumer["owner_modules"])
        schema_producer = impact_plan(["platform/tools/platform_maintenance_qa_evidence.py"])
        self.assertIn("test_platform_maintenance_qa_evidence", schema_producer["owner_modules"])
        self.assertIn("test_platform_live_qa_wrappers", schema_producer["owner_modules"])
        workflow_consumer = impact_plan([".github/workflows/platform-live-user-qa.yml"])
        self.assertIn("backend-privileged", workflow_consumer["owner_contours"])
        self.assertIn("test_platform_maintenance_qa_evidence", workflow_consumer["owner_modules"])

        for source_path in (
            "platform/tools/platform_test_runner.py",
            "platform/tools/platform_verify.py",
            "platform/tools/platform_verify_contract.py",
            "platform/tools/platform_test_catalog.py",
        ):
            with self.subTest(source_path=source_path):
                owner_plan = impact_plan([source_path])
                self.assertIn("verification-contract", owner_plan["owner_contours"])
                self.assertIn(
                    "test_platform_verification_contract",
                    owner_plan["owner_modules"],
                )
        classifier_plan = impact_plan(["platform/tools/platform_ci_classifier.py"])
        self.assertIn("test_platform_ci_classifier", classifier_plan["owner_modules"])
        security_plan = impact_plan([".github/workflows/platform-security.yml"])
        self.assertTrue(
            {
                "test_platform_ci_classifier",
                "test_platform_verification_contract",
            }.issubset(set(security_plan["owner_modules"]))
        )
        docs_plan = impact_plan(["platform/docs/test-suite-governance.md"])
        self.assertIn("docs", docs_plan["required_gates"])
        self.assertIn("test_platform_verification_contract", docs_plan["owner_modules"])

        test_owner = impact_plan(["platform/tests/test_platform_live_qa_wrappers.py"])
        self.assertEqual(test_owner["owner_contours"], ["backend-privileged"])
        self.assertIn("backend", test_owner["required_gates"])
        self.assertIn("test_platform_live_qa_wrappers", test_owner["owner_modules"])

        unknown = impact_plan(["platform/tools/new_unmapped_consumer.py"])
        self.assertEqual(unknown["status"], "broad_ci_required")
        self.assertEqual(unknown["required_gates"], list(CI_GATE_IDS))
        self.assertFalse(unknown["skip_authority"])
        self.assertIn("full canonical CI", unknown["reason"])
        malformed = impact_plan(["platform/../secrets.env"])
        self.assertEqual(malformed["status"], "broad_ci_required")
        self.assertEqual(malformed["required_gates"], list(CI_GATE_IDS))

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

        safe_environment = {
            "PLATFORM_PYTHON_BIN": "/venv/bin/python",
            "PLATFORM_ENVIRONMENT": "test",
            "PLATFORM_DB_SCHEMA": "platform",
            "PLATFORM_DATABASE_URL": (
                "postgresql+asyncpg://u:p@127.0.0.1:5432/platformdb_test"
            ),
            "PLATFORM_REDIS_URL": "redis://127.0.0.1:6379/15",
        }
        safe_settings = {
            "platform_environment": "test",
            "platform_db_schema": "platform",
            "platform_database_url": safe_environment["PLATFORM_DATABASE_URL"],
            "platform_redis_url": safe_environment["PLATFORM_REDIS_URL"],
        }
        aggregate_order: list[str] = []

        def record_gate(gate_id: str) -> int:
            aggregate_order.append(gate_id)
            return 0

        def record_upgrade(label: str, command: list[str], **kwargs: object) -> int:
            aggregate_order.append("schema-upgrade")
            self.assertEqual(label, "ci-backend-database-bootstrap")
            self.assertEqual(
                command,
                ["/venv/bin/python", "-m", "alembic", "upgrade", "head"],
            )
            self.assertEqual(kwargs["env"], safe_environment)
            self.assertEqual(kwargs["timeout_seconds"], MIGRATION_SUBPROCESS_TIMEOUT_SECONDS)
            return 0

        with (
            patch("tools.platform_verify.dispatch", side_effect=record_gate),
            patch(
                "tools.platform_verify._validated_privileged_environment",
                return_value=(safe_environment, safe_settings),
            ),
            patch("tools.platform_verify._privileged_runner_python", return_value="/venv/bin/python"),
            patch("tools.platform_verify._run", side_effect=record_upgrade),
            patch("tools.platform_verification_lock.verification_resource_lock", return_value=nullcontext()),
        ):
            self.assertEqual(dispatch_ci(), 0)
        self.assertEqual(
            aggregate_order,
            [
                "verification-contract",
                "docs",
                "python-quality",
                "security",
                "migration",
                "schema-upgrade",
                "backend",
                "web-quality",
                "web-hermetic",
            ],
        )

        early_failure_order: list[str] = []
        with (
            patch(
                "tools.platform_verify.dispatch",
                side_effect=lambda gate_id: early_failure_order.append(gate_id) or 19,
            ),
            patch("tools.platform_verify._prepare_ci_backend_database") as prepare_database,
        ):
            self.assertEqual(dispatch_ci(), 19)
        self.assertEqual(early_failure_order, ["verification-contract"])
        prepare_database.assert_not_called()

        failed_order: list[str] = []

        def record_failed_gate(gate_id: str) -> int:
            failed_order.append(gate_id)
            return 0

        with (
            patch("tools.platform_verify.dispatch", side_effect=record_failed_gate),
            patch(
                "tools.platform_verify._validated_privileged_environment",
                return_value=(safe_environment, safe_settings),
            ),
            patch("tools.platform_verify._privileged_runner_python", return_value="/venv/bin/python"),
            patch("tools.platform_verify._run", return_value=17) as failed_upgrade,
            patch("tools.platform_verification_lock.verification_resource_lock", return_value=nullcontext()),
        ):
            self.assertEqual(dispatch_ci(), 17)
        self.assertEqual(
            failed_order,
            ["verification-contract", "docs", "python-quality", "security", "migration"],
        )
        failed_upgrade.assert_called_once()

        unsafe_settings = {
            **safe_settings,
            "platform_environment": "production",
            "platform_database_url": "postgresql://u:p@127.0.0.1:5432/platformdb",
        }
        unsafe_order: list[str] = []

        with (
            patch("tools.platform_verify.dispatch", side_effect=lambda gate_id: unsafe_order.append(gate_id) or 0),
            patch(
                "tools.platform_verify._validated_privileged_environment",
                return_value=(safe_environment, unsafe_settings),
            ),
            patch("tools.platform_verify._run") as unsafe_upgrade,
            self.assertRaises(VerificationError),
        ):
            dispatch_ci()
        self.assertEqual(
            unsafe_order,
            ["verification-contract", "docs", "python-quality", "security", "migration"],
        )
        unsafe_upgrade.assert_not_called()
        missing_seed = _integration_preflight_error(
            migration_heads=["test-head"],
            role_slugs={"admin"},
            expected_head="test-head",
        )
        self.assertIsNotNone(missing_seed)
        self.assertIn("missing required seed roles", str(missing_seed))
        self.assertIn("authenticated_user", str(missing_seed))
        self.assertIn("run the migration gate", str(missing_seed))
        self.assertIn("`alembic upgrade head`", str(missing_seed))
        self.assertIn("disposable platformdb_test", str(missing_seed))
        contract_command, test_command = _verification_contract_commands()
        actionlint_command = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "tools/platform_actionlint.py"),
        ]
        self.assertEqual(
            [call.args[1] for call in run.call_args_list],
            [contract_command, actionlint_command, test_command],
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

    def test_contract_self_test_is_clean(self) -> None:
        self.assertEqual(ALLOWED_ACTION_OWNERS, frozenset({"actions"}))
        self.assertEqual(action_pin_issues(), [])
        self.assertEqual(workflow_level_permission_issues(), [])
        autodeploy = (
            Path(__file__).resolve().parents[2]
            / ".github/workflows/platform-production-autodeploy.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(source_bound_backup_dispatch_issues(autodeploy), [])
        guard_start = autodeploy.index(
            "      - name: Verify exact source-bound restore-verified backup"
        )
        dispatch_start = autodeploy.index(
            "      - name: Dispatch production deployment or baseline reconciliation"
        )
        guard = autodeploy[guard_start:dispatch_start]
        mutations = [
            autodeploy.replace(guard, "", 1),
            autodeploy.replace(
                "        if: ${{ steps.gate.outputs.deploy == 'true' }}\n        shell: bash\n        env:\n          GH_TOKEN: ${{ github.token }}\n          GITHUB_REPOSITORY: ${{ github.repository }}\n          GITHUB_API_URL: ${{ github.api_url }}\n          SOURCE_SECURITY_RUN_ID:",
                "        if: ${{ always() }}\n        shell: bash\n        env:\n          GH_TOKEN: ${{ github.token }}\n          GITHUB_REPOSITORY: ${{ github.repository }}\n          GITHUB_API_URL: ${{ github.api_url }}\n          SOURCE_SECURITY_RUN_ID:",
                1,
            ),
            autodeploy.replace(
                '          set -euo pipefail\n          /usr/bin/python3 -I "$BACKUP_GUARD_TOOL"',
                '          set +e\n          /usr/bin/python3 -I "$BACKUP_GUARD_TOOL" || true',
                1,
            ),
            autodeploy.replace(
                '          "${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/actions/workflows/platform-production-deploy.yml/dispatches"',
                '          "${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/actions/workflows/platform-production-deploy.yml/dispatches"\n          --request POST',
                1,
            ),
            autodeploy.replace(guard, "", 1).replace(
                "      - name: Dispatch production deployment or baseline reconciliation",
                "      - name: Verify exact source-bound restore-verified backup\n        run: true\n\n"
                "      - name: Dispatch production deployment or baseline reconciliation",
                1,
            ),
            autodeploy.replace(
                '/usr/bin/python3 -I "$BACKUP_GUARD_TOOL"',
                '/usr/bin/python3 -I "$BACKUP_GUARD_TOOL" || echo ignored failure',
                1,
            ),
            autodeploy.replace(
                '/usr/bin/python3 -I "$BACKUP_GUARD_TOOL"',
                '/usr/bin/python3 -I "$BACKUP_GUARD_TOOL"; status=$?; echo "$status"; exit 0',
                1,
            ),
            autodeploy.replace(
                '      - name: Verify exact source-bound restore-verified backup\n',
                '      - name: Verify exact source-bound restore-verified backup\n'
                '        continue-on-error: ${{ github.event_name == \'push\' }}\n',
                1,
            ),
            autodeploy.replace(
                '          SOURCE_SECURITY_RUN_ATTEMPT: ${{ github.event.workflow_run.run_attempt }}\n',
                '          SOURCE_SECURITY_RUN_ATTEMPT: ${{ github.event.workflow_run.run_attempt }}\n'
                '          SOURCE_RUN_COMPLETED_AT: ${{ github.event.workflow_run.completed_at }}\n',
                1,
            ),
            "jobs:\n  malformed: [\n",
        ]
        for index, malformed in enumerate(mutations):
            with self.subTest(mutation=index):
                self.assertTrue(source_bound_backup_dispatch_issues(malformed))
        dispatch_end = autodeploy.index("\n      - name: Auto-deploy dispatch summary", dispatch_start)
        dispatch_step = autodeploy[dispatch_start:dispatch_end]
        moved_dispatch = autodeploy[:dispatch_start] + autodeploy[dispatch_end:]
        moved_dispatch = moved_dispatch.replace(
            "jobs:\n",
            "jobs:\n  unrelated-dispatch:\n    runs-on: ubuntu-latest\n    steps:\n"
            + dispatch_step
            + "\n",
            1,
        )
        self.assertTrue(
            any(
                "final backup proof and production dispatch must share" in issue
                for issue in source_bound_backup_dispatch_issues(moved_dispatch)
            )
        )
        workflow_text = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(_backend_workflow_issues(workflow_text), [])
        privileged_match = re.search(
            r"^  backend-privileged:\n(?P<block>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            workflow_text,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(privileged_match)
        assert privileged_match is not None
        privileged_block = "  backend-privileged:\n" + privileged_match.group("block")
        privileged_start = privileged_match.start()
        privileged_end = privileged_match.end()
        missing_privileged_redis = (
            workflow_text[:privileged_start]
            + privileged_block.replace("    services:\n      redis:\n", "", 1)
            + workflow_text[privileged_end:]
        )
        self.assertTrue(
            any(
                "backend-privileged must declare only its isolated Redis service" in issue
                for issue in _backend_workflow_issues(missing_privileged_redis)
            )
        )
        privileged_with_postgres = (
            workflow_text[:privileged_start]
            + privileged_block.replace(
                "    services:\n      redis:\n",
                "    services:\n      postgres:\n        image: postgres:16\n      redis:\n",
                1,
            )
            + workflow_text[privileged_end:]
        )
        self.assertTrue(
            any(
                "backend-privileged must declare only its isolated Redis service" in issue
                for issue in _backend_workflow_issues(privileged_with_postgres)
            )
        )
        self.assertEqual(
            security_status_permission_issues(workflow_text),
            [],
        )
        self.assertEqual(host_tools_pin_verification_issues(workflow_text), [])
        self.assertEqual(actionlint_workflow_issues(workflow_text), [])
        missing_actionlint_attestation = workflow_text.replace(
            "gh attestation verify --repo rhysd/actionlint \\\n",
            "gh attestation verify --repo other/tool \\\n",
            1,
        )
        self.assertTrue(
            any(
                "pinned upstream release attestation identity" in issue
                for issue in actionlint_workflow_issues(missing_actionlint_attestation)
            )
        )
        missing_actionlint_signer = workflow_text.replace(
            "--signer-workflow rhysd/actionlint/.github/workflows/release.yaml",
            "--signer-workflow rhysd/actionlint/.github/workflows/other.yaml",
            1,
        )
        self.assertTrue(
            any(
                "pinned upstream release attestation identity" in issue
                for issue in actionlint_workflow_issues(missing_actionlint_signer)
            )
        )
        missing_actionlint_checksum = workflow_text.replace(
            "sha256sum --check --status", "sha256sum --check", 1
        )
        self.assertTrue(
            any(
                "checksum verification" in issue
                for issue in actionlint_workflow_issues(missing_actionlint_checksum)
            )
        )
        actionlint_tool = Path(__file__).resolve().parents[1] / "tools/platform_actionlint.py"
        blocked_environment = os.environ.copy()
        blocked_environment.pop("ACTIONLINT_BIN", None)
        blocked_environment["PATH"] = ""
        blocked = subprocess.run(
            [sys.executable, str(actionlint_tool)],
            capture_output=True,
            text=True,
            check=False,
            env=blocked_environment,
            timeout=20,
        )
        self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
        self.assertIn("LOCAL GATE BLOCKED", blocked.stderr)
        missing_pin_resolver = workflow_text.replace(
            "platform/tools/platform_host_tools_pin.py resolve",
            "platform/tools/platform_host_tools_pin.py inspect",
            1,
        )
        self.assertTrue(
            any(
                "host-tools-pin-preflight must run the canonical resolver exactly once" in issue
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
        missing_early_dependency = workflow_text.replace(
            "needs.host-tools-pin-preflight.result == 'success' &&",
            "",
            1,
        )
        self.assertTrue(
            any(
                "backend-static must explicitly require successful host-tools-pin-preflight" in issue
                for issue in host_tools_pin_verification_issues(missing_early_dependency)
            )
        )
        self.assertEqual(release_runtime_workflow_issues(workflow_text), [])
        runtime_match = re.search(
            r"^  release-runtime:\n(?P<block>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            workflow_text,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(runtime_match)
        assert runtime_match is not None
        runtime_block = runtime_match.group("block")
        self.assertIn("image: redis:7", runtime_block)
        missing_runtime_redis = workflow_text.replace(
            runtime_block,
            runtime_block.replace("    services:\n      redis:\n", "", 1),
            1,
        )
        self.assertTrue(
            any(
                "release-runtime fixture must declare isolated Redis DB 15" in issue
                for issue in release_runtime_workflow_issues(missing_runtime_redis)
            )
        )
        runtime_with_postgres = workflow_text.replace(
            runtime_block,
            runtime_block.replace(
                "    services:\n      redis:\n",
                "    services:\n      postgres:\n        image: postgres:16\n      redis:\n",
                1,
            ),
            1,
        )
        self.assertTrue(
            any(
                "release-runtime fixture must declare isolated Redis DB 15" in issue
                for issue in release_runtime_workflow_issues(runtime_with_postgres)
            )
        )
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
        real_job_start = workflow_text.index("  release-runtime-real:\n")
        next_job = re.search(
            r"^  [A-Za-z0-9_-]+:\n",
            workflow_text[real_job_start + len("  release-runtime-real:\n"):],
            re.MULTILINE,
        )
        real_job_end = (
            real_job_start + len("  release-runtime-real:\n") + next_job.start()
            if next_job is not None
            else len(workflow_text)
        )
        real_job_block = workflow_text[real_job_start:real_job_end]
        self.assertIn("github.ref == 'refs/heads/dev'", real_job_block)
        missing_dev_route = (
            workflow_text[:real_job_start]
            + real_job_block.replace(
                "github.ref == 'refs/heads/dev'",
                "github.ref == 'refs/heads/main'",
            )
            + workflow_text[real_job_end:]
        )
        self.assertNotIn("github.ref == 'refs/heads/dev'", real_job_block.replace(
            "github.ref == 'refs/heads/dev'",
            "github.ref == 'refs/heads/main'",
        ))
        self.assertTrue(
            any(
                "canonical dev ref condition" in issue
                for issue in release_runtime_workflow_issues(missing_dev_route)
            )
        )
        untrusted_pr_route = workflow_text.replace(
            "github.event.pull_request.head.repo.full_name == github.repository",
            "github.event.pull_request.head.repo.full_name != github.repository",
            1,
        )
        self.assertTrue(
            any(
                "same-repository dev PR head repository" in issue
                for issue in release_runtime_workflow_issues(untrusted_pr_route)
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
            "      - name: Remove exact release size projection temporary directory",
            "      - name: Unapproved release artifact upload\n"
            "        uses: actions/upload-artifact@" + "a" * 40 + "\n"
            "      - name: Remove exact release size projection temporary directory",
            1,
        )
        self.assertTrue(
            any(
                "must not publish" in issue
                for issue in release_runtime_workflow_issues(release_publishing)
            )
        )
        projection_upload_step = (
            "      - name: Upload evidence-only release size projection\n"
            "        if: ${{ success() }}\n"
            "        uses: actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f # v6.0.0\n"
            "        with:\n"
            "          name: platform-release-size-${{ github.run_id }}-${{ github.run_attempt }}\n"
            "          path: ${{ steps.real-runtime-build.outputs.projection_path }}\n"
            "          if-no-files-found: error\n"
            "          retention-days: 14\n"
        )
        self.assertEqual(workflow_text.count(projection_upload_step), 1)
        for label, replacement in (
            (
                "projection path",
                projection_upload_step.replace(
                    "path: ${{ steps.real-runtime-build.outputs.projection_path }}",
                    "path: ${{ github.workspace }}/release.tar.gz",
                    1,
                ),
            ),
            (
                "wildcard path",
                projection_upload_step.replace(
                    "path: ${{ steps.real-runtime-build.outputs.projection_path }}",
                    "path: ${{ steps.real-runtime-build.outputs.projection_path }}/**",
                    1,
                ),
            ),
            (
                "run-bound name",
                projection_upload_step.replace(
                    "platform-release-size-${{ github.run_id }}-${{ github.run_attempt }}",
                    "platform-release-size-latest",
                    1,
                ),
            ),
            (
                "unapproved action ref",
                projection_upload_step.replace(
                    "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f # v6.0.0",
                    "actions/upload-artifact@v6.0.0",
                    1,
                ),
            ),
            (
                "missing-file policy",
                projection_upload_step.replace(
                    "if-no-files-found: error",
                    "if-no-files-found: warn",
                    1,
                ),
            ),
            (
                "retention",
                projection_upload_step.replace("retention-days: 14", "retention-days: 90", 1),
            ),
            ("missing upload", ""),
        ):
            altered = workflow_text.replace(projection_upload_step, replacement, 1)
            with self.subTest(release_projection_upload=label):
                self.assertTrue(
                    any(
                        "must not publish" in issue
                        for issue in release_runtime_workflow_issues(altered)
                    )
                )
        release_attestation = workflow_text.replace(
            "      - name: Remove exact release size projection temporary directory",
            "      - name: Unapproved release attestation\n"
            "        uses: actions/attest-build-provenance@" + "a" * 40 + "\n"
            "      - name: Remove exact release size projection temporary directory",
            1,
        )
        self.assertTrue(
            any(
                "must not publish" in issue
                for issue in release_runtime_workflow_issues(release_attestation)
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
        production_text = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(_production_secret_scope_issues(production_text), [])
        host_start = production_text.index("  host-capability-preflight:")
        host_end = production_text.index("  build-release:", host_start)
        host_block = production_text[host_start:host_end]
        wrong_source_ref = host_block.replace(
            "ref: ${{ env.TARGET_SHA }}", "ref: refs/heads/dev", 1
        )
        with self.subTest(host_verifier_checkout="wrong source ref"):
            self.assertTrue(
                any(
                    "checkout the exact target" in issue
                    for issue in _production_secret_scope_issues(
                        production_text[:host_start] + wrong_source_ref + production_text[host_end:]
                    )
                )
            )
        missing_dispatch_binding = host_block.replace(
            "needs.validate-dispatch.result == 'success'", "needs.validate-dispatch.result == 'skipped'", 1
        )
        with self.subTest(host_verifier_checkout="missing provenance dependency"):
            self.assertTrue(
                any(
                    "exact dispatch provenance" in issue
                    for issue in _production_secret_scope_issues(
                        production_text[:host_start] + missing_dispatch_binding + production_text[host_end:]
                    )
                )
            )
        missing_shared_verifier = host_block.replace(
            '"$GITHUB_WORKSPACE/target-source/platform/tools/platform_host_tools_installed_check.py"',
            '"$GITHUB_WORKSPACE/target-source/platform/tools/other.py"',
            1,
        )
        with self.subTest(host_verifier_checkout="missing shared verifier"):
            self.assertTrue(
                any(
                    "shared verifier" in issue
                    for issue in _production_secret_scope_issues(
                        production_text[:host_start] + missing_shared_verifier + production_text[host_end:]
                    )
                )
            )
        capability_audit_issue = "exact v3/v4 capability layout"
        for marker in (
            'source_sha != sys.argv[8] or version != sys.argv[9]',
            'if version == "production-host-tools-v3":',
            'elif version == "production-host-tools-v4":',
            'capabilities_path.read_text(encoding="ascii") != expected_capabilities',
        ):
            with self.subTest(host_tools_capability_contract=marker):
                altered = production_text.replace(marker, "", 1)
                self.assertTrue(
                    any(
                        capability_audit_issue in issue
                        for issue in _production_secret_scope_issues(altered)
                    )
                )
        cpu_append = '              capabilities += ("cpu_diagnostic_plan_control",)\n'
        moved_cpu_append = production_text.replace(cpu_append, "", 1).replace(
            '          else:\n              raise SystemExit("host-tools toolset version is unsupported")\n'
            '          expected_paths = files | {"capabilities.txt"}',
            '          else:\n              raise SystemExit("host-tools toolset version is unsupported")\n'
            '          capabilities += ("cpu_diagnostic_plan_control",)\n'
            '          expected_paths = files | {"capabilities.txt"}',
            1,
        )
        with self.subTest(host_tools_capability_contract="CPU capability moved outside v4"):
            self.assertNotEqual(moved_cpu_append, production_text)
            self.assertTrue(
                any(
                    "CPU diagnostic capability must be added only in the exact v4 branch" in issue
                    for issue in _production_secret_scope_issues(moved_cpu_append)
                )
            )
        missing_legacy_source_binding = production_text.replace(
            '"retained_load_source_binding", ', "", 1
        )
        with self.subTest(host_tools_capability_contract="legacy source-binding capability removed"):
            self.assertTrue(
                any(
                    "exact legacy v3 capability tuple" in issue
                    for issue in _production_secret_scope_issues(missing_legacy_source_binding)
                )
            )
        cpu_in_legacy_capabilities = production_text.replace(
            '              "retained_load_source_binding", "python_isolated", "python_bytecode_disabled",\n          )',
            '              "retained_load_source_binding", "python_isolated", "python_bytecode_disabled", "cpu_diagnostic_plan_control",\n          )',
            1,
        )
        with self.subTest(host_tools_capability_contract="CPU capability added to legacy tuple"):
            self.assertTrue(
                any(
                    "exact legacy v3 capability tuple" in issue
                    for issue in _production_secret_scope_issues(cpu_in_legacy_capabilities)
                )
            )
        version_plumbing_issue = "trusted pinned toolset version"
        for marker in (
            "host_tools_toolset_version: ${{ steps.build-host-tools-bundle.outputs.host_tools_toolset_version }}",
            "printf 'host_tools_toolset_version=%s\\n' \"$host_tools_toolset_version\" >> \"$GITHUB_OUTPUT\"",
            "host_tools_toolset_version: ${{ needs.build-host-tools.outputs.host_tools_toolset_version }}",
            "HOST_TOOLS_TOOLSET_VERSION: ${{ needs.build-host-tools.outputs.host_tools_toolset_version }}",
            "HOST_TOOLS_TOOLSET_VERSION: ${{ needs.host-capability-preflight.outputs.host_tools_toolset_version }}",
        ):
            with self.subTest(host_tools_version_plumbing=marker):
                altered = production_text.replace(marker, "", 1)
                self.assertTrue(
                    any(
                        version_plumbing_issue in issue
                        for issue in _production_secret_scope_issues(altered)
                    )
                )
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
            restore_id = next(
                case.test_id for case in discover_test_cases()
                if case.module == "test_platform_backup_restore_drill"
                and case.method_name == "test_isolated_restore_signal_cleanup_under_root"
            )
            restore_timing = next(item for item in aggregate["timings"] if item["test_id"] == restore_id)
            self.assertEqual(restore_timing["outcome"], "passed")
            self.assertNotIn(restore_id, aggregate["skipped"])

        mailbox_skip_id = (
            "tests.test_platform_live_qa_mailbox_helper.MailboxHelperTests."
            "test_live_shared_env_metadata_matches_reviewed_contour_when_present"
        )
        mailbox_skip_reason = "production shared env path is absent"
        self.assertEqual(
            INTENTIONAL_SKIP_IDS,
            {mailbox_skip_id: mailbox_skip_reason},
        )
        mailbox_case = next(case for case in discover_test_cases() if case.test_id == mailbox_skip_id)
        self.assertEqual(mailbox_case.contour, "backend-privileged")
        with tempfile.TemporaryDirectory() as directory:
            component_dir = Path(directory)
            _write_backend_component_fixture(component_dir)
            summary_path = component_dir / "backend-privileged" / "summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["skipped"] = [mailbox_skip_id]
            summary["skipped_count"] = 1
            summary["skip_reasons"] = {mailbox_skip_id: mailbox_skip_reason}
            timing = next(item for item in summary["timings"] if item["test_id"] == mailbox_skip_id)
            timing["outcome"] = "skipped"
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            aggregate = verify_backend_components(component_dir)
            self.assertEqual(aggregate["status"], "passed")
            self.assertEqual(aggregate["skipped"], [mailbox_skip_id])

        mutations = (
            "duplicate executed ID",
            "out-of-order executed IDs",
            "unexpected executed ID",
            "missing executed ID",
            "empty timings",
            "invalid timing duration",
            "tests_run mismatch",
            "unapproved isolated restore skip",
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
                elif mutation == "unapproved isolated restore skip":
                    restore_id = next(
                        case.test_id for case in discover_test_cases()
                        if case.module == "test_platform_backup_restore_drill"
                        and case.method_name == "test_isolated_restore_signal_cleanup_under_root"
                    )
                    summary_path = (
                        component_dir / "backend-privileged" / "summary.json"
                    )
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    self.assertIn(restore_id, summary["expected_ids"])
                    self.assertIn(restore_id, summary["executed_ids"])
                    summary["skipped"] = [restore_id]
                    summary["skipped_count"] = 1
                    summary["skip_reasons"] = {
                        restore_id: "isolated restore unexpectedly skipped"
                    }
                    timing = next(item for item in summary["timings"] if item["test_id"] == restore_id)
                    timing["outcome"] = "skipped"
                    summary_path.write_text(json.dumps(summary), encoding="utf-8")
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
                "ready-vote-transport-pair-v1",
                "ready-vote-capacity-ramp-v2",
                "ready-vote-saturation-ramp-v4",
                "ready-vote-stress-15k-v2",
                "ready-vote-stress-20k-v2",
                "ready-vote-spike-v1",
                "read-mix-human-v2",
                "read-mix-stress-v2",
                "read-mix-concurrency-ramp-v1",
                "authenticated-page-load-v1",
                "authenticated-page-load-v2",
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
                    run_profile_worker(
                        profile,
                        Path(directory) / "manifest.json",
                        report_path,
                    ),
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
