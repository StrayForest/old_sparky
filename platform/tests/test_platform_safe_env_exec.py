from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "platform_safe_env_exec.py"
)
SPEC = importlib.util.spec_from_file_location(
    "platform_safe_env_exec_tested", SCRIPT_PATH
)
assert SPEC is not None and SPEC.loader is not None
safe_env = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(safe_env)


class SafeEnvExecTests(unittest.TestCase):
    def test_production_path_uses_the_active_platform_owner_identity(self) -> None:
        with mock.patch.object(
            safe_env.pwd,
            "getpwnam",
            return_value=mock.Mock(pw_uid=996),
        ) as getpwnam:
            self.assertEqual(
                safe_env._production_component_owners(),
                (0, 0, 996, 0, 0),
            )
        getpwnam.assert_called_once_with("oldsparky-platform")

    def test_dotenv_is_parsed_as_data_without_shell_expansion(self) -> None:
        values = safe_env.parse_dotenv(
            b"PLATFORM_ENVIRONMENT=production\n"
            b"PLATFORM_WEB_ORIGIN=https://old-sparky.com\n"
            b"PLATFORM_SECRET_KEY='$(touch /tmp/must-not-run)'\n"
            b"PLATFORM_EMAIL_SENDER_EMAIL='Old Sparky <noreply@example.invalid>'\n"
        )
        self.assertEqual(values["PLATFORM_SECRET_KEY"], "$(touch /tmp/must-not-run)")
        self.assertEqual(
            values["PLATFORM_EMAIL_SENDER_EMAIL"],
            "Old Sparky <noreply@example.invalid>",
        )

    def test_dotenv_rejects_export_duplicate_and_foreign_keys(self) -> None:
        invalid_payloads = (
            b"export PLATFORM_ENVIRONMENT=production\n",
            b"PLATFORM_ENVIRONMENT=production\nPLATFORM_ENVIRONMENT=test\n",
            b"LD_PRELOAD=/tmp/attack.so\n",
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(safe_env.SafeEnvError):
                    safe_env.parse_dotenv(payload)

    def test_dotenv_rejects_noncanonical_line_separators(self) -> None:
        for payload in (
            b"PLATFORM_ENVIRONMENT=production\rPLATFORM_WEB_ORIGIN=x\n",
            "PLATFORM_ENVIRONMENT=production\u2028PLATFORM_WEB_ORIGIN=x".encode(),
            b"PLATFORM_ENVIRONMENT=production\x00ignored\n",
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(safe_env.SafeEnvError, "unsafe|carriage"):
                    safe_env.parse_dotenv(payload)

    def test_clean_environment_drops_every_inherited_variable(self) -> None:
        values = {
            "PLATFORM_ENVIRONMENT": "production",
            "PLATFORM_DATABASE_URL": "postgresql://required-secret",
        }
        with mock.patch.dict(
            os.environ,
            {"LD_PRELOAD": "/tmp/attack.so", "PYTHONPATH": "/tmp/attack"},
            clear=True,
        ):
            child = safe_env.clean_child_environment(
                values,
                pythonpath=safe_env.TRUSTED_PLATFORM_ROOT,
            )
        self.assertNotIn("LD_PRELOAD", child)
        self.assertEqual(child["PYTHONPATH"], str(safe_env.TRUSTED_PLATFORM_ROOT))
        self.assertEqual(child["HOME"], "/nonexistent")
        self.assertEqual(
            child["PLATFORM_DATABASE_URL"], values["PLATFORM_DATABASE_URL"]
        )

    def test_command_validation_rejects_opt_runtime(self) -> None:
        with self.assertRaisesRegex(safe_env.SafeEnvError, "fixed root-controlled"):
            safe_env.validate_trusted_command(
                [
                    "/opt/oldsparky/platform/shared/venv/bin/python",
                    "/opt/oldsparky/platform/current/tools/platform_cleanup_live_user_qa.py",
                ],
                pythonpath=safe_env.TRUSTED_PLATFORM_ROOT,
            )

    @unittest.skipUnless(os.geteuid() == 0, "root-owned dotenv contract")
    def test_openat_reader_reads_an_exact_root_owned_file(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            private = root / "private"
            private.mkdir(mode=0o700)
            env_file = private / "environment"
            payload = b"PLATFORM_ENVIRONMENT=production\n"
            env_file.write_bytes(payload)
            os.chmod(env_file, 0o600)
            owners = tuple(
                path.stat().st_uid for path in (Path("/"), Path("/root"), root, private)
            )
            self.assertEqual(
                safe_env._read_env_bytes_at(env_file, owners=owners),
                payload,
            )

    @unittest.skipUnless(os.geteuid() == 0, "root-owned dotenv contract")
    def test_openat_reader_rejects_a_symlink_component(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            actual = root / "actual"
            actual.mkdir(mode=0o700)
            env_file = actual / "environment"
            env_file.write_text("PLATFORM_ENVIRONMENT=production\n", encoding="utf-8")
            os.chmod(env_file, 0o600)
            linked = root / "linked"
            linked.symlink_to(actual, target_is_directory=True)
            unsafe_path = linked / "environment"
            owners = (0, 0, 0, 0)
            with self.assertRaisesRegex(safe_env.SafeEnvError, "unavailable or unsafe"):
                safe_env._read_env_bytes_at(unsafe_path, owners=owners)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned dotenv contract")
    def test_openat_reader_rejects_a_hardlinked_env_file(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            private = root / "private"
            private.mkdir(mode=0o700)
            env_file = private / "environment"
            env_file.write_text("PLATFORM_ENVIRONMENT=production\n", encoding="utf-8")
            os.chmod(env_file, 0o600)
            os.link(env_file, private / "second-link")
            owners = tuple(
                path.stat().st_uid for path in (Path("/"), Path("/root"), root, private)
            )
            with self.assertRaisesRegex(safe_env.SafeEnvError, "metadata is unsafe"):
                safe_env._read_env_bytes_at(env_file, owners=owners)

    def test_safe_env_main_refuses_nonroot_before_reading(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(os, "geteuid", return_value=1000),
            mock.patch.object(safe_env, "read_production_env_bytes") as read_env,
            mock.patch("sys.stderr", stderr),
        ):
            result = safe_env.main(["print-public-value", "PLATFORM_WEB_ORIGIN"])
        self.assertEqual(result, 2)
        read_env.assert_not_called()
        self.assertIn("requires root", stderr.getvalue())

    def test_private_file_contract_is_exact_0600_root_owned(self) -> None:
        metadata = mock.Mock(
            st_mode=stat.S_IFREG | 0o640,
            st_nlink=1,
            st_uid=0,
            st_gid=0,
            st_size=10,
        )
        with (
            mock.patch.object(
                safe_env.grp, "getgrnam", return_value=mock.Mock(gr_gid=988)
            ),
            self.assertRaisesRegex(safe_env.SafeEnvError, "metadata is unsafe"),
        ):
            safe_env._validate_env_file(metadata)


if __name__ == "__main__":
    unittest.main()
