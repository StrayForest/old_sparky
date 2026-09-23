from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest
from unittest import mock

from tools import platform_validate_edge_policy as edge


class PlatformValidateEdgePolicyTests(unittest.TestCase):
    def test_nginx_ranges_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cloudflare-real-ip.conf"
            path.write_text(
                "# managed\nset_real_ip_from 1.1.1.0/24;\n"
                "set_real_ip_from 2001:db8::/32;\n",
                encoding="ascii",
            )
            self.assertEqual(
                edge.nginx_ranges(path),
                {"1.1.1.0/24", "2001:db8::/32"},
            )

    def test_ufw_ranges_require_exact_managed_http_and_https_rules(self) -> None:
        desired = {"1.1.1.0/24"}
        status = "\n".join(
            (
                "[ 1] 80/tcp ALLOW IN 1.1.1.0/24 # oldsparky-cloudflare-origin",
                "[ 2] 443/tcp ALLOW IN 1.1.1.0/24 # oldsparky-cloudflare-origin",
            )
        )
        self.assertEqual(edge.ufw_ranges(status, desired), desired)

    def test_ufw_rejects_broad_web_rule(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "broad public"):
            edge.ufw_ranges(
                "80/tcp ALLOW IN Anywhere # oldsparky-cloudflare-origin\n",
                {"1.1.1.0/24"},
            )

    def test_ufw_rejects_missing_managed_port(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "443/tcp"):
            edge.ufw_ranges(
                "80/tcp ALLOW IN 1.1.1.0/24 # oldsparky-cloudflare-origin\n",
                {"1.1.1.0/24"},
            )

    def test_ufw_rejects_unmanaged_narrow_http_rule(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unmanaged"):
            edge.ufw_ranges(
                "80/tcp ALLOW IN 203.0.113.0/24\n"
                "443/tcp ALLOW IN 1.1.1.0/24 # oldsparky-cloudflare-origin\n",
                {"1.1.1.0/24"},
            )

    def test_ufw_supports_ipv6_and_rejects_duplicate_effective_rules(self) -> None:
        desired = {"2001:db8::/32"}
        status = "\n".join(
            (
                "80/tcp ALLOW IN 2001:db8::/32 # oldsparky-cloudflare-origin",
                "443/tcp ALLOW IN 2001:db8::/32 # oldsparky-cloudflare-origin",
            )
        )
        self.assertEqual(edge.ufw_ranges(status, desired), desired)
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            edge.ufw_ranges(
                status + "\n443/tcp ALLOW IN 2001:db8::/32 # oldsparky-cloudflare-origin",
                desired,
            )

    def test_ufw_application_profile_is_checked_as_http_rule(self) -> None:
        desired = {"1.1.1.0/24"}
        status = "Nginx Full ALLOW IN 1.1.1.0/24 # oldsparky-cloudflare-origin\n"
        self.assertEqual(edge.ufw_ranges(status, desired), desired)
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            edge.ufw_ranges(status + status, desired)

    def test_ufw_baseline_requires_active_default_deny(self) -> None:
        edge.validate_ufw_baseline(
            "Status: active\nDefault: deny (incoming), allow (outgoing), disabled (routed)\n"
        )
        with self.assertRaisesRegex(RuntimeError, "must be active"):
            edge.validate_ufw_baseline("Status: inactive\n")
        with self.assertRaisesRegex(RuntimeError, "default to deny"):
            edge.validate_ufw_baseline("Status: active\nDefault: allow (incoming)\n")

    def test_policy_errors_never_echo_rule_or_network_input(self) -> None:
        private_key_marker = "".join(
            ("-----BEGIN ", "OPENSSH ", "PRIVATE ", "KEY-----")
        )
        adversarial = (
            "80/tcp ALLOW IN 203.0.113.7/32 # email=alice@example.com "
            "2001:db8::42/128 Authorization: Bearer secret-token "
            "Cookie=session=secret-session https://private.invalid/invite?token=secret "
            "SELECT email FROM users WHERE password='secret-password' "
            "/home/root/private-report.json "
            + private_key_marker
            + " "
            "ssh -i /root/.ssh/id_ed25519 operator@example.com"
        )
        with self.assertRaises(RuntimeError) as raised:
            edge.ufw_ranges(adversarial, {"1.1.1.0/24"})
        message = str(raised.exception)
        self.assertNotIn("203.0.113.7", message)
        self.assertNotIn("2001:db8::42", message)
        self.assertNotIn("alice@example.com", message)
        self.assertNotIn("token=secret", message)
        self.assertNotIn("secret-token", message)
        self.assertNotIn("secret-session", message)
        self.assertNotIn("secret-password", message)
        self.assertNotIn("/home/root", message)
        self.assertNotIn("OPENSSH PRIVATE KEY", message)
        self.assertNotIn("operator@example.com", message)

    def test_cli_failure_is_fixed_schema_and_sanitized(self) -> None:
        with mock.patch.object(
            edge,
            "desired_ranges",
            side_effect=RuntimeError(
                "https://private.invalid/?token=secret from /home/root"
            ),
        ), mock.patch.object(
            edge.sys,
            "argv",
            ["platform_validate_edge_policy.py", "--json"],
        ), mock.patch("sys.stdout") as stdout, mock.patch("sys.stderr") as stderr:
            self.assertEqual(edge.main(), 1)

        stdout_text = "".join(call.args[0] for call in stdout.write.call_args_list)
        public = json.loads(stdout_text)
        self.assertEqual(
            set(public),
            {
                "schema",
                "ok",
                "status",
                "error_class",
                "cloudflare_ranges",
                "nginx_ranges",
                "ufw_ranges",
                "read_only",
            },
        )
        self.assertEqual(public["error_class"], "internal")
        stderr_text = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertNotIn("private.invalid", stderr_text)
        self.assertNotIn("token=secret", stderr_text)
        self.assertNotIn("/home/root", stderr_text)

    def test_cli_argument_failure_does_not_echo_untrusted_value(self) -> None:
        with mock.patch.object(
            edge.sys,
            "argv",
            [
                "platform_validate_edge_policy.py",
                "--timeout",
                "https://private.invalid/?token=secret",
                "--json",
            ],
        ), mock.patch("sys.stdout") as stdout, mock.patch("sys.stderr") as stderr:
            self.assertEqual(edge.main(), 1)

        stdout_text = "".join(call.args[0] for call in stdout.write.call_args_list)
        public = json.loads(stdout_text)
        self.assertEqual(public["error_class"], "argument")
        stderr_text = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertNotIn("private.invalid", stderr_text)
        self.assertNotIn("token=secret", stderr_text)


if __name__ == "__main__":
    unittest.main()
