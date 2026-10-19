"""Contracts for the explicit API/worker production recovery mode."""

from __future__ import annotations

import json
import contextlib
import io
import os
from pathlib import Path
import pwd
import shutil
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


def _run_recovery_wrapper_as_nonroot(receipt: str, remote_status: int) -> subprocess.CompletedProcess[str]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        item
        for item in workflow["jobs"]["recover-services"]["steps"]
        if item.get("name") == "Recover deadlock-api, deadlock-worker, and optional nginx"
    )
    with tempfile.TemporaryDirectory(prefix="service-recovery-wrapper-") as temporary:
        root = Path(temporary)
        runner_temp = root / "runner-temp"
        ssh_dir = runner_temp / "platform-production-service-recovery-ssh"
        stub_dir = root / "bin"
        runner_temp.mkdir(parents=True)
        ssh_dir.mkdir()
        stub_dir.mkdir()
        for name in ("id_ed25519", "known_hosts"):
            path = ssh_dir / name
            path.write_text("test-only\n", encoding="ascii")
            path.chmod(0o600)
        ssh_stub = stub_dir / "ssh"
        ssh_stub.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "test \"$(id -u)\" -ne 0\n"
            "remote_payload=\"$(cat)\"\n"
            "[[ \"$remote_payload\" == *platform_release_lock_open* ]]\n"
            "[[ \"$remote_payload\" == *SERVICE_RECOVERY* ]]\n"
            "[[ \"$remote_payload\" == *--expected-source-sha* ]]\n"
            "printf '%s\\n' \"$TEST_STUB_RECEIPT\"\n"
            "exit \"$TEST_STUB_STATUS\"\n",
            encoding="ascii",
        )
        ssh_stub.chmod(0o755)
        github_env = runner_temp / "github_env"
        github_env.write_text("", encoding="ascii")
        uid = os.geteuid()
        gid = os.getegid()
        prefix: list[str] = []
        if uid == 0:
            if shutil.which("setpriv") is None:
                raise AssertionError("setpriv is required to exercise the wrapper as nonroot")
            account = pwd.getpwnam("nobody")
            uid, gid = account.pw_uid, account.pw_gid
            prefix = [
                "setpriv", "--reuid", str(uid), "--regid", str(gid), "--clear-groups", "--"
            ]
        for path in (root, runner_temp, ssh_dir, stub_dir):
            os.chown(path, uid, gid)
        for path in (ssh_dir / "id_ed25519", ssh_dir / "known_hosts", ssh_stub, github_env):
            os.chown(path, uid, gid)
        env = {
            "PATH": f"{stub_dir}:/usr/bin:/bin",
            "HOME": str(root),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_ENV": str(github_env),
            "SSH_DIR": str(ssh_dir),
            "PROD_SSH_USER": "test-user",
            "PROD_SSH_HOST": "test-host.invalid",
            "EXPECTED_SOURCE_SHA": "a" * 40,
            "EXPECTED_RELEASE_SLUG": f"gha-1-2-{'a' * 12}",
            "RECOVERY_MODE": "api-worker-nginx",
            "TEST_STUB_RECEIPT": receipt,
            "TEST_STUB_STATUS": str(remote_status),
        }
        return subprocess.run(
            [*prefix, "/bin/bash", "-e", "-o", "pipefail", "-c", step["run"]],
            text=True,
            capture_output=True,
            env=env,
            timeout=20,
            check=False,
        )


class PlatformServiceRecoveryWorkflowTests(unittest.TestCase):
    def _run_worker_readiness_bootstrap(self, env_contents: bytes | None) -> tuple[str, subprocess.CompletedProcess[str] | None, str]:
        namespace = {"__name__": "service_recovery_test"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), namespace)
        sentinel = "fixture-broker-value-must-not-be-printed"
        with tempfile.TemporaryDirectory(prefix="worker-runtime-bootstrap-") as temporary:
            app_dir = Path(temporary) / "platform"
            release = app_dir / "releases" / "gha-1-2-aaaaaaaaaaaa"
            shared = app_dir / "shared"
            tools = release / "tools"
            env_dir = shared / "env"
            python_packages = release / "python_packages"
            infra_package = python_packages / "platform_infra"
            worker_package = release / "apps" / "platform_worker"
            for directory in (tools, env_dir, infra_package, worker_package, shared / "venv" / "bin"):
                directory.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PLATFORM_ROOT / "tools" / "platform_runtime_common.sh", tools)
            shutil.copy2(PLATFORM_ROOT / "tools" / "platform_safe_env_exec.py", tools)
            python = shared / "venv" / "bin" / "python"
            python.symlink_to("/usr/bin/python3")
            env_file = env_dir / "worker.env"
            if env_contents is not None:
                env_file.write_bytes(env_contents)
                env_file.chmod(0o644)
            (python_packages / "__init__.py").write_text("", encoding="utf-8")
            (infra_package / "__init__.py").write_text("", encoding="utf-8")
            (infra_package / "config.py").write_text(
                "import os\nBROKER_SENTINEL = os.environ.get('PLATFORM_CELERY_BROKER_URL')\n",
                encoding="utf-8",
            )
            (worker_package.parent / "__init__.py").write_text("", encoding="utf-8")
            (worker_package / "__init__.py").write_text("", encoding="utf-8")
            (worker_package / "worker.py").write_text(
                "import os\n"
                "from python_packages.platform_infra.config import BROKER_SENTINEL\n"
                "class Control:\n"
                "    def ping(self, *, timeout, limit, destination):\n"
                f"        if BROKER_SENTINEL != {sentinel!r}: return []\n"
                "        expected = os.environ.get('PLATFORM_EXPECTED_CELERY_NODE')\n"
                "        return [{expected: {'ok': 'pong'}}] if destination == [expected] and timeout == 5.0 and limit == 1 else []\n"
                "class App:\n"
                "    control = Control()\n"
                "celery_app = App()\n",
                encoding="utf-8",
            )
            self.assertTrue(os.access(python, os.X_OK))
            if os.geteuid() == 0:
                account = pwd.getpwnam("nobody")
                uid, gid, username = account.pw_uid, account.pw_gid, account.pw_name
                for directory, _dirs, files in os.walk(temporary):
                    os.chown(directory, uid, gid)
                    for filename in files:
                        os.chown(Path(directory) / filename, uid, gid, follow_symlinks=False)
                    for name in _dirs:
                        os.chown(Path(directory) / name, uid, gid, follow_symlinks=False)
                os.chown(python, uid, gid, follow_symlinks=False)
                os.chmod(temporary, 0o755)
            else:
                username = pwd.getpwuid(os.geteuid()).pw_name
            clock = SimpleNamespace(now=0.0)
            captured: list[subprocess.CompletedProcess[str]] = []

            def command(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                adjusted = list(argv)
                if os.geteuid() == 0:
                    adjusted[2] = username
                else:
                    adjusted = adjusted[4:]
                result = subprocess.run(
                    adjusted,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=float(kwargs["timeout"]),
                    cwd=kwargs["cwd"],
                    env=kwargs["env"],
                )
                captured.append(result)
                if result.returncode != 0:
                    clock.now = 31.0
                return result

            with patch.dict(namespace, {
                "_command": command,
                "_assert_service_unit": lambda **_kwargs: ({}, "disabled"),
                "time": SimpleNamespace(monotonic=lambda: clock.now, sleep=lambda _delay: None),
            }):
                ready = namespace["_worker_ready"](app_dir, release, Path(temporary) / "systemd")
            return ready, (captured[-1] if captured else None), sentinel

    def test_worker_readiness_uses_exact_safe_runtime_bootstrap_and_release_imports(self) -> None:
        sentinel = "fixture-broker-value-must-not-be-printed"
        env_contents = f"PLATFORM_CELERY_BROKER_URL={sentinel}\n".encode()
        ready, result, observed_sentinel = self._run_worker_readiness_bootstrap(env_contents)
        self.assertEqual(ready, "passed")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "WORKER_PING status=passed\n")
        self.assertNotIn(observed_sentinel, result.stdout + result.stderr)

    def test_worker_readiness_fails_closed_for_missing_or_malformed_isolated_env(self) -> None:
        for env_contents in (None, b"PLATFORM_TEST_BROKER_SENTINEL='unterminated\n"):
            with self.subTest(env_contents=env_contents):
                ready, result, sentinel = self._run_worker_readiness_bootstrap(env_contents)
                self.assertEqual(ready, "bootstrap_failure")
                self.assertIsNotNone(result)
                assert result is not None
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(sentinel, result.stdout + result.stderr)

    def test_worker_readiness_matches_pinned_celery_ping_envelope(self) -> None:
        from celery.worker.control import ping as celery_ping

        expected = "celery@contract-host"
        reply = celery_ping(None)
        self.assertEqual(reply, {"ok": "pong"})
        namespace: dict[str, object] = {"__name__": "service_recovery_contract"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), namespace)

        def run_probe(worker_reply: object) -> tuple[int, str]:
            worker_module = ModuleType("apps.platform_worker.worker")
            worker_module.celery_app = SimpleNamespace(
                control=SimpleNamespace(
                    ping=lambda **_kwargs: [{expected: worker_reply}],
                ),
            )
            packages = {
                "apps": ModuleType("apps"),
                "apps.platform_worker": ModuleType("apps.platform_worker"),
                "apps.platform_worker.worker": worker_module,
            }
            output = io.StringIO()
            with patch.dict(sys.modules, packages), patch.dict(
                os.environ, {"PLATFORM_EXPECTED_CELERY_NODE": expected}
            ), contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    exec(
                        compile(namespace["WORKER_READINESS_CODE"], "worker-readiness-probe.py", "exec"),
                        {"__name__": "__main__"},
                    )
            return int(raised.exception.code), output.getvalue()

        self.assertEqual(run_probe(reply), (0, "WORKER_PING status=passed\n"))
        self.assertEqual(
            run_probe("pong"),
            (1, "WORKER_PING status=failed reason=unexpected_reply\n"),
        )

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
        self.assertIn("      - name: Recover deadlock-api, deadlock-worker, and optional nginx", workflow)
        self.assertIn("inputs.mode == 'api-worker' || inputs.mode == 'api-worker-nginx'", workflow)
        self.assertIn("RECOVER-DEADLOCK-API-WORKER", workflow)
        self.assertIn("RECOVER-DEADLOCK-API-WORKER-NGINX", workflow)
        self.assertIn("RECOVER-DEADLOCK-WEB", workflow)
        self.assertNotIn("actions/checkout@", workflow)
        self.assertIn("platform_release_lock.sh", workflow)
        api_mode = workflow.split("- name: Recover deadlock-api, deadlock-worker, and optional nginx", 1)[1]
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
                self.assertEqual(namespace["_worker_ready"](app_dir, release, Path(temporary) / "systemd"), "passed")
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv[:4], ["/usr/sbin/runuser", "-u", "oldsparky-worker", "--"])
        self.assertEqual(argv[-4], namespace["WORKER_READINESS_BOOTSTRAP"])
        self.assertIn("platform_runtime_common.sh", argv[-4])
        self.assertIn("platform_load_env_file", argv[-4])
        self.assertIn("PYTHONPATH=\"$PLATFORM_ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}\"", argv[-4])
        self.assertIn("control.ping", argv[-2])
        self.assertIn("destination=[expected]", argv[-2])
        self.assertIn("celery_app", argv[-2])
        self.assertTrue(argv[-1].startswith("celery@"))
        self.assertEqual(kwargs["timeout"], namespace["WORKER_PROBE_TIMEOUT"])
        self.assertGreater(kwargs["timeout"], 8.27)
        self.assertNotIn("secret", " ".join(argv).lower())

    def test_proxy_readiness_uses_loopback_tls_and_bounds_health_body(self) -> None:
        namespace = {"__name__": "service_recovery_test"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), namespace)
        events: list[tuple[object, ...]] = []

        class FakeSocket:
            def settimeout(self, timeout: float) -> None:
                events.append(("timeout", timeout))

            def sendall(self, request: bytes) -> None:
                events.append(("request", request))

            def getpeercert(self, *, binary_form: bool) -> bytes:
                self.assert_binary_form = binary_form
                return b"origin-cert"

            def close(self) -> None:
                events.append(("close",))

        tls_socket = FakeSocket()

        class FakeContext:
            def wrap_socket(self, _sock: object, *, server_hostname: str) -> FakeSocket:
                events.append(("sni", server_hostname))
                return tls_socket

        class FakeResponse:
            status = 200

            def __init__(self, _sock: object) -> None:
                pass

            def begin(self) -> None:
                pass

            def read(self, limit: int) -> bytes:
                events.append(("read", limit))
                return b'{"status":"ok","service":"deadlock-platform-api"}'

        fake_ssl = SimpleNamespace(PROTOCOL_TLS_CLIENT=1, SSLContext=lambda _protocol: FakeContext(), CERT_NONE=0)
        fake_http = SimpleNamespace(HTTPResponse=FakeResponse, HTTPException=Exception)
        fake_socket = SimpleNamespace(
            create_connection=lambda address, timeout: events.append(("connect", address, timeout)) or FakeSocket()
        )
        with patch.dict(namespace, {
            "ssl": fake_ssl,
            "socket": fake_socket,
            "http": SimpleNamespace(client=fake_http),
            "_nginx_certificate_der": lambda: b"origin-cert",
        }):
            self.assertTrue(namespace["_proxy_api_ready"]())
        self.assertIn(("connect", ("127.0.0.1", 443), 2.0), events)
        self.assertIn(("sni", "old-sparky.com"), events)
        self.assertIn(("read", 4097), events)
        request = next(item[1] for item in events if item[0] == "request")
        self.assertIn(b"Host: old-sparky.com", request)
        self.assertIn(b"/api/v1/health/ready", request)

    def test_nginx_configuration_checks_origin_certificate_and_private_key_identity(self) -> None:
        namespace = {"__name__": "service_recovery_test"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), namespace)
        calls: list[list[str]] = []

        def command(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="nginx config\n")

        with patch.dict(namespace, {
            "_command": command,
            "_nginx_certificate_der": lambda: b"origin-cert",
            "_owned_file_identity": lambda path, **kwargs: (7, 11, 0, 0, 1, 0o600, 128, 13, 17),
        }):
            namespace["_nginx_configuration_identity"]()

        openssl_calls = [argv for argv in calls if argv[0] == "/usr/bin/openssl"]
        self.assertEqual(len(openssl_calls), 2)
        self.assertIn(["-checkhost", "old-sparky.com"], [argv[-2:] for argv in openssl_calls])
        self.assertIn(["-checkend", "2592000"], [argv[-2:] for argv in openssl_calls])
        self.assertEqual(namespace["NGINX_KEY_PATH"].name, "old-sparky.com-origin.key")
        self.assertEqual(namespace["NGINX_CERT_MIN_REMAINING_SECONDS"], 30 * 86_400)

        calls.clear()
        def expired_certificate(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            return SimpleNamespace(
                returncode=1 if "-checkend" in argv else 0,
                stdout="nginx config\n" if argv[:2] == ["/usr/sbin/nginx", "-T"] else "",
            )

        with patch.dict(namespace, {
            "_command": expired_certificate,
            "_nginx_certificate_der": lambda: b"origin-cert",
            "_owned_file_identity": lambda _path, **_kwargs: (7, 11, 0, 0, 1, 0o600, 128, 13, 17),
        }):
            with self.assertRaises(namespace["RecoveryRefusal"]) as error:
                namespace["_nginx_configuration_identity"]()
        self.assertEqual(error.exception.stage, "nginx_certificate_validity")

    def test_embedded_ping_rejects_a_different_worker_node(self) -> None:
        namespace = {"__name__": "service_recovery_test"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), namespace)
        code = ""

        def command(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            nonlocal code
            code = argv[-2]
            return SimpleNamespace(returncode=0, stdout="WORKER_PING status=passed\n")

        with tempfile.TemporaryDirectory() as temporary, patch.dict(namespace, {
            "_command": command,
            "_assert_service_unit": lambda **_kwargs: ({}, "disabled"),
            "time": SimpleNamespace(monotonic=lambda: 1.0, sleep=lambda _delay: None),
            "socket": SimpleNamespace(gethostname=lambda: "local-host"),
        }):
            self.assertEqual(
                namespace["_worker_ready"](Path(temporary), Path(temporary), Path(temporary)),
                "passed",
            )

        app = ModuleType("apps")
        platform_worker = ModuleType("apps.platform_worker")
        worker = ModuleType("apps.platform_worker.worker")
        worker.celery_app = SimpleNamespace(
            control=SimpleNamespace(ping=lambda **_kwargs: [{"celery@other-host": {"ok": "pong"}}])
        )
        modules = {"apps": app, "apps.platform_worker": platform_worker, "apps.platform_worker.worker": worker}
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {"PLATFORM_EXPECTED_CELERY_NODE": "celery@local-host"}), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                exec(compile(code, "worker-readiness-probe.py", "exec"), {"__name__": "__main__"})
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(output.getvalue(), "WORKER_PING status=failed reason=unexpected_reply\n")

    def test_embedded_ping_projects_closed_outcomes_and_rejects_legacy_pong(self) -> None:
        namespace = {"__name__": "service_recovery_test"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), namespace)
        secret = "PRIVATE_PING_EXCEPTION_SENTINEL"

        def raise_secret(**_kwargs: object) -> list[dict[str, str]]:
            raise RuntimeError(secret)

        cases = (
            ([{"celery@local-host": "pong"}], "unexpected_reply"),
            ([], "no_reply"),
            (raise_secret, "exception"),
        )
        for reply, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                app = ModuleType("apps")
                platform_worker = ModuleType("apps.platform_worker")
                worker = ModuleType("apps.platform_worker.worker")
                ping = reply if callable(reply) else lambda **_kwargs: reply
                worker.celery_app = SimpleNamespace(control=SimpleNamespace(ping=ping))
                modules = {"apps": app, "apps.platform_worker": platform_worker, "apps.platform_worker.worker": worker}
                output = io.StringIO()
                with patch.dict(sys.modules, modules), patch.dict(os.environ, {"PLATFORM_EXPECTED_CELERY_NODE": "celery@local-host"}), contextlib.redirect_stdout(output):
                    with self.assertRaises(SystemExit) as raised:
                        exec(compile(namespace["WORKER_READINESS_CODE"], "worker-readiness-probe.py", "exec"), {"__name__": "__main__"})
                self.assertEqual(raised.exception.code, 1)
                self.assertEqual(output.getvalue(), f"WORKER_PING status=failed reason={expected_reason}\n")
                self.assertNotIn(secret, output.getvalue())

    def test_embedded_worker_ping_retries_within_fixed_deadline(self) -> None:
        namespace = {"__name__": "service_recovery_test"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), namespace)
        results = iter((
            SimpleNamespace(returncode=1, stdout="WORKER_PING status=failed reason=no_reply\n"),
            SimpleNamespace(returncode=0, stdout="WORKER_PING status=passed\n"),
        ))
        with tempfile.TemporaryDirectory() as temporary, patch.dict(namespace, {
            "_command": lambda *_args, **_kwargs: next(results),
            "_assert_service_unit": lambda **_kwargs: ({}, "disabled"),
            "time": SimpleNamespace(monotonic=lambda: 1.0, sleep=lambda _delay: None),
        }):
            self.assertEqual(namespace["_worker_ready"](Path(temporary), Path(temporary), Path(temporary)), "passed")

    def test_embedded_worker_probe_timeout_can_recover_before_deadline(self) -> None:
        namespace = {"__name__": "service_recovery_test"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), namespace)
        outcomes = iter((namespace["RecoveryRefusal"]("command_timeout"), SimpleNamespace(returncode=0, stdout="WORKER_PING status=passed\n")))
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
            self.assertEqual(namespace["_worker_ready"](Path(temporary), Path(temporary), Path(temporary)), "passed")
        self.assertEqual(calls, 2)

    def test_embedded_worker_readiness_stops_at_one_shared_deadline(self) -> None:
        namespace = {"__name__": "service_recovery_test"}
        exec(compile(_embedded_controller(), "embedded-service-recovery.py", "exec"), namespace)
        clock = SimpleNamespace(now=0.0)
        calls = 0

        def command(*_args: object, **_kwargs: object) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            clock.now = 31.0
            raise namespace["RecoveryRefusal"]("command_timeout")

        with tempfile.TemporaryDirectory() as temporary, patch.dict(namespace, {
            "_command": command,
            "_assert_service_unit": lambda **_kwargs: ({}, "disabled"),
            "time": SimpleNamespace(monotonic=lambda: clock.now, sleep=lambda _delay: None),
        }):
            self.assertEqual(namespace["_worker_ready"](Path(temporary), Path(temporary), Path(temporary)), "timeout")
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
        self.nginx_unit = self.root / "usr" / "lib" / "systemd" / "system" / "nginx.service"
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
        self.timer_names = (
            "deadlock-maintenance.timer",
            "deadlock-logrotate.timer",
            "deadlock-cloudflare-ips.timer",
            "deadlock-health-monitor.timer",
        )
        for unit in self.timer_names:
            unit_bytes = f"[Unit]\nDescription={unit}\n[Timer]\nOnCalendar=daily\n".encode()
            self._file(self.release / "deploy" / "systemd" / unit, unit_bytes, 0o644)
            self._file(self.systemd / unit, unit_bytes, 0o644)
        self._file(self.nginx_unit, b"[Unit]\nDescription=nginx\n[Service]\nExecStart=/usr/sbin/nginx\n", 0o644)
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

    def _states(self, *, nginx_active: bool = False) -> dict[str, dict[str, str]]:
        states = {
            service: {
                "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
                "Result": "success", "ExecMainStatus": "0", "MainPID": "0",
                "FragmentPath": str(self.systemd / f"{service}.service"), "DropInPaths": "",
                "UnitFileState": "disabled", "User": f"oldsparky-{service.removeprefix('deadlock-')}",
                "Group": f"oldsparky-{service.removeprefix('deadlock-')}",
            }
            for service in ("deadlock-api", "deadlock-worker")
        }

        states["nginx"] = {
            "LoadState": "loaded",
            "ActiveState": "active" if nginx_active else "inactive",
            "SubState": "running" if nginx_active else "dead",
            "Result": "success",
            "ExecMainStatus": "0",
            "MainPID": "234" if nginx_active else "0",
            "FragmentPath": str(self.nginx_unit),
            "DropInPaths": "",
            "UnitFileState": "enabled",
            "User": "",
            "Group": "",
        }
        for unit in self.timer_names:
            states[unit] = {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "SubState": "dead",
                "Result": "success",
                "ExecMainStatus": "0",
                "MainPID": "0",
                "FragmentPath": str(self.systemd / unit),
                "DropInPaths": "",
                "UnitFileState": "disabled",
                "User": "",
                "Group": "",
            }
        return states

    def _run(
        self,
        *,
        worker_ping: bool = True,
        bad_prestate: bool = False,
        mode: str = "api-worker",
        nginx_active: bool = False,
        proxy_ready: bool = True,
        nginx_start_ok: bool = True,
        nginx_partial_start: bool = False,
        nginx_config_ok: bool = True,
        nginx_config_drift: bool = False,
        timer_drift: bool = False,
        timer_bad_prestate: bool = False,
        timer_identity_mismatch: bool = False,
        nginx_bad_identity: bool = False,
        nginx_bad_prestate: bool = False,
    ) -> tuple[int, list[tuple[str, str]], dict[str, dict[str, str]]]:
        states = self._states(nginx_active=nginx_active)
        if nginx_bad_identity:
            states["nginx"]["DropInPaths"] = "/run/systemd/system/nginx.service.d/50-extra.conf"
        if nginx_bad_prestate:
            states["nginx"].update(ActiveState="failed", SubState="failed", Result="exit-code", MainPID="0")
        if timer_bad_prestate:
            states[self.timer_names[0]].update(ActiveState="failed", SubState="failed", Result="exit-code")
        if timer_identity_mismatch:
            self._file(self.systemd / self.timer_names[0], b"[Unit]\nDescription=unexpected timer\n", 0o644)
        if bad_prestate:
            states["deadlock-api"]["ActiveState"] = "failed"
            states["deadlock-api"]["SubState"] = "failed"
            states["deadlock-api"]["Result"] = "exit-code"
        actions: list[tuple[str, str]] = []
        nginx_config_calls = 0

        def nginx_config_identity() -> str:
            nonlocal nginx_config_calls
            nginx_config_calls += 1
            if not nginx_config_ok:
                raise self.namespace["RecoveryRefusal"]("nginx_config_test")
            if nginx_config_drift and nginx_config_calls > 1:
                return "changed-nginx-config-digest"
            return "nginx-config-digest"

        receipt = io.StringIO()

        def systemctl(_binary: str, *args: str, **_kwargs: object) -> SimpleNamespace:
            action, unit, *rest = args
            service = unit.removesuffix(".service")
            if action == "show":
                requested = next(
                    item.partition("=")[2].split(",")
                    for item in rest
                    if item.startswith("--property=")
                )
                output = "".join(
                    f"{key}={states[service][key]}\n"
                    for key in requested
                    if key in states[service]
                )
                return SimpleNamespace(returncode=0, stdout=output)
            if action == "is-enabled":
                enabled = states[service]["UnitFileState"] == "enabled"
                return SimpleNamespace(returncode=0 if enabled else 1, stdout="enabled\n" if enabled else "disabled\n")
            if action == "is-active":
                return SimpleNamespace(returncode=0 if service not in states or states[service]["ActiveState"] == "active" else 3, stdout="")
            if action == "start":
                actions.append((action, service))
                if service == "nginx" and not nginx_start_ok:
                    if nginx_partial_start:
                        states[service].update(ActiveState="active", SubState="running", MainPID="123")
                    return SimpleNamespace(returncode=1, stdout="")
                states[service].update(ActiveState="active", SubState="running", MainPID="123", Result="success", ExecMainStatus="0")
                if service == "deadlock-worker" and timer_drift:
                    states[self.timer_names[0]].update(ActiveState="active", SubState="waiting")
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
                "_worker_ready": lambda *_args: "passed" if worker_ping else "no_reply",
                "_wait_proxy_api_ready": lambda: proxy_ready,
                "_nginx_configuration_identity": nginx_config_identity,
                "NGINX_UNIT_PATH": self.nginx_unit,
            }),
            patch.object(self.namespace["pwd"], "getpwnam", return_value=SimpleNamespace(pw_gid=os.getgid())),
            contextlib.redirect_stdout(receipt),
        ):
            result = self.namespace["recover"](self.app, self.sha, self.slug, self.systemd, mode=mode)
        self.last_receipt = receipt.getvalue()
        return result, actions, states

    def test_pointer_identity_accepts_the_canonical_absolute_release_targets(self) -> None:
        for name, release in (("current", self.release), ("previous", self.previous)):
            pointer = self.app / name
            pointer.unlink()
            pointer.symlink_to(release)
            target, resolved, _identity = self.namespace["_pointer_identity"](self.app, name)
            self.assertEqual(target, str(release))
            self.assertEqual(resolved, str(release))

    def test_recovery_succeeds_with_deploy_generated_absolute_pointers(self) -> None:
        (self.app / "current").unlink()
        (self.app / "previous").unlink()
        (self.app / "current").symlink_to(self.release)
        (self.app / "previous").symlink_to(self.previous)
        result, actions, _states = self._run(mode="api-worker-nginx")
        self.assertEqual(result, 0)
        self.assertEqual([service for action, service in actions if action == "start"], [
            "deadlock-api", "deadlock-worker", "nginx"
        ])

    def test_pointer_identity_rejects_traversal_and_release_alias_symlinks(self) -> None:
        pointer = self.app / "current"
        pointer.unlink()
        pointer.symlink_to("releases/../shared")
        with self.assertRaises(self.namespace["RecoveryRefusal"]) as caught:
            self.namespace["_pointer_identity"](self.app, "current")
        self.assertEqual(caught.exception.stage, "pointer_identity")

        pointer.unlink()
        alias = self.releases / "alias"
        alias.symlink_to(self.release)
        pointer.symlink_to("releases/alias")
        with self.assertRaises(self.namespace["RecoveryRefusal"]) as caught:
            self.namespace["_pointer_identity"](self.app, "current")
        self.assertEqual(caught.exception.stage, "pointer_identity")

    def test_yaml_wrapper_accepts_controller_receipt_as_nonroot_and_projects_failure_stage(self) -> None:
        for name, release in (("current", self.release), ("previous", self.previous)):
            pointer = self.app / name
            pointer.unlink()
            pointer.symlink_to(release)
        controller_result, _actions, _states = self._run(mode="api-worker-nginx")
        self.assertEqual(controller_result, 0)
        passed = _run_recovery_wrapper_as_nonroot(self.last_receipt, controller_result)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertIn("PRODUCTION_API_WORKER_RECOVERY schema=1 status=passed", passed.stdout)
        self.assertNotIn("mode=api-worker-nginx", passed.stdout)

        current = self.app / "current"
        current.unlink()
        current.symlink_to("releases/../shared")
        controller_result, _actions, _states = self._run(mode="api-worker-nginx")
        self.assertEqual(controller_result, 1)
        self.assertIn("stage=pointer_identity", self.last_receipt)
        self.assertIn("source_sha=unavailable release_slug=unavailable", self.last_receipt)
        early_failure = _run_recovery_wrapper_as_nonroot(self.last_receipt, controller_result)
        self.assertEqual(early_failure.returncode, 1)
        self.assertIn("stage=pointer_identity observed_identity=unavailable", early_failure.stdout)
        self.assertNotIn(self.sha, early_failure.stdout)

    def test_yaml_wrapper_rejects_malformed_receipt_without_leaking_payload(self) -> None:
        sentinel = "SECRET_SENTINEL_DO_NOT_PROJECT"
        malformed = _run_recovery_wrapper_as_nonroot(sentinel, 1)
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("error_class=receipt", malformed.stdout)
        self.assertNotIn(sentinel, malformed.stdout)
        self.assertNotIn(sentinel, malformed.stderr)

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
        self.assertIn("stage=worker_readiness_no_reply", self.last_receipt)
        projected = _run_recovery_wrapper_as_nonroot(self.last_receipt, result)
        self.assertNotEqual(projected.returncode, 0)
        self.assertIn("stage=worker_readiness_no_reply", projected.stdout)
        self.assertEqual(states["deadlock-api"]["ActiveState"], "inactive")
        self.assertEqual(states["deadlock-worker"]["ActiveState"], "inactive")

    def test_unknown_failure_prestate_refuses_without_start(self) -> None:
        result, actions, _states = self._run(bad_prestate=True)
        self.assertEqual(result, 1)
        self.assertFalse(any(action == "start" for action, _service in actions))

    def test_nginx_mode_starts_api_worker_then_nginx_and_checks_proxy(self) -> None:
        result, actions, states = self._run(mode="api-worker-nginx")
        self.assertEqual(result, 0)
        self.assertEqual([service for action, service in actions if action == "start"], [
            "deadlock-api", "deadlock-worker", "nginx"
        ])
        self.assertEqual(states["nginx"]["ActiveState"], "active")
        self.assertIn("proxy_readiness=loopback_pinned_tls_api_health timers=preserved", self.last_receipt)

    def test_nginx_mode_preserves_preexisting_active_nginx(self) -> None:
        result, actions, states = self._run(mode="api-worker-nginx", nginx_active=True)
        self.assertEqual(result, 0)
        self.assertNotIn(("start", "nginx"), actions)
        self.assertNotIn(("stop", "nginx"), actions)
        self.assertEqual(states["nginx"]["ActiveState"], "active")

    def test_nginx_readiness_failure_rolls_back_started_units_in_reverse_order(self) -> None:
        result, actions, states = self._run(mode="api-worker-nginx", proxy_ready=False)
        self.assertEqual(result, 1)
        self.assertEqual([service for action, service in actions if action == "stop"], [
            "nginx", "deadlock-worker", "deadlock-api"
        ])
        self.assertEqual(states["nginx"]["ActiveState"], "inactive")

    def test_nginx_start_failure_without_activation_rolls_back_only_app_units(self) -> None:
        result, actions, states = self._run(mode="api-worker-nginx", nginx_start_ok=False)
        self.assertEqual(result, 1)
        self.assertEqual([service for action, service in actions if action == "stop"], [
            "deadlock-worker", "deadlock-api"
        ])
        self.assertEqual(states["nginx"]["ActiveState"], "inactive")

    def test_nginx_partial_start_failure_is_unwound_in_reverse_order(self) -> None:
        result, actions, states = self._run(
            mode="api-worker-nginx", nginx_start_ok=False, nginx_partial_start=True
        )
        self.assertEqual(result, 1)
        self.assertEqual([service for action, service in actions if action == "stop"], [
            "nginx", "deadlock-worker", "deadlock-api"
        ])
        self.assertEqual(states["nginx"]["ActiveState"], "inactive")

    def test_invalid_nginx_config_refuses_before_starting_services(self) -> None:
        result, actions, _states = self._run(mode="api-worker-nginx", nginx_config_ok=False)
        self.assertEqual(result, 1)
        self.assertFalse(any(action == "start" for action, _service in actions))

    def test_nginx_unit_dropin_or_unknown_prestate_refuses_before_start(self) -> None:
        for options in ({"nginx_bad_identity": True}, {"nginx_bad_prestate": True}):
            with self.subTest(options=options):
                result, actions, _states = self._run(mode="api-worker-nginx", **options)
                self.assertEqual(result, 1)
                self.assertFalse(any(action == "start" for action, _service in actions))

    def test_timer_state_change_is_detected_without_timer_mutation(self) -> None:
        result, actions, states = self._run(timer_drift=True)
        self.assertEqual(result, 1)
        self.assertFalse(any(action in {"start", "stop"} and service.endswith(".timer") for action, service in actions))
        self.assertEqual(states[self.timer_names[0]]["ActiveState"], "active")
        self.assertEqual([service for action, service in actions if action == "stop"], [
            "deadlock-worker", "deadlock-api"
        ])

    def test_unknown_timer_prestate_refuses_before_start(self) -> None:
        result, actions, _states = self._run(timer_bad_prestate=True)
        self.assertEqual(result, 1)
        self.assertFalse(any(action == "start" for action, _service in actions))
        self.assertIn("stage=timer_state", self.last_receipt)
        self.assertIn("timers=not_checked", self.last_receipt)

    def test_timer_unit_identity_mismatch_refuses_before_start(self) -> None:
        result, actions, _states = self._run(timer_identity_mismatch=True)
        self.assertEqual(result, 1)
        self.assertFalse(any(action == "start" for action, _service in actions))
        self.assertIn("stage=timer_unit_identity", self.last_receipt)

    def test_timer_snapshot_uses_only_timer_applicable_systemd_properties(self) -> None:
        properties = self.namespace["TIMER_SHOW_PROPERTIES"]

        def systemctl(_binary: str, *args: str, **_kwargs: object) -> SimpleNamespace:
            if args[0] == "show":
                unit = args[1]
                active = unit != "deadlock-maintenance.timer"
                values = {
                    "LoadState": "loaded",
                    "ActiveState": "active" if active else "inactive",
                    "SubState": "waiting" if active else "dead",
                    "Result": "success",
                    "FragmentPath": str(self.systemd / unit),
                    "DropInPaths": "",
                    "UnitFileState": "enabled" if active else "disabled",
                }
                output = "".join(f"{key}={values[key]}\n" for key in properties)
                return SimpleNamespace(returncode=0, stdout=output)
            if args[0] == "is-enabled":
                enabled = args[1] != "deadlock-maintenance.timer"
                return SimpleNamespace(
                    returncode=0 if enabled else 1,
                    stdout="enabled\n" if enabled else "disabled\n",
                )
            raise AssertionError(args)

        with patch.dict(self.namespace, {"_systemctl": systemctl}):
            snapshot = self.namespace["_timer_snapshot"](
                self.release, self.systemd, "/usr/bin/systemctl"
            )
        self.assertEqual(set(snapshot), set(self.timer_names))
        self.assertTrue(all(len(value) == len(properties) + 2 for value in snapshot.values()))
        self.assertEqual(snapshot["deadlock-maintenance.timer"][1], "disabled")
        self.assertEqual(snapshot["deadlock-logrotate.timer"][1], "enabled")

    def test_nginx_config_drift_refuses_before_starting_proxy(self) -> None:
        result, actions, states = self._run(mode="api-worker-nginx", nginx_config_drift=True)
        self.assertEqual(result, 1)
        self.assertNotIn(("start", "nginx"), actions)
        self.assertEqual([service for action, service in actions if action == "stop"], [
            "deadlock-worker", "deadlock-api"
        ])
        self.assertEqual(states["nginx"]["ActiveState"], "inactive")

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
