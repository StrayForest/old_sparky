from __future__ import annotations

import io
import os
from pathlib import Path
import pwd
import shlex
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

from tools import platform_retained_load_export_executor as executor


class RetainedLoadExportExecutorTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
