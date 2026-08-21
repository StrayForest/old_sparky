from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "platform_update_cloudflare_ips.py"
SPEC = importlib.util.spec_from_file_location("platform_update_cloudflare_ips", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CloudflareIpUpdaterTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
