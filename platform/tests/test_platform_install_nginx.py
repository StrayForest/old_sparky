from __future__ import annotations

import importlib.util
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "platform_install_nginx.py"
SPEC = importlib.util.spec_from_file_location("platform_install_nginx", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PlatformInstallNginxTests(unittest.TestCase):
    def _validate_vhost_text(self, vhost_text: str) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            candidate = Path(temporary_dir) / "candidate.conf"
            candidate.write_text(vhost_text, encoding="utf-8")
            MODULE.validate_security_header_contract(
                candidate,
                MODULE.DEFAULT_SNIPPET_SOURCE,
                MODULE.DEFAULT_SNIPPET_DESTINATION,
            )

    def test_disabled_default_backup_is_outside_sites_enabled(self) -> None:
        self.assertNotEqual(
            MODULE.DEFAULT_OLD_DISABLED.parent,
            MODULE.DEFAULT_OLD_ENABLED.parent,
        )

    def test_cloudflare_file_accepts_only_cidr_directives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "cloudflare.conf"
            path.write_text(
                "\n".join(
                    [f"set_real_ip_from 192.0.2.{index}/32;" for index in range(20)]
                ),
                encoding="ascii",
            )
            self.assertEqual(MODULE.validate_cloudflare_ranges(path), 20)
            path.write_text("include /tmp/untrusted.conf;\n", encoding="ascii")
            with self.assertRaises(ValueError):
                MODULE.validate_cloudflare_ranges(path)

    def test_default_vhost_and_snippet_match_approved_security_contract(self) -> None:
        MODULE.validate_security_header_contract(
            MODULE.DEFAULT_SOURCE,
            MODULE.DEFAULT_SNIPPET_SOURCE,
            MODULE.DEFAULT_SNIPPET_DESTINATION,
        )
        snippet_text = MODULE.DEFAULT_SNIPPET_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("Content-Security-Policy", snippet_text)

    def test_vhost_rejects_nginx_owned_csp_in_both_modes(self) -> None:
        original = MODULE.DEFAULT_SOURCE.read_text(encoding="utf-8")
        include_line = f"    include {MODULE.DEFAULT_SNIPPET_DESTINATION};"
        for header_name in (
            "Content-Security-Policy",
            "Content-Security-Policy-Report-Only",
        ):
            with self.subTest(header_name=header_name):
                candidate = original.replace(
                    include_line,
                    f'{include_line}\n    add_header {header_name} "default-src \'none\'" always;',
                    1,
                )
                with self.assertRaisesRegex(ValueError, "must not own"):
                    self._validate_vhost_text(candidate)

    def test_csp_report_limit_contract_rejects_rate_burst_and_status_drift(self) -> None:
        original = MODULE.DEFAULT_SOURCE.read_text(encoding="utf-8")
        replacements = (
            ("rate=60r/m", "rate=10r/m", "60 requests per minute"),
            ("client_max_body_size 32k", "client_max_body_size 64k", "32 KiB"),
            ("burst=30 nodelay", "burst=10 nodelay", "unexpected directive"),
            ("limit_req_status 429;", "limit_req_status 503;", "HTTP 429"),
        )
        for current, replacement, message in replacements:
            with self.subTest(replacement=replacement):
                with self.assertRaisesRegex(ValueError, message):
                    self._validate_vhost_text(original.replace(current, replacement, 1))

    def test_csp_report_location_preserves_correlation_headers(self) -> None:
        original = MODULE.DEFAULT_SOURCE.read_text(encoding="utf-8")
        location_start = original.index("    location = /api/v1/security/csp-report {")
        location_end = original.index("\n    }", location_start)
        location = original[location_start:location_end]
        mutated_location = location.replace(
            "proxy_set_header CF-Ray $http_cf_ray;",
            "proxy_set_header CF-Ray missing;",
            1,
        )

        with self.assertRaisesRegex(ValueError, "correlation metadata"):
            self._validate_vhost_text(
                original[:location_start] + mutated_location + original[location_end:]
            )

    def test_non_csp_rate_limits_remain_unchanged(self) -> None:
        original = MODULE.DEFAULT_SOURCE.read_text(encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "media rate policy"):
            self._validate_vhost_text(
                original.replace(
                    "zone=platform_media_uploads:1m rate=10r/m",
                    "zone=platform_media_uploads:1m rate=60r/m",
                    1,
                )
            )

    def test_cloudflare_real_ip_contract_rejects_drift(self) -> None:
        original = MODULE.DEFAULT_SOURCE.read_text(encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "real-IP policy"):
            self._validate_vhost_text(
                original.replace("real_ip_recursive on;", "real_ip_recursive off;", 1)
            )

    def test_local_add_header_requires_shared_security_snippet(self) -> None:
        include_line = (
            f"        include {MODULE.DEFAULT_SNIPPET_DESTINATION};\n"
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            candidate = Path(temporary_dir) / "candidate.conf"
            candidate.write_text(
                MODULE.DEFAULT_SOURCE.read_text(encoding="utf-8").replace(
                    include_line,
                    "",
                    1,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "local add_header"):
                MODULE.validate_security_header_contract(
                    candidate,
                    MODULE.DEFAULT_SNIPPET_SOURCE,
                    MODULE.DEFAULT_SNIPPET_DESTINATION,
                )

    def test_default_vhost_rejects_tls12_cipher_policy_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            candidate = Path(temporary_dir) / "candidate.conf"
            candidate.write_text(
                MODULE.DEFAULT_SOURCE.read_text(encoding="utf-8").replace(
                    "ECDHE-ECDSA-AES128-GCM-SHA256:",
                    "ECDHE-ECDSA-AES128-SHA:",
                    1,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "TLS policy"):
                MODULE.validate_security_header_contract(
                    candidate,
                    MODULE.DEFAULT_SNIPPET_SOURCE,
                    MODULE.DEFAULT_SNIPPET_DESTINATION,
                )

    def test_install_rolls_back_vhost_and_snippet_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "source.conf"
            snippet_source = root / "source-snippet.conf"
            available = root / "sites-available/deadlock-platform"
            enabled = root / "sites-enabled/deadlock-platform"
            snippet_destination = root / "snippets/security.conf"
            old_enabled = root / "sites-enabled/default"
            old_disabled = root / "sites-available/default.pre-domain"

            source.write_bytes(b"new vhost\n")
            snippet_source.write_bytes(b"new snippet\n")
            available.parent.mkdir(parents=True)
            enabled.parent.mkdir(parents=True)
            snippet_destination.parent.mkdir(parents=True)
            available.write_bytes(b"old vhost\n")
            available.chmod(0o640)
            snippet_destination.write_bytes(b"old snippet\n")
            snippet_destination.chmod(0o600)
            enabled.symlink_to(available)
            old_enabled.write_bytes(b"old default\n")

            with (
                mock.patch.object(MODULE.os, "geteuid", return_value=0),
                mock.patch.object(MODULE, "DEFAULT_OLD_ENABLED", old_enabled),
                mock.patch.object(MODULE, "DEFAULT_OLD_DISABLED", old_disabled),
                mock.patch.object(
                    MODULE,
                    "run_checked",
                    side_effect=RuntimeError("candidate invalid"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "candidate invalid"):
                    MODULE.install(
                        source,
                        available,
                        enabled,
                        "nginx",
                        False,
                        snippet_source=snippet_source,
                        snippet_destination=snippet_destination,
                    )

            self.assertEqual(available.read_bytes(), b"old vhost\n")
            self.assertEqual(stat.S_IMODE(available.stat().st_mode), 0o640)
            self.assertEqual(snippet_destination.read_bytes(), b"old snippet\n")
            self.assertEqual(stat.S_IMODE(snippet_destination.stat().st_mode), 0o600)
            self.assertTrue(enabled.is_symlink())
            self.assertEqual(enabled.readlink(), available)
            self.assertEqual(old_enabled.read_bytes(), b"old default\n")
            self.assertFalse(old_disabled.exists())

    def test_install_updates_pair_when_only_snippet_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "source.conf"
            snippet_source = root / "source-snippet.conf"
            available = root / "sites-available/deadlock-platform"
            enabled = root / "sites-enabled/deadlock-platform"
            snippet_destination = root / "snippets/security.conf"
            old_enabled = root / "sites-enabled/default"
            old_disabled = root / "sites-available/default.pre-domain"

            source.write_bytes(b"same vhost\n")
            snippet_source.write_bytes(b"new snippet\n")
            available.parent.mkdir(parents=True)
            enabled.parent.mkdir(parents=True)
            snippet_destination.parent.mkdir(parents=True)
            available.write_bytes(source.read_bytes())
            available.chmod(0o644)
            snippet_destination.write_bytes(b"old snippet\n")
            snippet_destination.chmod(0o644)
            enabled.symlink_to(available)

            with (
                mock.patch.object(MODULE.os, "geteuid", return_value=0),
                mock.patch.object(MODULE, "DEFAULT_OLD_ENABLED", old_enabled),
                mock.patch.object(MODULE, "DEFAULT_OLD_DISABLED", old_disabled),
                mock.patch.object(MODULE, "run_checked") as run_checked,
            ):
                changed = MODULE.install(
                    source,
                    available,
                    enabled,
                    "nginx",
                    False,
                    snippet_source=snippet_source,
                    snippet_destination=snippet_destination,
                )

            self.assertTrue(changed)
            self.assertEqual(available.read_bytes(), source.read_bytes())
            self.assertEqual(snippet_destination.read_bytes(), snippet_source.read_bytes())
            self.assertEqual(enabled.readlink(), available)
            run_checked.assert_called_once_with(["nginx", "-t"])

if __name__ == "__main__":
    unittest.main()
