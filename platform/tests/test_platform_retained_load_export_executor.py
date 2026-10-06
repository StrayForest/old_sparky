from __future__ import annotations

import io
import os
from pathlib import Path
import pwd
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from tools import platform_retained_load_export_executor as executor
from tools import platform_capture_retained_recovery_stderr as recovery_capture


class RetainedLoadExportExecutorTests(unittest.TestCase):
    def test_production_shell_handoffs_keep_control_identity_on_stdin(self) -> None:
        tools = Path(__file__).resolve().parents[1] / "tools"
        external = (tools / "platform_production_external_fixture_qa.sh").read_text(
            encoding="utf-8"
        )
        cleanup = (tools / "platform_production_retained_load_cleanup_qa.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("control-email-json-stdin", external)
        self.assertNotIn('--value "$control_email"', external)
        self.assertIn("<target-sha> <concurrency> <run-id>", external)
        external_parse = external.index("control-email-json-stdin")
        external_lock = external.index('PLATFORM_RETAINED_LOAD_LOCK_SUPERVISED:-}')
        self.assertGreater(external_parse, external_lock)

        self.assertIn("control-email-json-stdin", cleanup)
        self.assertNotIn('--value "$control_email"', cleanup)
        self.assertIn("<target-sha> <load-run-id> <cleanup-run-id>", cleanup)
        self.assertIn("--control-email-stdin", cleanup)
        self.assertNotIn('--control-email "$control_email"', cleanup)
        self.assertRegex(cleanup, r'printf .+\\n. \"\$control_email\" \\|')

    def test_recovery_capture_accepts_only_closed_stdin_schema(self) -> None:
        valid = recovery_capture.parse_request(
            b'{"schema":1,"load_run_id":"12345","cleanup_run_id":"67890",'
            b'"control_email":"control@example.invalid","mode":"external-vote"}'
        )
        self.assertEqual(
            valid,
            {
                "load_run_id": "12345",
                "cleanup_run_id": "67890",
                "control_email": "control@example.invalid",
                "mode": "external-vote",
            },
        )
        invalid = (
            b'{"schema":1,"load_run_id":"1","cleanup_run_id":"2",'
            b'"control_email":"control@example.invalid","mode":"external-vote",'
            b'"path":"/tmp/unsafe"}',
            b'{"schema":1,"load_run_id":"1","cleanup_run_id":"2",'
            b'"control_email":"control@example.invalid","mode":"external-vote",'
            b'"load_run_id":"3"}',
            b'{"schema":true,"load_run_id":"1","cleanup_run_id":"2",'
            b'"control_email":"control@example.invalid","mode":"external-vote"}',
            b'{"schema":1,"load_run_id":"1","cleanup_run_id":"2",'
            b'"control_email":"bad\\"@example.invalid","mode":"external-vote"}',
        )
        for document in invalid:
            with self.subTest(document=document):
                with self.assertRaises(recovery_capture.CaptureError):
                    recovery_capture.parse_request(document)

    def test_recovery_stderr_is_bounded_private_and_failure_is_preserved(self) -> None:
        class FakeInput:
            def __init__(self) -> None:
                self.value = b""
                self.closed = False

            def write(self, value: bytes) -> int:
                self.value += value
                return len(value)

            def close(self) -> None:
                self.closed = True

        class FakeProcess:
            def __init__(self, payload: bytes) -> None:
                self.stdin = FakeInput()
                self.stderr = io.BytesIO(payload)

            def wait(self) -> int:
                return 37

            def poll(self) -> int:
                return 37

        payload = b"private-recovery-diagnostic-" * 4000
        with tempfile.TemporaryDirectory(
            prefix="retained-recovery-", dir="/root"
        ) as parent:
            run_root_base = Path(parent) / "production-retained-matrix"
            run_root = run_root_base / "gha-12345"
            run_root.mkdir(parents=True, mode=0o700)
            run_root.chmod(0o700)
            with (
                patch.object(recovery_capture, "RUN_ROOT_BASE", run_root_base),
                patch.object(
                    recovery_capture.subprocess,
                    "Popen",
                    return_value=FakeProcess(payload),
                ) as popen,
            ):
                status = recovery_capture.capture_recovery_stderr(
                    load_run_id="12345",
                    cleanup_run_id="67890",
                    control_email="private@example.invalid",
                    mode="external-vote",
                )

            self.assertEqual(status, 37)
            popen.assert_called_once()
            command = popen.call_args.args[0]
            self.assertEqual(command[0:3], ["/usr/bin/python3.12", "-I", "-B"])
            self.assertTrue(
                any(
                    argument.endswith("platform_recover_retained_report.py")
                    for argument in command
                )
            )
            self.assertIn("--control-email-stdin", command)
            self.assertNotIn("private@example.invalid", command)
            self.assertTrue(popen.call_args.kwargs["stdin"] == subprocess.PIPE)
            self.assertNotIn("private@example.invalid", str(popen.call_args.kwargs["env"]))
            self.assertTrue(popen.return_value.stdin.closed)
            self.assertEqual(
                popen.return_value.stdin.value, b"private@example.invalid\n"
            )
            self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(popen.call_args.kwargs["close_fds"], True)
            self.assertEqual(popen.call_args.kwargs["cwd"], "/")
            self.assertEqual(
                popen.call_args.kwargs["env"], recovery_capture.RECOVERY_ENV
            )

            capture = run_root / "cleanup-recovery-12345-67890.stderr"
            metadata = capture.lstat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual((metadata.st_uid, metadata.st_gid), (0, 0))
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
            self.assertEqual(metadata.st_nlink, 1)
            content = capture.read_bytes()
            self.assertLessEqual(len(content), recovery_capture.MAX_CAPTURE_BYTES)
            self.assertIn(b"private-recovery-diagnostic", content)
            self.assertTrue(content.endswith(recovery_capture.TRUNCATION_MARKER))

    def test_recovery_capture_collision_preserves_existing_private_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="retained-recovery-collision-", dir="/root"
        ) as parent:
            run_root_base = Path(parent) / "production-retained-matrix"
            run_root = run_root_base / "gha-12345"
            run_root.mkdir(parents=True, mode=0o700)
            run_root.chmod(0o700)
            capture = run_root / "cleanup-recovery-12345-67890.stderr"
            capture.write_bytes(b"existing failure evidence\n")
            capture.chmod(0o600)
            before = capture.stat()
            with (
                patch.object(recovery_capture, "RUN_ROOT_BASE", run_root_base),
                patch.object(recovery_capture.subprocess, "Popen") as popen,
            ):
                with self.assertRaises(FileExistsError):
                    recovery_capture.capture_recovery_stderr(
                        load_run_id="12345",
                        cleanup_run_id="67890",
                        control_email="private@example.invalid",
                        mode="external-vote",
                    )
            popen.assert_not_called()
            after = capture.stat()
            self.assertEqual(capture.read_bytes(), b"existing failure evidence\n")
            self.assertEqual(
                (before.st_dev, before.st_ino), (after.st_dev, after.st_ino)
            )
            self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)

    def test_recovery_capture_drains_child_after_early_stdin_close(self) -> None:
        class ClosedInput:
            def write(self, value: bytes) -> int:
                raise BrokenPipeError

            def close(self) -> None:
                raise BrokenPipeError

        class EarlyExitProcess:
            def __init__(self) -> None:
                self.stdin = ClosedInput()
                self.stderr = io.BytesIO(b"bounded child startup failure")

            def wait(self) -> int:
                return 37

            def poll(self) -> int:
                return 37

        with tempfile.TemporaryDirectory(
            prefix="retained-recovery-early-exit-", dir="/root"
        ) as parent:
            run_root_base = Path(parent) / "production-retained-matrix"
            run_root = run_root_base / "gha-12345"
            run_root.mkdir(parents=True, mode=0o700)
            run_root.chmod(0o700)
            with (
                patch.object(recovery_capture, "RUN_ROOT_BASE", run_root_base),
                patch.object(
                    recovery_capture.subprocess,
                    "Popen",
                    return_value=EarlyExitProcess(),
                ) as popen,
            ):
                self.assertEqual(
                    recovery_capture.capture_recovery_stderr(
                        load_run_id="12345",
                        cleanup_run_id="67890",
                        control_email="private@example.invalid",
                        mode="external-vote",
                    ),
                    37,
                )
            self.assertNotIn(
                "private@example.invalid", popen.call_args.args[0]
            )
            self.assertEqual(
                (run_root / "cleanup-recovery-12345-67890.stderr").read_bytes(),
                b"bounded child startup failure",
            )

    def test_cleanup_recovery_failure_emits_only_fixed_stage_marker(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "tools"
            / "platform_production_retained_load_cleanup_qa.sh"
        ).read_text(encoding="utf-8")

        def function_body(name: str) -> str:
            prefix = f"{name}() {{"
            return prefix + script.split(prefix, 1)[1].split("\n}", 1)[0] + "\n}"

        shell_functions = "\n".join(
            function_body(name)
            for name in (
                "cleanup_stage_emit",
                "cleanup_exit_report",
            )
        )
        harness = "\n".join(
            (
                "set -Eeuo pipefail",
                'CLEANUP_STAGE="run_root"',
                "platform_retained_load_lock_close() { :; }",
                shell_functions,
                "run_external_vote_recovery() {",
                '  CLEANUP_STAGE="external_vote_recovery"',
                "  /bin/bash -c "
                + shlex.quote(
                    "printf 'private stdout sentinel\\n'; "
                    "printf 'private stderr sentinel\\n' >&2; exit 37"
                )
                + " >/dev/null 2>&1",
                "}",
                "trap cleanup_exit_report EXIT",
                'if run_external_vote_recovery; then exit 0; else status=$?; exit "$status"; fi',
            )
        )
        completed = subprocess.run(
            ["/bin/bash", "-c", harness],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )
        self.assertEqual(completed.returncode, 37)
        self.assertEqual(
            completed.stdout.decode("ascii"),
            "RETAINED_CLEANUP_STAGE schema=1 stage=external_vote_recovery exit_code=37\n",
        )
        self.assertEqual(completed.stderr, b"")
        self.assertNotIn(b"private", completed.stdout + completed.stderr)
        self.assertNotIn(b"PRODUCTION_RETAINED_LOAD_CLEANUP_OK", completed.stdout)

        recovery_call = script.index("run_external_vote_recovery\n")
        self.assertIn('CLEANUP_STAGE="external_vote_recovery"', script)
        recovery_function = script.split("run_external_vote_recovery() {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn("platform_capture_retained_recovery_stderr.py", recovery_function)
        self.assertIn(">/dev/null 2>&1", recovery_function)
        self.assertNotIn("--control-email", recovery_function)
        self.assertIn('"schema":1,"load_run_id":"$load_run_id"', recovery_function)
        export_creation = script.index(
            '/usr/bin/mkdir -m 0700 -- "$export_dir"', recovery_call
        )
        self.assertLess(recovery_call, export_creation)

    def test_missing_preprovisioned_owner_fails_closed_without_provisioning(
        self,
    ) -> None:
        with patch.object(executor.pwd, "getpwnam", side_effect=KeyError):
            with self.assertRaisesRegex(
                executor.ExportCleanupError, "account_lookup_failed"
            ):
                executor.resolve_artifact_identity(
                    require_root=False, verify_shadow=False
                )

    def test_closed_payload_accepts_only_exact_positive_run_bindings(self) -> None:
        valid = io.BytesIO(
            b'{"schema":1,"load_run_id":"37418874056","cleanup_run_id":"37423702916"}'
        )
        self.assertEqual(
            executor._parse_payload(SimpleNamespace(buffer=valid)),
            {
                "schema": 1,
                "load_run_id": "37418874056",
                "cleanup_run_id": "37423702916",
            },
        )
        invalid_documents = (
            b'{"schema":1,"load_run_id":"1","cleanup_run_id":"2","uid":1000}',
            b'{"schema":1,"load_run_id":"1","cleanup_run_id":"2","cleanup_run_id":"3"}',
            b'{"schema":true,"load_run_id":"1","cleanup_run_id":"2"}',
            b'{"schema":1,"load_run_id":"0","cleanup_run_id":"2"}',
            b'{"schema":1,"load_run_id":"1","cleanup_run_id":"-2"}',
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                with self.assertRaises(executor.ExportCleanupError):
                    executor._parse_payload(
                        SimpleNamespace(buffer=io.BytesIO(document))
                    )
        invalid_load_documents = (
            b'{"schema":1,"load_run_id":"1","cleanup_run_id":"2"}',
            b'{"schema":true,"load_run_id":"1"}',
            b'{"schema":1,"load_run_id":"0"}',
            b'{"schema":1,"load_run_id":"1","path":"/tmp/unsafe"}',
        )
        for document in invalid_load_documents:
            with self.subTest(document=document):
                with self.assertRaises(executor.ExportCleanupError):
                    executor._parse_load_payload(
                        SimpleNamespace(buffer=io.BytesIO(document))
                    )

    def test_inventory_planning_rejects_unlisted_or_insecure_entries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="load-export-plan-") as parent:
            root = Path(parent) / "export"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            artifact = root / "canonical.log"
            artifact.write_text("fixed test evidence\n", encoding="ascii")
            os.chmod(artifact, 0o600)
            identity = executor.ArtifactIdentity(os.getuid(), os.getgid())
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                parent_metadata = os.fstat(parent_fd)
                plan = executor._plan_root(
                    parent_fd,
                    parent_metadata,
                    "export",
                    executor.CLEANUP_EXPORT_NAMES,
                    identity,
                )
                self.assertIsNotNone(plan)
                assert plan is not None
                os.close(plan.descriptor)

                unexpected = root / "unlisted"
                unexpected.write_text("must reject\n", encoding="ascii")
                os.chmod(unexpected, 0o600)
                with self.assertRaisesRegex(executor.ExportCleanupError, "inventory"):
                    executor._plan_root(
                        parent_fd,
                        parent_metadata,
                        "export",
                        executor.CLEANUP_EXPORT_NAMES,
                        identity,
                    )
                unexpected.unlink()

                os.chmod(artifact, 0o644)
                with self.assertRaisesRegex(executor.ExportCleanupError, "metadata"):
                    executor._plan_root(
                        parent_fd,
                        parent_metadata,
                        "export",
                        executor.CLEANUP_EXPORT_NAMES,
                        identity,
                    )
            finally:
                os.close(parent_fd)

    def test_cleanup_script_preserves_legacy_removal_and_defers_artifact_owner(
        self,
    ) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "tools"
            / "platform_production_retained_load_cleanup_qa.sh"
        ).read_text(encoding="utf-8")
        removal = script.split("remove_external_load_export() {", 1)[1].split("\n}", 1)[
            0
        ]
        self.assertIn(
            '-user "$legacy_export_uid" -o -user "$export_uid" -o -user 0', removal
        )
        defer = removal.index(
            '"$(stat -c \'%u\' -- "$external_load_export_dir")" == "$export_uid"'
        )
        self.assertLess(defer, removal.index('rm -rf -- "$external_load_export_dir"'))
        self.assertGreaterEqual(script.count("remove_external_load_export"), 3)
        self.assertNotIn('chown -R "$export_uid:$export_gid"', script)

    def test_fixture_exit_trap_only_writes_to_its_created_export_inode(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "tools"
            / "platform_production_external_fixture_qa.sh"
        ).read_text(encoding="utf-8")
        trap = script.split("write_supervisor_exit() {", 1)[1].split("\n}", 1)[0]
        self.assertIn('export_created" == "1', trap)
        self.assertIn("%d:%i:%u:%g:%a:%h", trap)
        self.assertLess(
            script.index('export_dir_identity="$(/usr/bin/stat'),
            script.index(
                "export_created=1",
                script.index('/usr/bin/mkdir -m 0700 -- "$export_dir"'),
            ),
        )

    @unittest.skipUnless(os.geteuid() == 0, "requires isolated root-owned trap fixture")
    def test_fixture_trap_collision_leaves_preexisting_export_unchanged(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "tools"
            / "platform_production_external_fixture_qa.sh"
        ).read_text(encoding="utf-8")
        function = (
            "write_supervisor_exit() {"
            + script.split("write_supervisor_exit() {", 1)[1].split("\n}", 1)[0]
            + "\n}"
        )
        with tempfile.TemporaryDirectory(prefix="load-export-trap-") as parent:
            export_dir = Path(parent) / "existing"
            export_dir.mkdir(mode=0o700)
            receipt = export_dir / "supervisor.exit"
            receipt.write_bytes(b"prior receipt\n")
            receipt.chmod(0o600)
            before = receipt.stat()
            command = "\n".join(
                (
                    "set -euo pipefail",
                    f"export_dir={shlex.quote(str(export_dir))}",
                    f"supervisor_exit_path={shlex.quote(str(receipt))}",
                    "export_created=0",
                    "export_dir_identity=''",
                    "export_uid=0",
                    "export_gid=0",
                    "platform_retained_load_lock_close() { :; }",
                    function,
                    "write_supervisor_exit",
                )
            )
            completed = subprocess.run(
                ["/bin/bash", "-c", command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=5,
            )
            self.assertEqual(
                completed.returncode, 0, completed.stderr.decode("utf-8", "replace")
            )
            after = receipt.stat()
            self.assertEqual(receipt.read_bytes(), b"prior receipt\n")
            self.assertEqual(
                (
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                    before.st_size,
                    before.st_mtime_ns,
                ),
                (
                    after.st_dev,
                    after.st_ino,
                    after.st_mode,
                    after.st_size,
                    after.st_mtime_ns,
                ),
            )

    @unittest.skipUnless(
        os.geteuid() == 0 and shutil.which("setpriv"),
        "requires root setpriv and an isolated sticky directory",
    )
    def test_exact_removal_validates_both_roots_before_unlink_and_is_idempotent(
        self,
    ) -> None:
        try:
            user = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("no unprivileged test identity is installed")
        with tempfile.TemporaryDirectory(prefix="load-export-remove-") as parent:
            tmp_root = Path(parent) / "tmp"
            tmp_root.mkdir(mode=0o700)
            os.chmod(tmp_root, 0o1777)
            os.chown(tmp_root, 0, 0)
            os.chmod(parent, 0o711)
            load_name = f"{executor.LOAD_PREFIX}123456789"
            cleanup_name = f"{executor.CLEANUP_PREFIX}987654321"

            def make_root(name: str, filename: str) -> Path:
                root = tmp_root / name
                root.mkdir(mode=0o700)
                os.chmod(root, 0o700)
                artifact = root / filename
                artifact.write_text("private fixture\n", encoding="ascii")
                os.chmod(artifact, 0o600)
                os.chown(artifact, user.pw_uid, user.pw_gid)
                os.chown(root, user.pw_uid, user.pw_gid)
                return root

            load_root = make_root(load_name, "canonical.log")
            cleanup_root = make_root(cleanup_name, "cleanup-summary.json")
            unexpected = cleanup_root / "not-allowlisted"
            unexpected.write_text("must block all mutation\n", encoding="ascii")
            os.chmod(unexpected, 0o600)
            os.chown(unexpected, user.pw_uid, user.pw_gid)
            module_path = Path(executor.__file__).resolve()
            program = "\n".join(
                (
                    "import importlib.util",
                    "from pathlib import Path",
                    "import sys",
                    "spec = importlib.util.spec_from_file_location('export_executor', "
                    + repr(str(module_path))
                    + ")",
                    "module = importlib.util.module_from_spec(spec)",
                    "sys.modules[spec.name] = module",
                    "spec.loader.exec_module(module)",
                    f"module.TMP_ROOT = Path({str(tmp_root)!r})",
                    f"identity = module.ArtifactIdentity({user.pw_uid}, {user.pw_gid})",
                    "payload = {'load_run_id': '123456789', 'cleanup_run_id': '987654321'}",
                    "try:",
                    "    module.remove_exact_exports(payload, identity)",
                    "except module.ExportCleanupError as exc:",
                    "    if exc.error_class != 'export_root_inventory_invalid': raise",
                    "else:",
                    "    raise SystemExit('invalid second inventory unexpectedly passed')",
                    f"if not Path({str(load_root)!r}).exists() or not Path({str(cleanup_root)!r}).exists():",
                    "    raise SystemExit('first root was mutated before second inventory validation')",
                    f"Path({str(unexpected)!r}).unlink()",
                    "if module.remove_exact_exports(payload, identity) != 2:",
                    "    raise SystemExit('both exact roots were not removed')",
                    f"if Path({str(load_root)!r}).exists() or Path({str(cleanup_root)!r}).exists():",
                    "    raise SystemExit('an exact root remains after removal')",
                    "if module.remove_exact_exports(payload, identity) != 0:",
                    "    raise SystemExit('repeat exact removal was not idempotent')",
                    "print('EXACT_REMOVE_PASS')",
                )
            )
            command = [
                "/usr/bin/setpriv",
                f"--reuid={user.pw_uid}",
                f"--regid={user.pw_gid}",
                "--clear-groups",
                "--no-new-privs",
                "--bounding-set=-all",
                "--inh-caps=-all",
                "--ambient-caps=-all",
                "/usr/bin/python3.12",
                "-I",
                "-B",
                "-c",
                program,
            ]
            completed = subprocess.run(
                command,
                cwd="/",
                env=executor.FIXED_ENVIRONMENT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                close_fds=True,
                timeout=10,
            )
            self.assertEqual(
                completed.returncode, 0, completed.stderr.decode("utf-8", "replace")
            )
            self.assertEqual(completed.stdout, b"EXACT_REMOVE_PASS\n")

    @unittest.skipUnless(
        os.geteuid() == 0 and shutil.which("setpriv"), "requires root setpriv"
    )
    def test_helper_import_and_actual_privilege_drop_contract(self) -> None:
        try:
            user = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("no unprivileged test identity is installed")
        module_path = Path(executor.__file__).resolve()
        program = "\n".join(
            (
                "import importlib.util",
                "from pathlib import Path",
                "import sys",
                "spec = importlib.util.spec_from_file_location('export_executor', "
                + repr(str(module_path))
                + ")",
                "module = importlib.util.module_from_spec(spec)",
                "sys.modules[spec.name] = module",
                "spec.loader.exec_module(module)",
                f"module._verify_dropped_identity(module.ArtifactIdentity({user.pw_uid}, {user.pw_gid}), {{0, 1, 2}})",
                "print('DROP_CONTRACT_PASS')",
            )
        )
        command = [
            "/usr/bin/setpriv",
            f"--reuid={user.pw_uid}",
            f"--regid={user.pw_gid}",
            "--clear-groups",
            "--no-new-privs",
            "--bounding-set=-all",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "/usr/bin/python3.12",
            "-I",
            "-B",
            "-c",
            program,
        ]
        completed = subprocess.run(
            command,
            cwd="/",
            env=executor.FIXED_ENVIRONMENT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            close_fds=True,
            timeout=10,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8", "replace")
        )
        self.assertEqual(completed.stdout, b"DROP_CONTRACT_PASS\n")

    @unittest.skipUnless(
        os.geteuid() == 0 and shutil.which("setpriv"),
        "requires root setpriv and an isolated sticky directory",
    )
    def test_touch_complete_creates_exact_marker_and_is_idempotent(self) -> None:
        try:
            user = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("no unprivileged test identity is installed")
        with tempfile.TemporaryDirectory(prefix="load-export-complete-") as parent:
            tmp_root = Path(parent) / "tmp"
            tmp_root.mkdir(mode=0o700)
            os.chmod(tmp_root, 0o1777)
            os.chown(tmp_root, 0, 0)
            os.chmod(parent, 0o711)
            root = tmp_root / f"{executor.LOAD_PREFIX}123456789"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            os.chown(root, user.pw_uid, user.pw_gid)
            module_path = Path(executor.__file__).resolve()
            program = "\n".join(
                (
                    "import importlib.util",
                    "from pathlib import Path",
                    "import sys",
                    "spec = importlib.util.spec_from_file_location('export_executor', "
                    + repr(str(module_path))
                    + ")",
                    "module = importlib.util.module_from_spec(spec)",
                    "sys.modules[spec.name] = module",
                    "spec.loader.exec_module(module)",
                    f"module.TMP_ROOT = Path({str(tmp_root)!r})",
                    f"identity = module.ArtifactIdentity({user.pw_uid}, {user.pw_gid})",
                    "payload = {'load_run_id': '123456789'}",
                    "if module.touch_exact_complete(payload, identity) != 'created':",
                    "    raise SystemExit('first marker creation was not reported')",
                    "if module.touch_exact_complete(payload, identity) != 'existing':",
                    "    raise SystemExit('repeat marker creation was not idempotent')",
                    "marker = Path(" + repr(str(root / "complete")) + ")",
                    "metadata = marker.stat(follow_symlinks=False)",
                    "if marker.is_symlink() or marker.read_bytes() != b'':",
                    "    raise SystemExit('marker is not an empty regular file')",
                    "if (metadata.st_uid, metadata.st_gid, metadata.st_nlink, metadata.st_mode & 0o777) != ("
                    + f"{user.pw_uid}, {user.pw_gid}, 1, 0o600):",
                    "    raise SystemExit('marker metadata is outside the exact contract')",
                    "print('TOUCH_COMPLETE_PASS')",
                )
            )
            command = [
                "/usr/bin/setpriv",
                f"--reuid={user.pw_uid}",
                f"--regid={user.pw_gid}",
                "--clear-groups",
                "--no-new-privs",
                "--bounding-set=-all",
                "--inh-caps=-all",
                "--ambient-caps=-all",
                "/usr/bin/python3.12",
                "-I",
                "-B",
                "-c",
                program,
            ]
            completed = subprocess.run(
                command,
                cwd="/",
                env=executor.FIXED_ENVIRONMENT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                close_fds=True,
                timeout=10,
            )
            self.assertEqual(
                completed.returncode, 0, completed.stderr.decode("utf-8", "replace")
            )
            self.assertEqual(completed.stdout, b"TOUCH_COMPLETE_PASS\n")

    @unittest.skipUnless(
        os.geteuid() == 0 and shutil.which("setpriv"),
        "requires root setpriv and an isolated sticky directory",
    )
    def test_touch_complete_rejects_marker_collision_and_symlinked_root(self) -> None:
        try:
            user = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("no unprivileged test identity is installed")
        with tempfile.TemporaryDirectory(
            prefix="load-export-complete-collision-"
        ) as parent:
            tmp_root = Path(parent) / "tmp"
            tmp_root.mkdir(mode=0o700)
            os.chmod(tmp_root, 0o1777)
            os.chown(tmp_root, 0, 0)
            os.chmod(parent, 0o711)
            root = tmp_root / f"{executor.LOAD_PREFIX}123456789"
            root.mkdir(mode=0o700)
            os.chown(root, user.pw_uid, user.pw_gid)
            collision = root / "complete"
            collision.write_bytes(b"existing marker evidence\n")
            collision.chmod(0o644)
            os.chown(collision, user.pw_uid, user.pw_gid)
            before = collision.stat(follow_symlinks=False)
            target = tmp_root / "target"
            target.mkdir(mode=0o700)
            os.chown(target, user.pw_uid, user.pw_gid)
            symlink_root = tmp_root / f"{executor.LOAD_PREFIX}987654321"
            symlink_root.symlink_to(target, target_is_directory=True)
            module_path = Path(executor.__file__).resolve()
            program = "\n".join(
                (
                    "import importlib.util",
                    "from pathlib import Path",
                    "import sys",
                    "spec = importlib.util.spec_from_file_location('export_executor', "
                    + repr(str(module_path))
                    + ")",
                    "module = importlib.util.module_from_spec(spec)",
                    "sys.modules[spec.name] = module",
                    "spec.loader.exec_module(module)",
                    f"module.TMP_ROOT = Path({str(tmp_root)!r})",
                    f"identity = module.ArtifactIdentity({user.pw_uid}, {user.pw_gid})",
                    "try:",
                    "    module.touch_exact_complete({'load_run_id': '123456789'}, identity)",
                    "except module.ExportCleanupError as exc:",
                    "    if exc.error_class != 'entry_metadata_invalid': raise",
                    "else:",
                    "    raise SystemExit('unsafe existing marker unexpectedly accepted')",
                    "try:",
                    "    module.touch_exact_complete({'load_run_id': '987654321'}, identity)",
                    "except module.ExportCleanupError as exc:",
                    "    if exc.error_class != 'export_root_open_failed': raise",
                    "else:",
                    "    raise SystemExit('symlinked exact root unexpectedly accepted')",
                    "if Path(" + repr(str(target / "complete")) + ").exists():",
                    "    raise SystemExit('symlink target was mutated')",
                    "print('TOUCH_COLLISION_PASS')",
                )
            )
            command = [
                "/usr/bin/setpriv",
                f"--reuid={user.pw_uid}",
                f"--regid={user.pw_gid}",
                "--clear-groups",
                "--no-new-privs",
                "--bounding-set=-all",
                "--inh-caps=-all",
                "--ambient-caps=-all",
                "/usr/bin/python3.12",
                "-I",
                "-B",
                "-c",
                program,
            ]
            completed = subprocess.run(
                command,
                cwd="/",
                env=executor.FIXED_ENVIRONMENT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                close_fds=True,
                timeout=10,
            )
            self.assertEqual(
                completed.returncode, 0, completed.stderr.decode("utf-8", "replace")
            )
            self.assertEqual(completed.stdout, b"TOUCH_COLLISION_PASS\n")
            after = collision.stat(follow_symlinks=False)
            self.assertEqual(collision.read_bytes(), b"existing marker evidence\n")
            self.assertEqual(
                (before.st_dev, before.st_ino, before.st_mode, before.st_mtime_ns),
                (after.st_dev, after.st_ino, after.st_mode, after.st_mtime_ns),
            )


if __name__ == "__main__":
    unittest.main()
