from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "cloudflare_readonly_audit.py"
SPEC = importlib.util.spec_from_file_location("cloudflare_readonly_audit", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CloudflareReadonlyAuditTests(unittest.TestCase):
    def test_ruleset_summary_keeps_response_body_buffering_parameter(self) -> None:
        result = MODULE.ApiResult(
            status=200,
            payload={
                "success": True,
                "result": {
                    "id": "ruleset-id",
                    "rules": [
                        {
                            "id": "rule-id",
                            "ref": "html-streaming",
                            "expression": "starts_with(http.request.uri.path, \"/tournaments/\")",
                            "enabled": True,
                            "action_parameters": {
                                "response_body_buffering": "none",
                                "cache": False,
                            },
                        }
                    ],
                },
            },
        )

        summary = MODULE.ruleset_summary(result)

        self.assertEqual(summary["ruleset_id"], "ruleset-id")
        self.assertEqual(
            summary["rules"][0]["action_parameters"]["response_body_buffering"],
            "none",
        )

    def test_public_projector_is_closed_and_drops_provider_strings(self) -> None:
        private_key_marker = "".join(
            ("-----BEGIN ", "OPENSSH ", "PRIVATE ", "KEY-----")
        )
        private = {
            "generated_at": "2026-09-12T00:00:00+00:00",
            "zone_name": "old-sparky.com",
            "account_id_suffix": "account-secret",
            "checks": [
                {
                    "id": "dns-routing",
                    "status": "PASS",
                    "summary": (
                        "alice@example.com Authorization: Bearer secret-token "
                        "Cookie=session=secret-session; "
                        "https://example.test/invite?token=secret-token "
                        "203.0.113.7 2001:db8::7 /home/root/id_rsa "
                        + private_key_marker
                        + " ssh -i /root/.ssh/id_ed25519 operator@example.com "
                        "SELECT email FROM users WHERE password='secret-password'"
                    ),
                    "evidence": {
                        "http_status": 200,
                        "records": {
                            "old-sparky.com": [
                                {
                                    "name": "old-sparky.com",
                                    "content": "203.0.113.7",
                                }
                            ]
                        },
                    },
                },
                {
                    "id": "turnstile-hostnames",
                    "status": "REVIEW",
                    "evidence": {
                        "widgets": [
                            {
                                "sitekey": "sitekey-secret",
                                "domains": ["https://private.example/?token=abc"],
                            }
                        ],
                        "unexpected_domains": ["private.example"],
                    },
                },
                {
                    "id": "managed-and-custom-waf",
                    "status": "FAIL",
                    "evidence": {
                        "http_status": 500,
                        "error_codes": ["provider-error"],
                        "rules": [
                            {
                                "expression": "http.request.uri.query contains 'token'",
                                "description": "do not publish",
                            }
                        ],
                    },
                },
            ],
        }

        public = MODULE.project_public_report(private)
        encoded = json.dumps(public, sort_keys=True)

        self.assertEqual(public["schema"], "cloudflare-audit-public-v1")
        self.assertTrue(public["read_only"])
        self.assertEqual(len(public["checks"]), len(MODULE.PUBLIC_CHECK_RESOURCES))
        self.assertNotIn("alice@example.com", encoded)
        for forbidden in (
            "account-secret",
            "sitekey-secret",
            "203.0.113.7",
            "2001:db8::7",
            "secret-token",
            "secret-session",
            "private.example",
            "token-secret",
            "/home/root",
            "id_ed25519",
            "OPENSSH PRIVATE KEY",
            "SELECT",
        ):
            self.assertNotIn(forbidden, encoded)
        for check in public["checks"]:
            self.assertEqual(
                set(check),
                {
                    "id",
                    "resource_class",
                    "status",
                    "error_class",
                    "counts",
                    "booleans",
                    "numeric",
                },
            )
            self.assertIn(check["status"], MODULE.PUBLIC_STATUS)
            self.assertIn(check["error_class"], MODULE.PUBLIC_ERROR_CLASSES)

    def test_private_raw_report_writer_refuses_symlink_and_uses_mode_six_hundred(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report.json"
            MODULE._write_json(output, {"safe": True})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(output.read_text()), {"safe": True})

            target = root / "target.json"
            link = root / "raw.json"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                MODULE._write_json(link, {"secret": True})
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
