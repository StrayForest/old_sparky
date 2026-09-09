from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
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


if __name__ == "__main__":
    unittest.main()
