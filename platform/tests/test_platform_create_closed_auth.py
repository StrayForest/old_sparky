from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "platform_create_closed_auth.py"
SPEC = importlib.util.spec_from_file_location("platform_create_closed_auth", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PlatformCreateClosedAuthTests(unittest.TestCase):
    def test_username_is_bounded_ascii(self) -> None:
        self.assertEqual(MODULE.validate_username("closed-launch"), "closed-launch")
        for value in ("ab", "space name", "юзер", "x" * 33):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MODULE.validate_username(value)

    def test_apache_hash_passes_secret_only_on_stdin(self) -> None:
        with patch.object(MODULE.subprocess, "run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = "$apr1$salt$digest\n"
            digest = MODULE.apache_md5_hash("secret-value")

        self.assertEqual(digest, "$apr1$salt$digest")
        command = run_mock.call_args.args[0]
        self.assertNotIn("secret-value", command)
        self.assertEqual(run_mock.call_args.kwargs["input"], "secret-value\n")

    def test_atomic_write_sets_requested_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            destination = Path(temporary_dir) / "secret.txt"
            MODULE.atomic_write(
                destination,
                "secret\n",
                mode=0o600,
                uid=os.getuid(),
                gid=os.getgid(),
            )

            mode = stat.S_IMODE(destination.stat().st_mode)

        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
