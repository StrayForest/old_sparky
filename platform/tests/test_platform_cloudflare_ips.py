from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "platform_update_cloudflare_ips.py"
SPEC = importlib.util.spec_from_file_location("platform_update_cloudflare_ips", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CloudflareIpUpdaterTests(unittest.TestCase):
    @staticmethod
    def _write_nginx_validation_fixture(directory: Path) -> Path:
        fixture = directory / "nginx-fixture"
        fixture.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "if [ \"$#\" -ne 1 ] || [ \"$1\" != \"-t\" ]; then\n"
            "    exit 64\n"
            "fi\n"
            "printf '%s\\n' \"$@\" > \"$0.argv\"\n",
            encoding="ascii",
        )
        fixture.chmod(0o700)
        return fixture

    def test_operation_budget_matches_service_timeout_margin(self) -> None:
        self.assertEqual(MODULE.FETCH_TIMEOUT_MAX_SECONDS, 30.0)
        self.assertEqual(MODULE.SUBPROCESS_TIMEOUT_SECONDS, 30.0)
        self.assertEqual(MODULE.MAX_SUBPROCESS_CALLS, 4)
        self.assertEqual(MODULE.OPERATION_BUDGET_SECONDS, 180.0)
        self.assertEqual(MODULE.SERVICE_TIMEOUT_SECONDS, 210.0)

    def test_parse_and_render_validated_ranges(self) -> None:
        ipv4 = MODULE.parse_ranges("\n".join(f"192.0.2.{index}/32" for index in range(10)), 4)
        ipv6 = MODULE.parse_ranges("\n".join(f"2001:db8::{index}/128" for index in range(5)), 6)

        rendered = MODULE.render_config(ipv4, ipv6)

        self.assertIn("set_real_ip_from 192.0.2.0/32;", rendered)
        self.assertIn("set_real_ip_from 2001:db8::/128;", rendered)
        self.assertEqual(rendered.count("set_real_ip_from"), 15)

    def test_rejects_empty_or_wrong_family_sources(self) -> None:
        with self.assertRaises(ValueError):
            MODULE.parse_ranges("", 4)
        with self.assertRaises(ValueError):
            MODULE.parse_ranges("\n".join("2001:db8::/128" for _ in range(10)), 4)

    def test_changed_candidate_is_installed_and_unchanged_candidate_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            nginx_fixture = self._write_nginx_validation_fixture(Path(temporary))
            output = Path(temporary) / "cloudflare-real-ip.conf"
            output.write_text("old\n", encoding="ascii")
            self.assertTrue(MODULE.install_candidate(output, "new\n", str(nginx_fixture), False))
            self.assertEqual(output.read_text(encoding="ascii"), "new\n")
            self.assertEqual(nginx_fixture.with_name(f"{nginx_fixture.name}.argv").read_text(), "-t\n")
            with mock.patch.object(MODULE, "run_checked") as run_checked:
                self.assertFalse(MODULE.install_candidate(output, "new\n", str(nginx_fixture), False))
                run_checked.assert_not_called()

    def test_validation_timeout_rolls_back_exact_previous_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "cloudflare-real-ip.conf"
            output.write_text("previous\n", encoding="ascii")
            with mock.patch.object(
                MODULE,
                "run_checked",
                side_effect=RuntimeError("bounded command timed out after 30s: nginx"),
            ):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    MODULE.install_candidate(output, "candidate\n", "nginx", False)
            self.assertEqual(output.read_text(encoding="ascii"), "previous\n")

    def test_reload_timeout_rolls_back_exact_previous_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "cloudflare-real-ip.conf"
            output.write_text("previous\n", encoding="ascii")
            calls: list[list[str]] = []

            def run_checked(command: list[str], **_: object) -> None:
                calls.append(command)
                if len(calls) == 2:
                    raise RuntimeError("bounded command timed out after 30s: /usr/bin/systemctl")

            with mock.patch.object(MODULE, "run_checked", side_effect=run_checked):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    MODULE.install_candidate(output, "candidate\n", "nginx", True)
            self.assertEqual(output.read_text(encoding="ascii"), "previous\n")
            self.assertEqual(len(calls), 4)

    def test_subprocess_timeout_is_converted_to_fail_closed_error(self) -> None:
        command = ["/usr/sbin/nginx", "-t"]
        with mock.patch.object(
            MODULE.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(command, MODULE.SUBPROCESS_TIMEOUT_SECONDS),
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "bounded command timed out after 30s"):
                MODULE.run_checked(command)
        self.assertEqual(run.call_args.kwargs["timeout"], MODULE.SUBPROCESS_TIMEOUT_SECONDS)

    def test_rollback_failure_is_reported_without_claiming_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "cloudflare-real-ip.conf"
            output.write_text("previous\n", encoding="ascii")
            calls = 0

            def run_checked(_command: list[str], **_: object) -> None:
                nonlocal calls
                calls += 1
                raise RuntimeError("validation failed")

            with mock.patch.object(MODULE, "run_checked", side_effect=run_checked):
                with self.assertRaisesRegex(RuntimeError, "rollback also failed"):
                    MODULE.install_candidate(output, "candidate\n", "nginx", True)
            self.assertEqual(output.read_text(encoding="ascii"), "previous\n")
            self.assertEqual(calls, 3)


if __name__ == "__main__":
    unittest.main()
