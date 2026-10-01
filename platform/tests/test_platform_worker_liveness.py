from __future__ import annotations

import base64
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import time
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


class _FakeApp:
    def __init__(self, task: object, result: _FakeResult) -> None:
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


class _FakeTask:
    name = liveness.PING_TASK_NAME

    def __init__(self, result: _FakeResult) -> None:
        self.result = result
        self.apply_calls: list[dict[str, object]] = []

    def apply_async(self, **kwargs: object) -> _FakeResult:
        self.apply_calls.append(kwargs)
        return self.result


class _FakeRedisClient:
    def __init__(self, *, present: bool = False) -> None:
        self.present = present
        self.delete_calls: list[object] = []
        self.exists_calls: list[object] = []
        self.llen_calls: list[str] = []
        self.closed = False

    def delete(self, key: object) -> int:
        self.delete_calls.append(key)
        self.present = False
        return 1

    def exists(self, key: object) -> int:
        self.exists_calls.append(key)
        return int(self.present)

    def llen(self, queue: str) -> int:
        self.llen_calls.append(queue)
        return 0

    def close(self) -> None:
        self.closed = True


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
        self.assertLessEqual(liveness.MAX_SECONDS, 15.0)
        self.assertLess(
            liveness.CHILD_WORK_SECONDS
            + liveness.CHILD_STOP_GRACE_SECONDS
            + liveness.CHILD_KILL_GRACE_SECONDS,
            liveness.MAX_SECONDS,
        )
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
        liveness._redis_namespace(
            "redis://worker:secret@127.0.0.1:6379/13",
            expected_database="13",
            credentials_required=True,
        )
        for value in (
            "redis://127.0.0.1:6379/12",
            "rediss://127.0.0.1:6379/13",
            "REDIS://127.0.0.1:6379/13",
            "redis://127.0.0.1/13",
            "redis://127.0.0.1:/13",
            "redis://127.0.0.1:06379/13",
            "redis://127.0.0.1:6380/13",
            "redis://127.0.0.1:abc/13",
            "redis://127.0.0.1:6379/13?db=0",
            "redis://127.0.0.1:6379/13#fragment",
            "redis://worker:secret@127.0.0.1:6379/13",
            "redis://127.0.0.1:6379/13\x01",
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

    def test_child_roundtrip_uses_fixed_route_no_retry_and_forgets(self) -> None:
        result = _FakeResult()
        task = _FakeTask(result)
        app = _FakeApp(task, result)
        control_read, control_write = os.pipe()

        with (
            mock.patch.object(
                liveness.importlib,
                "import_module",
                return_value=types.SimpleNamespace(celery_app=app),
            ),
        ):
            status = liveness._child_roundtrip(
                "platform-release-ping-" + "a" * 32,
                time.monotonic() + 5.0,
                control_write,
            )
        events = [json.loads(line) for line in os.read(control_read, 16_384).splitlines()]
        os.close(control_read)

        self.assertEqual(status, 0)
        self.assertTrue(any(event["event"] == "terminal" and event["ok"] for event in events))
        self.assertTrue(result.forgotten)
        self.assertEqual(task.apply_calls[0]["task_id"].startswith("platform-release-ping-"), True)
        self.assertEqual(task.apply_calls[0]["queue"], liveness.DEFAULT_QUEUE)
        self.assertEqual(task.apply_calls[0]["routing_key"], liveness.DEFAULT_QUEUE)
        self.assertIs(task.apply_calls[0]["retry"], False)
        self.assertIn("expires", task.apply_calls[0])
        self.assertLessEqual(task.apply_calls[0]["expires"], liveness.DEFAULT_EXPIRES_SECONDS)
        self.assertEqual(result.get_calls[0]["propagate"], False)

    def _parent_env(self) -> dict[str, str]:
        return {
            "PLATFORM_CELERY_BROKER_URL": "redis://127.0.0.1:6379/13",
            "PLATFORM_CELERY_RESULT_BACKEND": "redis://127.0.0.1:6379/14",
            "PLATFORM_REDIS_URL": "redis://127.0.0.1:6379/15",
        }

    def _spawn_control_child(
        self,
        events: list[dict[str, object]],
        *,
        sleep_seconds: float = 0.0,
        exit_code: int = 0,
        ignore_term: bool = False,
        fork_descendant: bool = False,
    ) -> tuple[callable, dict[str, object]]:
        encoded = json.dumps(events, separators=(",", ":"))
        holder: dict[str, object] = {}

        def spawn(_identity: liveness.RunIdentity, _task_id: str, _deadline: float):
            read_fd, write_fd = os.pipe()
            os.set_inheritable(write_fd, True)
            script = textwrap.dedent(
                f"""
                import json, os, sys, time
                fd = int(sys.argv[1])
                if {ignore_term!r}:
                    import signal
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                if {fork_descendant!r}:
                    child_pid = os.fork()
                    if child_pid == 0:
                        for child_fd in (fd, 1, 2):
                            try:
                                os.close(child_fd)
                            except OSError:
                                pass
                        time.sleep(5.0)
                        raise SystemExit(0)
                for event in json.loads({encoded!r}):
                    os.write(fd, (json.dumps(event, separators=(',', ':')) + '\\n').encode('ascii'))
                time.sleep({sleep_seconds!r})
                os.close(fd)
                raise SystemExit({exit_code!r})
                """
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(write_fd)],
                pass_fds=(write_fd,),
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            os.close(write_fd)
            holder["process"] = process
            holder["pgid"] = os.getpgid(process.pid)
            return process, read_fd

        return spawn, holder

    def _spawn_cleanup_control_child(
        self,
        *,
        ok: bool = True,
        sleep_seconds: float = 0.0,
        exit_code: int = 0,
        ignore_term: bool = False,
        fork_descendant: bool = False,
    ) -> tuple[callable, dict[str, object]]:
        spawn, holder = self._spawn_control_child(
            [{"event": "cleanup", "ok": ok}],
            sleep_seconds=sleep_seconds,
            exit_code=exit_code,
            ignore_term=ignore_term,
            fork_descendant=fork_descendant,
        )

        def cleanup_spawn(
            _identity: liveness.RunIdentity,
            _key: str | bytes,
            _result_url: str,
            deadline: float,
        ):
            return spawn(_identity, "cleanup", deadline)

        return cleanup_spawn, holder

    def test_parent_forgets_exact_key_and_proves_success(self) -> None:
        task_id = "platform-release-ping-" + "b" * 32
        key = b"celery-task-meta-" + task_id.encode("ascii")
        events = [
            {"event": "route", "ok": True},
            {"event": "prepared", "key": base64.b64encode(key).decode("ascii")},
            {"event": "published", "key": base64.b64encode(key).decode("ascii")},
            {"event": "terminal", "ok": True, "forget_ok": True, "key": base64.b64encode(key).decode("ascii")},
        ]
        spawn, holder = self._spawn_control_child(events)
        cleanup_spawn, cleanup_holder = self._spawn_cleanup_control_child()
        redis_client = _FakeRedisClient()
        redis_module = types.SimpleNamespace(
            Redis=types.SimpleNamespace(from_url=lambda *_args, **_kwargs: redis_client)
        )
        identity = _identity(Path("/opt/oldsparky/platform"))
        with (
            mock.patch.object(liveness, "validate_worker_execution"),
            mock.patch.object(liveness, "_load_worker_environment"),
            mock.patch.dict(sys.modules, {"redis": redis_module}),
            mock.patch.dict(os.environ, self._parent_env(), clear=False),
            mock.patch.object(liveness, "_new_task_id", return_value=task_id),
        ):
            payload = liveness.run_liveness(
                identity,
                spawn_child=spawn,
                spawn_cleanup=cleanup_spawn,
            )

        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["cleanup"], "proven")
        self.assertEqual(redis_client.delete_calls, [])
        self.assertEqual(redis_client.exists_calls, [])
        self.assertIsNotNone(holder["process"].poll())
        self.assertIsNotNone(cleanup_holder["process"].poll())

    def test_terminal_success_with_fd_closed_descendant_is_cleanup_unproven(self) -> None:
        task_id = "platform-release-ping-" + "3" * 32
        key = b"celery-task-meta-" + task_id.encode("ascii")
        events = [
            {"event": "route", "ok": True},
            {"event": "published", "key": base64.b64encode(key).decode("ascii")},
            {
                "event": "terminal",
                "ok": True,
                "forget_ok": True,
                "key": base64.b64encode(key).decode("ascii"),
            },
        ]
        spawn, holder = self._spawn_control_child(events, fork_descendant=True)
        cleanup_spawn, cleanup_holder = self._spawn_cleanup_control_child()
        identity = _identity(Path("/opt/oldsparky/platform"))
        with (
            mock.patch.object(liveness, "validate_worker_execution"),
            mock.patch.object(liveness, "_load_worker_environment"),
            mock.patch.dict(os.environ, self._parent_env(), clear=False),
            mock.patch.object(liveness, "_new_task_id", return_value=task_id),
        ):
            payload = liveness.run_liveness(
                identity,
                spawn_child=spawn,
                spawn_cleanup=cleanup_spawn,
            )

        self.assertEqual(payload["status"], "cleanup_unproven")
        self.assertEqual(payload["cleanup"], "unproven")
        self.assertIsNotNone(holder["process"].poll())
        self.assertIsNotNone(cleanup_holder["process"].poll())
        self.assertFalse(liveness._group_exists(holder["pgid"]))

    def test_forget_failure_cannot_claim_a_successful_roundtrip(self) -> None:
        task_id = "platform-release-ping-" + "e" * 32
        key = b"celery-task-meta-" + task_id.encode("ascii")
        events = [
            {"event": "route", "ok": True},
            {"event": "prepared", "key": base64.b64encode(key).decode("ascii")},
            {"event": "published", "key": base64.b64encode(key).decode("ascii")},
            {
                "event": "terminal",
                "ok": True,
                "forget_ok": False,
                "key": base64.b64encode(key).decode("ascii"),
            },
        ]
        spawn, holder = self._spawn_control_child(events)
        cleanup_spawn, cleanup_holder = self._spawn_cleanup_control_child()
        redis_client = _FakeRedisClient()
        redis_module = types.SimpleNamespace(
            Redis=types.SimpleNamespace(from_url=lambda *_args, **_kwargs: redis_client)
        )
        identity = _identity(Path("/opt/oldsparky/platform"))
        with (
            mock.patch.object(liveness, "validate_worker_execution"),
            mock.patch.object(liveness, "_load_worker_environment"),
            mock.patch.dict(sys.modules, {"redis": redis_module}),
            mock.patch.dict(os.environ, self._parent_env(), clear=False),
            mock.patch.object(liveness, "_new_task_id", return_value=task_id),
        ):
            payload = liveness.run_liveness(
                identity,
                spawn_child=spawn,
                spawn_cleanup=cleanup_spawn,
            )

        self.assertEqual(payload["status"], "cleanup_unproven")
        self.assertEqual(payload["cleanup"], "unproven")
        self.assertEqual(redis_client.delete_calls, [])
        self.assertIsNotNone(holder["process"].poll())
        self.assertIsNotNone(cleanup_holder["process"].poll())

    def test_late_child_result_is_cleanup_unproven_even_when_key_is_absent(self) -> None:
        task_id = "platform-release-ping-" + "c" * 32
        key = b"celery-task-meta-" + task_id.encode("ascii")
        events = [
            {"event": "route", "ok": True},
            {"event": "prepared", "key": base64.b64encode(key).decode("ascii")},
            {"event": "published", "key": base64.b64encode(key).decode("ascii")},
        ]
        spawn, holder = self._spawn_control_child(events, sleep_seconds=5.0)
        cleanup_spawn, cleanup_holder = self._spawn_cleanup_control_child()
        redis_client = _FakeRedisClient()
        redis_module = types.SimpleNamespace(
            Redis=types.SimpleNamespace(from_url=lambda *_args, **_kwargs: redis_client)
        )
        identity = _identity(Path("/opt/oldsparky/platform"))
        with (
            mock.patch.object(liveness, "validate_worker_execution"),
            mock.patch.object(liveness, "_load_worker_environment"),
            mock.patch.dict(sys.modules, {"redis": redis_module}),
            mock.patch.dict(os.environ, self._parent_env(), clear=False),
            mock.patch.object(liveness, "_new_task_id", return_value=task_id),
            mock.patch.object(liveness, "CHILD_WORK_SECONDS", 0.1),
            mock.patch.object(liveness, "CHILD_STOP_GRACE_SECONDS", 0.05),
            mock.patch.object(liveness, "CHILD_KILL_GRACE_SECONDS", 0.05),
        ):
            payload = liveness.run_liveness(
                identity,
                spawn_child=spawn,
                spawn_cleanup=cleanup_spawn,
            )

        self.assertEqual(payload["status"], "cleanup_unproven")
        self.assertEqual(payload["cleanup"], "unproven")
        self.assertEqual(redis_client.delete_calls, [])
        self.assertIsNotNone(holder["process"].poll())
        self.assertIsNotNone(cleanup_holder["process"].poll())
        self.assertNotIn("pong", json.dumps(payload))

    def test_terminal_payload_then_hang_is_cleanup_unproven(self) -> None:
        task_id = "platform-release-ping-" + "f" * 32
        key = b"celery-task-meta-" + task_id.encode("ascii")
        events = [
            {"event": "route", "ok": True},
            {"event": "published", "key": base64.b64encode(key).decode("ascii")},
            {
                "event": "terminal",
                "ok": True,
                "forget_ok": True,
                "key": base64.b64encode(key).decode("ascii"),
            },
        ]
        spawn, holder = self._spawn_control_child(events, sleep_seconds=5.0)
        cleanup_spawn, cleanup_holder = self._spawn_cleanup_control_child()
        identity = _identity(Path("/opt/oldsparky/platform"))
        with (
            mock.patch.object(liveness, "validate_worker_execution"),
            mock.patch.object(liveness, "_load_worker_environment"),
            mock.patch.dict(os.environ, self._parent_env(), clear=False),
            mock.patch.object(liveness, "_new_task_id", return_value=task_id),
            mock.patch.object(liveness, "CHILD_WORK_SECONDS", 0.1),
            mock.patch.object(liveness, "CHILD_STOP_GRACE_SECONDS", 0.05),
            mock.patch.object(liveness, "CHILD_KILL_GRACE_SECONDS", 0.05),
        ):
            payload = liveness.run_liveness(
                identity,
                spawn_child=spawn,
                spawn_cleanup=cleanup_spawn,
            )

        self.assertEqual(payload["status"], "cleanup_unproven")
        self.assertEqual(payload["cleanup"], "unproven")
        self.assertEqual(payload["checks"]["task_result"], "failed")
        self.assertIsNotNone(holder["process"].poll())
        self.assertIsNotNone(cleanup_holder["process"].poll())

    def test_terminal_payload_then_nonzero_is_cleanup_unproven(self) -> None:
        task_id = "platform-release-ping-" + "1" * 32
        key = b"celery-task-meta-" + task_id.encode("ascii")
        events = [
            {"event": "route", "ok": True},
            {"event": "published", "key": base64.b64encode(key).decode("ascii")},
            {
                "event": "terminal",
                "ok": True,
                "forget_ok": True,
                "key": base64.b64encode(key).decode("ascii"),
            },
        ]
        spawn, holder = self._spawn_control_child(events, exit_code=7)
        cleanup_spawn, cleanup_holder = self._spawn_cleanup_control_child()
        identity = _identity(Path("/opt/oldsparky/platform"))
        with (
            mock.patch.object(liveness, "validate_worker_execution"),
            mock.patch.object(liveness, "_load_worker_environment"),
            mock.patch.dict(os.environ, self._parent_env(), clear=False),
            mock.patch.object(liveness, "_new_task_id", return_value=task_id),
        ):
            payload = liveness.run_liveness(
                identity,
                spawn_child=spawn,
                spawn_cleanup=cleanup_spawn,
            )

        self.assertEqual(payload["status"], "cleanup_unproven")
        self.assertEqual(payload["cleanup"], "unproven")
        self.assertEqual(payload["checks"]["task_result"], "failed")
        self.assertIsNotNone(holder["process"].poll())
        self.assertIsNotNone(cleanup_holder["process"].poll())

    def test_blocking_cleanup_is_killed_and_reaped_within_parent_deadline(self) -> None:
        task_id = "platform-release-ping-" + "2" * 32
        key = b"celery-task-meta-" + task_id.encode("ascii")
        events = [
            {"event": "route", "ok": True},
            {"event": "published", "key": base64.b64encode(key).decode("ascii")},
            {
                "event": "terminal",
                "ok": True,
                "forget_ok": True,
                "key": base64.b64encode(key).decode("ascii"),
            },
        ]
        spawn, child_holder = self._spawn_control_child(events)
        cleanup_spawn, cleanup_holder = self._spawn_cleanup_control_child(
            sleep_seconds=5.0,
            ignore_term=True,
        )
        identity = _identity(Path("/opt/oldsparky/platform"))
        started = time.monotonic()
        with (
            mock.patch.object(liveness, "validate_worker_execution"),
            mock.patch.object(liveness, "_load_worker_environment"),
            mock.patch.dict(os.environ, self._parent_env(), clear=False),
            mock.patch.object(liveness, "_new_task_id", return_value=task_id),
            mock.patch.object(liveness, "MAX_SECONDS", 0.6),
        ):
            payload = liveness.run_liveness(
                identity,
                spawn_child=spawn,
                spawn_cleanup=cleanup_spawn,
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0)
        self.assertEqual(payload["status"], "cleanup_unproven")
        self.assertEqual(payload["cleanup"], "unproven")
        self.assertIsNotNone(child_holder["process"].poll())
        self.assertIsNotNone(cleanup_holder["process"].poll())

    def test_cleanup_success_with_fd_closed_descendant_is_unproven(self) -> None:
        task_id = "platform-release-ping-" + "4" * 32
        key = b"celery-task-meta-" + task_id.encode("ascii")
        events = [
            {"event": "route", "ok": True},
            {"event": "published", "key": base64.b64encode(key).decode("ascii")},
            {
                "event": "terminal",
                "ok": True,
                "forget_ok": True,
                "key": base64.b64encode(key).decode("ascii"),
            },
        ]
        spawn, child_holder = self._spawn_control_child(events)
        cleanup_spawn, cleanup_holder = self._spawn_cleanup_control_child(
            fork_descendant=True,
        )
        identity = _identity(Path("/opt/oldsparky/platform"))
        with (
            mock.patch.object(liveness, "validate_worker_execution"),
            mock.patch.object(liveness, "_load_worker_environment"),
            mock.patch.dict(os.environ, self._parent_env(), clear=False),
            mock.patch.object(liveness, "_new_task_id", return_value=task_id),
        ):
            payload = liveness.run_liveness(
                identity,
                spawn_child=spawn,
                spawn_cleanup=cleanup_spawn,
            )

        self.assertEqual(payload["status"], "cleanup_unproven")
        self.assertEqual(payload["cleanup"], "unproven")
        self.assertIsNotNone(child_holder["process"].poll())
        self.assertIsNotNone(cleanup_holder["process"].poll())
        self.assertFalse(liveness._group_exists(cleanup_holder["pgid"]))

    def test_parent_cancellation_reaps_child_and_emits_no_exception(self) -> None:
        task_id = "platform-release-ping-" + "d" * 32
        key = b"celery-task-meta-" + task_id.encode("ascii")
        spawn, holder = self._spawn_control_child([], sleep_seconds=5.0)
        cleanup_spawn, cleanup_holder = self._spawn_cleanup_control_child()
        redis_client = _FakeRedisClient()
        redis_module = types.SimpleNamespace(
            Redis=types.SimpleNamespace(from_url=lambda *_args, **_kwargs: redis_client)
        )
        identity = _identity(Path("/opt/oldsparky/platform"))
        with (
            mock.patch.object(liveness, "validate_worker_execution"),
            mock.patch.object(liveness, "_load_worker_environment"),
            mock.patch.dict(sys.modules, {"redis": redis_module}),
            mock.patch.dict(os.environ, self._parent_env(), clear=False),
            mock.patch.object(liveness, "_new_task_id", return_value=task_id),
            mock.patch.object(liveness, "_collect_child", side_effect=KeyboardInterrupt),
        ):
            payload = liveness.run_liveness(
                identity,
                spawn_child=spawn,
                spawn_cleanup=cleanup_spawn,
            )

        self.assertEqual(payload["status"], "cleanup_unproven")
        self.assertEqual(payload["cleanup"], "unproven")
        self.assertIsNotNone(holder["process"].poll())
        self.assertIsNotNone(cleanup_holder["process"].poll())
        self.assertNotIn("KeyboardInterrupt", json.dumps(payload))

    def test_exception_after_spawn_finalizes_descendant_group(self) -> None:
        task_id = "platform-release-ping-" + "5" * 32
        spawn, holder = self._spawn_control_child(
            [],
            fork_descendant=True,
        )
        cleanup_spawn, cleanup_holder = self._spawn_cleanup_control_child()
        identity = _identity(Path("/opt/oldsparky/platform"))
        with (
            mock.patch.object(liveness, "validate_worker_execution"),
            mock.patch.object(liveness, "_load_worker_environment"),
            mock.patch.dict(os.environ, self._parent_env(), clear=False),
            mock.patch.object(liveness, "_new_task_id", return_value=task_id),
            mock.patch.object(liveness, "_collect_child", side_effect=KeyboardInterrupt),
        ):
            payload = liveness.run_liveness(
                identity,
                spawn_child=spawn,
                spawn_cleanup=cleanup_spawn,
            )

        self.assertEqual(payload["status"], "cleanup_unproven")
        self.assertEqual(payload["cleanup"], "unproven")
        self.assertIsNotNone(holder["process"].poll())
        self.assertIsNotNone(cleanup_holder["process"].poll())
        self.assertFalse(liveness._group_exists(holder["pgid"]))

    def test_malformed_child_control_is_redacted_and_cleanup_unproven(self) -> None:
        spawn, holder = self._spawn_control_child([])
        cleanup_spawn, cleanup_holder = self._spawn_cleanup_control_child()
        identity = _identity(Path("/opt/oldsparky/platform"))
        redis_client = _FakeRedisClient()
        redis_module = types.SimpleNamespace(
            Redis=types.SimpleNamespace(from_url=lambda *_args, **_kwargs: redis_client)
        )
        with (
            mock.patch.object(liveness, "validate_worker_execution"),
            mock.patch.object(liveness, "_load_worker_environment"),
            mock.patch.dict(sys.modules, {"redis": redis_module}),
            mock.patch.dict(os.environ, self._parent_env(), clear=False),
        ):
            payload = liveness.run_liveness(
                identity,
                spawn_child=spawn,
                spawn_cleanup=cleanup_spawn,
            )

        self.assertEqual(payload["status"], "cleanup_unproven")
        self.assertNotIn("redis://", json.dumps(payload))
        self.assertNotIn("platform-release-ping-", json.dumps(payload))
        self.assertIsNotNone(holder["process"].poll())
        self.assertIsNotNone(cleanup_holder["process"].poll())

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
        self.assertIn(
            'evidence_file="$(mktemp "$SHARED_DIR/.worker-liveness.XXXXXX")"',
            worker_block,
        )
        self.assertIn('noise_file="${evidence_file}.stderr"', worker_block)
        self.assertIn("run_bounded_command 25 /usr/sbin/runuser", worker_block)
        self.assertIn("Path(sys.argv[1]).read_bytes()", worker_block)
        self.assertIn("WORKER_LIVENESS schema=1 evidence=", worker_block)
        self.assertNotIn(">/dev/null", worker_block)
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
