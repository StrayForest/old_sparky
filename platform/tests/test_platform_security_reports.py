from __future__ import annotations

import json
import unittest

from fastapi import Request

from apps.platform_api.app.api.routes.security_reports import (
    MAX_REPORT_BATCH,
    MAX_REPORT_BYTES,
    _report_rows,
    _safe_status_code,
    _safe_url,
    receive_csp_report,
)


class PlatformSecurityReportTests(unittest.TestCase):
    def test_csp_report_strips_queries_and_ignores_script_sample(self) -> None:
        rows = _report_rows(
            {
                "csp-report": {
                    "document-uri": (
                        "https://account:password@old-sparky.com/profile?token=secret#private"
                    ),
                    "blocked-uri": "https://evil.example/payload.js?secret=1#fragment",
                    "effective-directive": "script-src-elem",
                    "script-sample": "sensitive inline content",
                    "status-code": 200,
                }
            }
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["document_uri"], "https://old-sparky.com/profile")
        self.assertEqual(rows[0]["blocked_uri"], "https://evil.example/payload.js")
        self.assertNotIn("script", rows[0])
        self.assertEqual(rows[0]["report_format"], "legacy")

    def test_url_sentinels_are_safe_and_distinct(self) -> None:
        expected = {
            "inline": "inline",
            "eval": "eval",
            "blob:https://old-sparky.com/id": "blob",
            "data:text/javascript,secret": "data",
            "chrome-extension://extension-id/script.js": "browser-extension",
            "moz-extension://extension-id/script.js": "browser-extension",
            "not a report URL": "invalid",
            "": "invalid",
        }
        for value, result in expected.items():
            with self.subTest(value=value):
                self.assertEqual(_safe_url(value), result)

    def test_hostile_status_values_are_normalized_without_raising(self) -> None:
        for value in ("not-a-number", {"secret": "value"}, [], True, 200.5, -1, 1000):
            with self.subTest(value=value):
                self.assertEqual(_safe_status_code(value), 0)
        self.assertEqual(_safe_status_code("200"), 200)

    def test_reporting_api_batch_is_bounded(self) -> None:
        rows = _report_rows(
            [
                {
                    "type": "csp-violation",
                    "body": {
                        "effectiveDirective": "img-src",
                        "disposition": "enforce",
                    },
                }
            ]
            * 20
        )

        self.assertEqual(len(rows), MAX_REPORT_BATCH)
        self.assertEqual(rows[0]["report_format"], "reporting-api")
        self.assertEqual(rows[0]["batch_index"], 1)
        self.assertEqual(rows[-1]["batch_index"], MAX_REPORT_BATCH)
        self.assertEqual(rows[-1]["batch_size"], MAX_REPORT_BATCH)
        self.assertEqual(rows[0]["disposition"], "enforce")

    def test_reporting_api_batch_ignores_non_csp_report_types(self) -> None:
        rows = _report_rows(
            [
                {"type": "deprecation", "body": {"effectiveDirective": "secret"}},
                {"type": "csp-violation", "body": {"effectiveDirective": "script-src"}},
            ]
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["batch_index"], 2)

    def test_directive_and_disposition_fields_never_log_raw_values(self) -> None:
        rows = _report_rows(
            {
                "csp-report": {
                    "effective-directive": "script-src https://token.example/secret",
                    "violated-directive": "token=secret",
                    "disposition": "enforce token=secret",
                }
            }
        )

        self.assertEqual(rows[0]["effective_directive"], "script-src")
        self.assertEqual(rows[0]["violated_directive"], "invalid")
        self.assertEqual(rows[0]["disposition"], "invalid")


class PlatformSecurityReportEndpointTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _request(body: bytes, *, headers: dict[str, str] | None = None) -> Request:
        request_headers = {
            "content-type": "application/csp-report",
            **(headers or {}),
        }
        messages = [
            {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }
        ]

        async def receive() -> dict[str, object]:
            if messages:
                return messages.pop(0)
            return {"type": "http.disconnect"}

        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/security/csp-report",
                "headers": [
                    (name.lower().encode("ascii"), value.encode("latin-1"))
                    for name, value in request_headers.items()
                ],
            },
            receive,
        )

    async def test_legacy_report_logs_only_normalized_fields_and_request_metadata(self) -> None:
        body = json.dumps(
            {
                "csp-report": {
                    "document-uri": (
                        "https://user:password@old-sparky.com/profile?token=secret#fragment"
                    ),
                    "blocked-uri": "inline",
                    "effective-directive": "script-src-elem",
                    "disposition": "enforce",
                    "status-code": "hostile-status",
                    "script-sample": "sensitive inline content",
                }
            }
        ).encode("utf-8")
        request = self._request(
            body,
            headers={
                "content-length": str(len(body)),
                "x-request-id": "request-123",
                "cf-ray": "abc123-HEL",
            },
        )

        with self.assertLogs("platform.security.csp", level="WARNING") as captured:
            response = await receive_csp_report(request)

        self.assertEqual(response.status_code, 204)
        log_output = "\n".join(captured.output)
        self.assertIn("request_id=request-123", log_output)
        self.assertIn("cf_ray=abc123-HEL", log_output)
        self.assertIn("report_format=legacy", log_output)
        self.assertIn("batch_position=1/1", log_output)
        self.assertIn("disposition=enforce", log_output)
        self.assertIn("status_code=0", log_output)
        self.assertNotIn("password", log_output)
        self.assertNotIn("token", log_output)
        self.assertNotIn("secret", log_output)
        self.assertNotIn("script-sample", log_output)

    async def test_reporting_api_batch_logs_each_original_batch_position(self) -> None:
        body = json.dumps(
            [
                {
                    "type": "csp-violation",
                    "body": {"effectiveDirective": "style-src-attr"},
                },
                {
                    "type": "csp-violation",
                    "body": {"effectiveDirective": "img-src"},
                },
            ]
        ).encode("utf-8")
        request = self._request(body, headers={"content-type": "application/reports+json"})

        with self.assertLogs("platform.security.csp", level="WARNING") as captured:
            response = await receive_csp_report(request)

        self.assertEqual(response.status_code, 204)
        self.assertEqual(len(captured.output), 2)
        self.assertIn("report_format=reporting-api", captured.output[0])
        self.assertIn("batch_position=1/2", captured.output[0])
        self.assertIn("batch_position=2/2", captured.output[1])
        self.assertTrue(all("row_count=2" in line for line in captured.output))

    async def test_malformed_json_is_accepted_without_logging_payload(self) -> None:
        request = self._request(b'{"token":"secret"')

        with self.assertNoLogs("platform.security.csp", level="WARNING"):
            response = await receive_csp_report(request)

        self.assertEqual(response.status_code, 204)

    async def test_excessively_nested_json_is_accepted_without_server_error(self) -> None:
        request = self._request(b"[" * 1_100 + b"]" * 1_100)

        response = await receive_csp_report(request)

        self.assertEqual(response.status_code, 204)

    async def test_invalid_content_length_is_rejected(self) -> None:
        response = await receive_csp_report(
            self._request(b"{}", headers={"content-length": "not-a-number"})
        )

        self.assertEqual(response.status_code, 400)

    async def test_declared_oversized_body_is_rejected(self) -> None:
        response = await receive_csp_report(
            self._request(b"", headers={"content-length": str(MAX_REPORT_BYTES + 1)})
        )

        self.assertEqual(response.status_code, 413)

    async def test_streamed_oversized_body_is_rejected(self) -> None:
        response = await receive_csp_report(self._request(b"x" * (MAX_REPORT_BYTES + 1)))

        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
