from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
