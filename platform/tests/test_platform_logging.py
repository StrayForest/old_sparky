from __future__ import annotations

import json
import logging
import unittest

from python_packages.platform_infra.logging import JsonUtcFormatter, redact_log_text


class PlatformLoggingTests(unittest.TestCase):
    def test_sensitive_values_are_redacted(self) -> None:
        text = redact_log_text(
            "password=hunter2 turnstile_token:abc cookie=session authorization=Bearer"
        )

        self.assertNotIn("hunter2", text)
        self.assertNotIn("abc", text)
        self.assertNotIn("session", text.split("cookie=", 1)[1])
        self.assertIn("authorization=[REDACTED]", text)

    def test_production_formatter_emits_utc_json(self) -> None:
        record = logging.LogRecord(
            name="platform.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="request_perf password=secret",
            args=(),
            exc_info=None,
        )

        payload = json.loads(JsonUtcFormatter().format(record))

        self.assertTrue(payload["timestamp"].endswith("Z"))
        self.assertEqual(payload["service"], "deadlock-platform")
        self.assertNotIn("secret", payload["message"])


if __name__ == "__main__":
    unittest.main()
