from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from unittest import mock

from tools import platform_worker_liveness as liveness


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = PLATFORM_ROOT / "tools"
SOURCE_SHA = "a" * 40


class _FakeResult:
    state = "SUCCESS"

    def __init__(self) -> None:
        self.backend = types.SimpleNamespace(
            get_key_for_task=lambda task_id: f"celery-task-meta-{task_id}"
        )
        self.forgotten = False
        self.get_calls: list[dict[str, object]] = []

    def get(self, **kwargs: object) -> str:
        self.get_calls.append(kwargs)
        return "pong"

    def forget(self) -> None:
        self.forgotten = True


class _FakeRedisClient:
    def __init__(self, *, present: bool = False) -> None:
        self.present = present
        self.exists_calls: list[object] = []
        self.llen_calls: list[str] = []
        self.closed = False

    def exists(self, key: object) -> int:
        self.exists_calls.append(key)
        return int(self.present)

    def llen(self, queue: str) -> int:
        self.llen_calls.append(queue)
        return 0

    def close(self) -> None:
        self.closed = True


class _FakeTask:
    name = liveness.PING_TASK_NAME

    def __init__(self, result: _FakeResult) -> None:
        self.result = result
        self.apply_calls: list[dict[str, object]] = []

    def apply_async(self, **kwargs: object) -> _FakeResult:
        self.apply_calls.append(kwargs)
        return self.result


class _FakeApp:
    def __init__(self, task: _FakeTask, result: _FakeResult) -> None:
        self.conf = types.SimpleNamespace(
            broker_url="redis://127.0.0.1:6379/13",
            result_backend="redis://127.0.0.1:6379/14",
            task_default_queue=liveness.DEFAULT_QUEUE,
        )
        self.tasks = {liveness.PING_TASK_NAME: task}
        self.amqp = types.SimpleNamespace(
            router=types.SimpleNamespace(
                route=lambda *_args: {"queue": liveness.DEFAULT_QUEUE}
            )
        )
        self._result = result

    def AsyncResult(self, _task_id: str) -> _FakeResult:
        return self._result


def _identity(root: Path) -> liveness.RunIdentity:
    release = root / "releases" / "release-a"
    return liveness.RunIdentity(
        app_dir=root,
        release=release,
        expected_source_sha=SOURCE_SHA,
        worker_env=root / "shared" / "env" / "worker.env",
    )


class WorkerLivenessHelperTests(unittest.TestCase):
    def test_task_ids_are_bounded_and_unique_under_concurrency(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as executor:
            task_ids = list(executor.map(lambda _: liveness._new_task_id(), range(128)))

        self.assertEqual(len(task_ids), len(set(task_ids)))
        self.assertTrue(all(liveness.TASK_ID_PATTERN.fullmatch(task_id) for task_id in task_ids))
        self.assertTrue(all(len(task_id) <= 128 for task_id in task_ids))

    def test_redis_namespace_requires_exact_loopback_db(self) -> None:
        liveness._redis_namespace(
            "redis://127.0.0.1:6379/13",
            expected_database="13",
        )
        for value in (
            "redis://127.0.0.1:6379/12",
            "redis://worker:secret@127.0.0.1:6379/13",
            "redis://redis.example.test:6379/13",
            "redis://127.0.0.1:6379/013",
        ):
            with self.subTest(value=value):
                with self.assertRaises(liveness.LivenessFailure):
                    liveness._redis_namespace(value, expected_database="13")

    def test_release_identity_mismatch_happens_before_worker_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)
            (root / "current").symlink_to(release)
            (release / "RELEASE.json").write_text(
                json.dumps(
                    {
                        "release_slug": "release-a",
                        "source_git_commit": "b" * 40,
                    }
                ),
                encoding="ascii",
            )
            output = io.StringIO()
            with (
                mock.patch.object(
                    liveness.importlib,
                    "import_module",
                    side_effect=AssertionError("worker import must not happen"),
                ),
                contextlib.redirect_stdout(output),
            ):
                status = liveness.main(
                    [
                        "--app-dir",
                        str(root),
                        "--release",
                        str(release),
                        "--expected-source-sha",
                        SOURCE_SHA,
                    ]
                )

        self.assertEqual(status, 1)
        rendered = output.getvalue()
        payload = json.loads(rendered)
        self.assertEqual(payload["status"], "failed")
        self.assertNotIn("redis://", rendered)
        self.assertNotIn("platform-release-ping-", rendered)
        self.assertNotIn("source_git_commit", rendered)
        self.assertNotIn("b" * 40, rendered)

    def test_roundtrip_uses_fixed_route_no_retry_and_proves_cleanup(self) -> None:
        result = _FakeResult()
        task = _FakeTask(result)
        app = _FakeApp(task, result)
        redis_client = _FakeRedisClient()
        redis_module = types.SimpleNamespace(
            Redis=types.SimpleNamespace(from_url=lambda *_args, **_kwargs: redis_client)
        )
        identity = _identity(Path("/opt/oldsparky/platform"))

        with (
            mock.patch.object(liveness, "validate_worker_execution"),
            mock.patch.object(liveness, "_load_worker_environment"),
            mock.patch.object(
                liveness.importlib,
                "import_module",
                return_value=types.SimpleNamespace(celery_app=app),
            ),
            mock.patch.dict(sys.modules, {"redis": redis_module}),
        ):
            payload = liveness.run_liveness(identity)

        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["cleanup"], "proven")
        self.assertEqual(task.apply_calls[0]["task_id"].startswith("platform-release-ping-"), True)
        self.assertEqual(task.apply_calls[0]["queue"], liveness.DEFAULT_QUEUE)
        self.assertEqual(task.apply_calls[0]["routing_key"], liveness.DEFAULT_QUEUE)
        self.assertIs(task.apply_calls[0]["retry"], False)
        self.assertIn("expires", task.apply_calls[0])
        self.assertLessEqual(task.apply_calls[0]["expires"], liveness.DEFAULT_EXPIRES_SECONDS)
        self.assertEqual(
            redis_client.exists_calls[0],
            f"celery-task-meta-{task.apply_calls[0]['task_id']}",
        )
        self.assertEqual(result.get_calls[0]["propagate"], False)
        self.assertTrue(result.forgotten)
        self.assertEqual(len(redis_client.exists_calls), 1)
        rendered = json.dumps(payload, sort_keys=True)
        self.assertNotIn("pong", rendered)
        self.assertNotIn("platform-release-ping-", rendered)
        self.assertNotIn("redis://", rendered)

    def test_cleanup_unproven_is_distinct_and_non_sensitive(self) -> None:
        result = _FakeResult()
        redis_client = _FakeRedisClient(present=True)
        redis_module = types.SimpleNamespace(
            Redis=types.SimpleNamespace(from_url=lambda *_args, **_kwargs: redis_client)
        )
        identity = _identity(Path("/opt/oldsparky/platform"))
        now = iter((0.0, 1.0, 16.0, 16.0))
        with (
            mock.patch.object(liveness, "validate_worker_execution"),
            mock.patch.object(liveness, "_load_worker_environment"),
            mock.patch.object(
                liveness.importlib,
                "import_module",
                return_value=types.SimpleNamespace(
                    celery_app=_FakeApp(_FakeTask(result), result)
                ),
            ),
            mock.patch.dict(sys.modules, {"redis": redis_module}),
            mock.patch.object(liveness.time, "sleep"),
        ):
            payload = liveness.run_liveness(identity, clock=lambda: next(now))

        self.assertEqual(payload["status"], "cleanup_unproven")
        self.assertEqual(payload["cleanup"], "unproven")
        self.assertNotIn("pong", json.dumps(payload))

    def test_helper_has_no_global_destructive_or_control_plane_operations(self) -> None:
        source = (TOOLS_ROOT / "platform_worker_liveness.py").read_text(encoding="utf-8")
        for forbidden in ("KEYS", "FLUSHDB", "terminate", "inspect", "beat"):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("tests.worker_liveness_probe", source)
        self.assertIn("apps.platform_worker.worker", source)


class WorkerLivenessReleaseWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.release = (TOOLS_ROOT / "platform_release_deploy.sh").read_text(
            encoding="utf-8"
        )
        self.supervisor = (
            TOOLS_ROOT / "platform_production_deploy_supervisor.sh"
        ).read_text(encoding="utf-8")
        self.restore = (TOOLS_ROOT / "platform_release_restore_runtime.sh").read_text(
            encoding="utf-8"
        )

    def test_supervisor_passes_one_exact_sha_into_candidate_deploy(self) -> None:
        invocation = self.supervisor.index('"$candidate_deploy" \\')
        self.assertIn(
            'PLATFORM_RELEASE_EXPECTED_SOURCE_SHA="$target_sha"',
            self.supervisor,
        )
        self.assertIn(
            '--expected-source-sha "$target_sha"',
            self.supervisor[invocation - 120 : invocation + 500],
        )

    def test_liveness_is_first_loopback_smoke_and_never_public(self) -> None:
        worker_at = self.release.index("run_worker_liveness_smoke")
        loopback_smoke_at = self.release.index(
            '"$CANDIDATE/tools/platform_deploy_smoke.py"', worker_at
        )
        public_smoke_at = self.release.index(
            '"$CANDIDATE/tools/platform_deploy_smoke.py"', loopback_smoke_at + 1
        )
        self.assertLess(worker_at, loopback_smoke_at)
        self.assertLess(loopback_smoke_at, public_smoke_at)
        self.assertNotIn("run_worker_liveness_smoke", self.release[public_smoke_at:])
        worker_block = self.release[
            self.release.index("run_worker_liveness_smoke()") : self.release.index(
                "\n}\n\nsystemd_now_ns()",
                self.release.index("run_worker_liveness_smoke()"),
            )
        ]
        self.assertIn("/usr/sbin/runuser -u oldsparky-worker", worker_block)
        self.assertIn('PLATFORM_ENV_FILE="$worker_env"', worker_block)
        self.assertIn('PYTHONPATH="$CANDIDATE"', worker_block)
        self.assertIn("--expected-source-sha", worker_block)
        self.assertNotIn("PUBLIC_EDGE_ORIGIN", worker_block)
        self.assertIn(
            'smoke_source_args=(--expected-source-sha "$EXPECTED_SOURCE_SHA")',
            self.release,
        )

    def test_source_mismatch_guard_precedes_quiesce_and_worker_publish(self) -> None:
        artifact_guard = self.release.index("validate_expected_artifact_source")
        preflight = self.release.index("run_release_preflight_quiet", artifact_guard)
        quiesce = self.release.index("quiesce_runtime_writers", preflight)
        self.assertLess(artifact_guard, preflight)
        self.assertLess(preflight, quiesce)
        identity_guard = self.release.index("verify_active_candidate_identity ||")
        runtime_reconcile = self.release.index("run_live_qa_reconcile ||")
        worker_publish = self.release.index("run_worker_liveness_smoke ||")
        self.assertLess(identity_guard, runtime_reconcile)
        self.assertLess(identity_guard, worker_publish)

    def test_recovery_no_restart_path_does_not_call_release_liveness(self) -> None:
        self.assertNotIn("platform_worker_liveness.py", self.restore)
        no_restart = self.restore.index("--no-restart")
        self.assertLess(no_restart, self.restore.index("RUN_SMOKE=0", no_restart))

    def test_release_smoke_uses_expected_source_identity_before_network_checks(self) -> None:
        smoke = (TOOLS_ROOT / "platform_deploy_smoke_impl.py").read_text(
            encoding="utf-8"
        )
        identity = smoke.index("check_release_source_identity")
        http = smoke.index("required_env_keys")
        self.assertLess(identity, http)
        self.assertIn("--expected-source-sha", smoke)


if __name__ == "__main__":
    unittest.main()
