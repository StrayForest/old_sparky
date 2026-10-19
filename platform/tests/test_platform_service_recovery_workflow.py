"""Contracts for the explicit API/worker production recovery mode."""

from __future__ import annotations

import json
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import yaml


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLATFORM_ROOT.parent
TOOL = PLATFORM_ROOT / "tools" / "platform_api_worker_service_recovery.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "platform-production-service-recovery.yml"


def _embedded_controller() -> bytes:
    source = WORKFLOW.read_text(encoding="utf-8")
    start = source.index("<<'SERVICE_RECOVERY'\n") + len("<<'SERVICE_RECOVERY'\n")
    end = source.index("          SERVICE_RECOVERY\n", start)
    lines = source[start:end].splitlines(keepends=True)
    return "".join(line[10:] if line.startswith("          ") else line for line in lines).encode()


class PlatformServiceRecoveryWorkflowTests(unittest.TestCase):
    def test_yaml_parsed_workflow_run_steps_pass_bash_syntax(self) -> None:
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for job_name, job in workflow["jobs"].items():
            for index, step in enumerate(job.get("steps", [])):
                script = step.get("run")
                if not isinstance(script, str):
                    continue
                with self.subTest(job=job_name, step=step.get("name", index)):
                    result = subprocess.run(
                        ["bash", "-n"],
                        input=script,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_secret_job_embeds_exact_reviewed_controller_without_checkout(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(_embedded_controller(), TOOL.read_bytes())
        self.assertIn("      - name: Recover deadlock-api and deadlock-worker", workflow)
        self.assertIn("if: ${{ inputs.mode == 'api-worker' }}", workflow)
        self.assertIn("RECOVER-DEADLOCK-API-WORKER", workflow)
        self.assertIn("RECOVER-DEADLOCK-WEB", workflow)
        self.assertNotIn("actions/checkout@", workflow)
        self.assertIn("platform_release_lock.sh", workflow)
        api_mode = workflow.split("- name: Recover deadlock-api and deadlock-worker", 1)[1]
        self.assertNotIn("curl ", api_mode)
        self.assertIn("/usr/bin/python3 -I -", api_mode)
        self.assertIn("stat.S_IMODE(metadata.st_mode) != 0o600", api_mode)
        self.assertIn("report.read(4097)", api_mode)
        self.assertLess(api_mode.index("platform_release_lock_supervisor_holds"), api_mode.index("<<'SERVICE_RECOVERY'"))
        self.assertIn("--expected-source-sha", api_mode)
        self.assertIn("--expected-release-slug", api_mode)

    def test_embedded_worker_ping_is_bounded_and_secret_output_is_not_projected(self) -> None:
        namespace = {"__name__": "service_recovery_test"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), namespace)
        calls: list[tuple[list[str], dict[str, object]]] = []

        def command(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0, stdout="WORKER_PING status=passed\n")

        with tempfile.TemporaryDirectory() as temporary:
            app_dir = Path(temporary) / "platform"
            release = app_dir / "releases" / "gha-1-2-aaaaaaaaaaaa"
            with patch.dict(namespace, {
                "_command": command,
                "_assert_service_unit": lambda **_kwargs: ({}, "disabled"),
                "time": SimpleNamespace(monotonic=lambda: 1.0, sleep=lambda _delay: None),
            }):
                self.assertTrue(namespace["_worker_ready"](app_dir, release, Path(temporary) / "systemd"))
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv[:4], ["/usr/sbin/runuser", "-u", "oldsparky-worker", "--"])
        self.assertIn("control.ping", argv[-1])
        self.assertIn("destination=[expected]", argv[-1])
        self.assertIn("celery_app", argv[-1])
        self.assertTrue(any(item.startswith("PLATFORM_EXPECTED_CELERY_NODE=celery@") for item in argv))
        self.assertLessEqual(kwargs["timeout"], 7.0)
        self.assertNotIn("secret", " ".join(argv).lower())

    def test_embedded_ping_rejects_a_different_worker_node(self) -> None:
        namespace = {"__name__": "service_recovery_test"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), namespace)
        code = ""

        def command(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            nonlocal code
            code = argv[-1]
            return SimpleNamespace(returncode=0, stdout="WORKER_PING status=passed\n")

        with tempfile.TemporaryDirectory() as temporary, patch.dict(namespace, {
            "_command": command,
            "_assert_service_unit": lambda **_kwargs: ({}, "disabled"),
            "time": SimpleNamespace(monotonic=lambda: 1.0, sleep=lambda _delay: None),
            "socket": SimpleNamespace(gethostname=lambda: "local-host"),
        }):
            self.assertTrue(namespace["_worker_ready"](Path(temporary), Path(temporary), Path(temporary)))

        app = ModuleType("apps")
        platform_worker = ModuleType("apps.platform_worker")
        worker = ModuleType("apps.platform_worker.worker")
        worker.celery_app = SimpleNamespace(
            control=SimpleNamespace(ping=lambda **_kwargs: [{"celery@other-host": "pong"}])
        )
        modules = {"apps": app, "apps.platform_worker": platform_worker, "apps.platform_worker.worker": worker}
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {"PLATFORM_EXPECTED_CELERY_NODE": "celery@local-host"}), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                exec(compile(code, "worker-readiness-probe.py", "exec"), {"__name__": "__main__"})
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(output.getvalue(), "WORKER_PING status=failed\n")

    def test_embedded_worker_ping_retries_within_fixed_deadline(self) -> None:
        namespace = {"__name__": "service_recovery_test"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), namespace)
        results = iter((SimpleNamespace(returncode=1, stdout=""), SimpleNamespace(returncode=0, stdout="WORKER_PING status=passed\n")))
        with tempfile.TemporaryDirectory() as temporary, patch.dict(namespace, {
            "_command": lambda *_args, **_kwargs: next(results),
            "_assert_service_unit": lambda **_kwargs: ({}, "disabled"),
            "time": SimpleNamespace(monotonic=lambda: 1.0, sleep=lambda _delay: None),
        }):
            self.assertTrue(namespace["_worker_ready"](Path(temporary), Path(temporary), Path(temporary)))

    def test_embedded_worker_probe_timeout_can_recover_before_deadline(self) -> None:
        namespace = {"__name__": "service_recovery_test"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), namespace)
        outcomes = iter((namespace["RecoveryRefusal"]("command_unavailable"), SimpleNamespace(returncode=0, stdout="WORKER_PING status=passed\n")))
        calls = 0

        def command(*_args: object, **_kwargs: object) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with tempfile.TemporaryDirectory() as temporary, patch.dict(namespace, {
            "_command": command,
            "_assert_service_unit": lambda **_kwargs: ({}, "disabled"),
            "time": SimpleNamespace(monotonic=lambda: 1.0, sleep=lambda _delay: None),
        }):
            self.assertTrue(namespace["_worker_ready"](Path(temporary), Path(temporary), Path(temporary)))
        self.assertEqual(calls, 2)

    def test_embedded_worker_readiness_stops_at_one_shared_deadline(self) -> None:
        namespace = {"__name__": "service_recovery_test"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), namespace)
        moments = iter((0.0, 8.0, 16.0, 24.0, 32.0, 40.0))
        calls = 0

        def command(*_args: object, **_kwargs: object) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            return SimpleNamespace(returncode=1, stdout="")

        with tempfile.TemporaryDirectory() as temporary, patch.dict(namespace, {
            "_command": command,
            "_assert_service_unit": lambda **_kwargs: ({}, "disabled"),
            "time": SimpleNamespace(monotonic=lambda: next(moments), sleep=lambda _delay: None),
        }):
            self.assertFalse(namespace["_worker_ready"](Path(temporary), Path(temporary), Path(temporary)))
        self.assertEqual(calls, 1)

    def test_input_and_unexpected_service_prestate_refuse_before_start(self) -> None:
        namespace = {"__name__": "service_recovery_test"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), namespace)
        calls: list[tuple[str, ...]] = []

        def fake_systemctl(_binary: str, *arguments: str, **_kwargs: object) -> SimpleNamespace:
            calls.append(arguments)
            return SimpleNamespace(returncode=1, stdout="")

        with tempfile.TemporaryDirectory() as temporary, patch.dict(namespace, {"_systemctl": fake_systemctl}):
            bad_sha = namespace["recover"](Path(temporary), "a" * 40, "gha-1-2-bbbbbbbbbbbb", Path(temporary))
        self.assertEqual(bad_sha, 1)
        self.assertFalse(any(arguments and arguments[0] == "start" for arguments in calls))


class PlatformServiceRecoveryControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "platform"
        self.releases = self.app / "releases"
        self.shared = self.app / "shared"
        self.systemd = self.root / "systemd"
        for directory in (self.app, self.releases, self.shared, self.systemd):
            directory.mkdir(parents=True, mode=0o755)
            directory.chmod(0o755)
        self.sha = "a" * 40
        self.slug = f"gha-1-2-{self.sha[:12]}"
        self.release = self.releases / self.slug
        self.previous = self.releases / "gha-1-1-bbbbbbbbbbbb"
        self._release(self.release, self.slug, self.sha)
        self._release(self.previous, self.previous.name, "b" * 40)
        (self.app / "current").symlink_to(Path("releases") / self.slug)
        (self.app / "previous").symlink_to(Path("releases") / self.previous.name)
        env_dir = self.shared / "env"
        env_dir.mkdir(mode=0o711)
        env_dir.chmod(0o711)
        self._file(self.shared / ".env.platform", b"PLATFORM_ENVIRONMENT=production\n", 0o600)
        for service in ("api", "worker"):
            self._file(env_dir / f"{service}.env", b"PLATFORM_ENVIRONMENT=production\n", 0o640)
        for service in ("deadlock-api", "deadlock-worker"):
            unit = f"[Service]\nUser=oldsparky-{service.removeprefix('deadlock-')}\nGroup=oldsparky-{service.removeprefix('deadlock-')}\n"
            self._file(self.release / "deploy" / "systemd" / f"{service}.service", unit.encode(), 0o644)
            target = self.systemd / f"{service}.service"
            target.parent.mkdir(parents=True, exist_ok=True)
            self._file(target, unit.encode(), 0o644)
        self.preflight = self.release / "tools" / "platform_release_preflight.sh"
        self._file(self.preflight, b"#!/bin/bash\nexit 0\n", 0o755)
        self.namespace = {"__name__": "service_recovery_test"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), self.namespace)

    @staticmethod
    def _file(path: Path, payload: bytes, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(mode)

    def _release(self, path: Path, slug: str, sha: str) -> None:
        path.mkdir(parents=True, mode=0o755)
        path.chmod(0o755)
        metadata = {"artifact_format_version": 1, "release_slug": slug, "source_git_commit": sha}
        self._file(path / "RELEASE.json", json.dumps(metadata, separators=(",", ":")).encode(), 0o444)

    def _states(self) -> dict[str, dict[str, str]]:
        return {
            service: {
                "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
                "Result": "success", "ExecMainStatus": "0", "MainPID": "0",
                "FragmentPath": str(self.systemd / f"{service}.service"), "DropInPaths": "",
                "UnitFileState": "disabled", "User": f"oldsparky-{service.removeprefix('deadlock-')}",
                "Group": f"oldsparky-{service.removeprefix('deadlock-')}",
            }
            for service in ("deadlock-api", "deadlock-worker")
        }

    def _run(self, *, worker_ping: bool = True, bad_prestate: bool = False) -> tuple[int, list[tuple[str, str]], dict[str, dict[str, str]]]:
        states = self._states()
        if bad_prestate:
            states["deadlock-api"]["ActiveState"] = "failed"
            states["deadlock-api"]["SubState"] = "failed"
            states["deadlock-api"]["Result"] = "exit-code"
        actions: list[tuple[str, str]] = []

        def systemctl(_binary: str, *args: str, **_kwargs: object) -> SimpleNamespace:
            action, unit, *rest = args
            service = unit.removesuffix(".service")
            if action == "show":
                output = "".join(f"{key}={value}\n" for key, value in states[service].items())
                return SimpleNamespace(returncode=0, stdout=output)
            if action == "is-enabled":
                return SimpleNamespace(returncode=1, stdout="disabled\n")
            if action == "is-active":
                return SimpleNamespace(returncode=0 if service not in states or states[service]["ActiveState"] == "active" else 3, stdout="")
            if action == "start":
                actions.append((action, service))
                states[service].update(ActiveState="active", SubState="running", MainPID="123", Result="success", ExecMainStatus="0")
                return SimpleNamespace(returncode=0, stdout="")
            if action == "stop":
                actions.append((action, service))
                states[service].update(ActiveState="inactive", SubState="dead", MainPID="0", Result="success", ExecMainStatus="0")
                return SimpleNamespace(returncode=0, stdout="")
            if action == "reset-failed":
                actions.append((action, service))
                states[service].update(Result="success", ExecMainStatus="0")
                return SimpleNamespace(returncode=0, stdout="")
            raise AssertionError(args)

        with (
            patch.dict(self.namespace, {
                "_systemctl": systemctl,
                "_assert_release_preflight": lambda *_args: None,
                "_assert_disk": lambda *_args: None,
                "_assert_active": lambda *_args: None,
                "_wait_api_ready": lambda: True,
                "_worker_ready": lambda *_args: worker_ping,
            }),
            patch.object(self.namespace["pwd"], "getpwnam", return_value=SimpleNamespace(pw_gid=os.getgid())),
        ):
            result = self.namespace["recover"](self.app, self.sha, self.slug, self.systemd)
        return result, actions, states

    def test_starts_only_api_then_worker_and_requires_ping(self) -> None:
        result, actions, states = self._run()
        self.assertEqual(result, 0)
        self.assertEqual([item for item in actions if item[0] == "start"], [
            ("start", "deadlock-api"), ("start", "deadlock-worker")
        ])
        self.assertEqual(states["deadlock-api"]["ActiveState"], "active")
        self.assertEqual(states["deadlock-worker"]["ActiveState"], "active")

    def test_worker_ping_failure_rolls_back_in_reverse_order(self) -> None:
        result, actions, states = self._run(worker_ping=False)
        self.assertEqual(result, 1)
        self.assertEqual([item for item in actions if item[0] == "start"], [
            ("start", "deadlock-api"), ("start", "deadlock-worker")
        ])
        self.assertEqual([item for item in actions if item[0] == "stop"], [
            ("stop", "deadlock-worker"), ("stop", "deadlock-api")
        ])
        self.assertEqual(states["deadlock-api"]["ActiveState"], "inactive")
        self.assertEqual(states["deadlock-worker"]["ActiveState"], "inactive")

    def test_unknown_failure_prestate_refuses_without_start(self) -> None:
        result, actions, _states = self._run(bad_prestate=True)
        self.assertEqual(result, 1)
        self.assertFalse(any(action == "start" for action, _service in actions))

    def test_service_unit_identity_uses_the_pinned_release_if_current_moves(self) -> None:
        service = "deadlock-api"
        other_unit = self.previous / "deploy" / "systemd" / f"{service}.service"
        self._file(other_unit, b"[Service]\nUser=unexpected\n", 0o644)
        (self.app / "current").unlink()
        (self.app / "current").symlink_to(Path("releases") / self.previous.name)
        unit_state = {
            "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
            "Result": "success", "ExecMainStatus": "0", "MainPID": "0",
            "FragmentPath": str(self.systemd / f"{service}.service"), "DropInPaths": "",
            "UnitFileState": "disabled", "User": "oldsparky-api", "Group": "oldsparky-api",
        }

        def systemctl(_binary: str, *args: str, **_kwargs: object) -> SimpleNamespace:
            if args[0] == "show":
                return SimpleNamespace(returncode=0, stdout="".join(f"{key}={value}\n" for key, value in unit_state.items()))
            if args[0] == "is-enabled":
                return SimpleNamespace(returncode=1, stdout="disabled\n")
            raise AssertionError(args)

        with patch.dict(self.namespace, {"_systemctl": systemctl}):
            state, enabled = self.namespace["_assert_service_unit"](
                release=self.release,
                systemd_dir=self.systemd,
                systemctl="/usr/bin/systemctl",
                service=service,
                before=True,
            )
        self.assertEqual(state["FragmentPath"], str(self.systemd / f"{service}.service"))
        self.assertEqual(enabled, "disabled")


if __name__ == "__main__":
    unittest.main()
