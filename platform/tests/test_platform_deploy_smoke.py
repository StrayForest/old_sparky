from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest

import httpx


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "platform_deploy_smoke.py"
SPEC = importlib.util.spec_from_file_location("platform_deploy_smoke", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

NONCE = "MDEyMzQ1Njc4OWFiY2RlZg=="


def document_headers(mode: str, nonce: str = NONCE) -> dict[str, str]:
    headers = dict(MODULE.EXPECTED_COMMON_SECURITY_HEADERS)
    headers["Content-Type"] = "text/html; charset=utf-8"
    headers[MODULE.CSP_HEADER_BY_MODE[mode]] = MODULE.EXPECTED_CSP_POLICY_TEMPLATE.format(
        nonce=nonce
    )
    headers["Reporting-Endpoints"] = MODULE.EXPECTED_REPORTING_ENDPOINTS
    return headers


def document_html(nonce: str = NONCE) -> str:
    return (
        "<!doctype html><html><head>"
        f'<style nonce="{nonce}">body{{display:block}}</style>'
        "</head><body>Old Sparky Arena"
        f'<script nonce="{nonce}">self.__next_f=[]</script>'
        '<script src="/_next/static/chunks/app.js"></script>'
        "</body></html>"
    )


class PlatformDeploySmokeTests(unittest.TestCase):
    def test_expected_csp_mode_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            MODULE.parse_args([])
        for mode in MODULE.CSP_HEADER_BY_MODE:
            self.assertEqual(
                MODULE.parse_args(["--expected-csp-mode", mode]).expected_csp_mode,
                mode,
            )

    def test_loopback_tls_can_use_domain_host_header(self) -> None:
        args = MODULE.parse_args(
            [
                "--expected-csp-mode",
                "enforce",
                "--edge-origin",
                "https://127.0.0.1",
                "--edge-host",
                "old-sparky.com",
                "--edge-insecure-loopback",
            ]
        )

        MODULE.validate_edge_options(args)

        self.assertEqual(args.edge_host, "old-sparky.com")
        self.assertTrue(args.edge_insecure_loopback)
        self.assertTrue(MODULE.edge_origin_is_loopback(args.edge_origin))

    def test_public_edge_keeps_health_endpoint_private(self) -> None:
        self.assertFalse(MODULE.edge_origin_is_loopback("https://old-sparky.com"))
        self.assertTrue(MODULE.edge_origin_is_loopback("http://localhost"))

    def test_remote_origin_cannot_disable_tls_verification(self) -> None:
        args = MODULE.parse_args(
            [
                "--expected-csp-mode",
                "enforce",
                "--edge-origin",
                "https://old-sparky.com",
                "--edge-insecure-loopback",
            ]
        )

        with self.assertRaisesRegex(ValueError, "only for a literal loopback"):
            MODULE.validate_edge_options(args)

    def test_host_header_rejects_header_injection(self) -> None:
        args = MODULE.parse_args(
            [
                "--expected-csp-mode",
                "enforce",
                "--edge-host",
                "old-sparky.com\r\nX-Test: unsafe",
            ]
        )

        with self.assertRaisesRegex(ValueError, "valid ASCII hostname"):
            MODULE.validate_edge_options(args)

    def test_common_security_headers_are_accepted(self) -> None:
        self.assertEqual(
            MODULE.common_security_header_errors(
                {
                    name.lower(): value
                    for name, value in MODULE.EXPECTED_COMMON_SECURITY_HEADERS.items()
                }
            ),
            [],
        )

    def test_smoke_contract_matches_nginx_common_header_snippet(self) -> None:
        snippet_path = (
            SCRIPT_PATH.parents[1]
            / "deploy/nginx/snippets/deadlock-platform-security-headers.conf"
        )
        actual_headers: dict[str, str] = {}
        for line in snippet_path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = MODULE.re.fullmatch(
                r'add_header (?P<name>\S+) "(?P<value>.*)" always;',
                line,
            )
            self.assertIsNotNone(match)
            assert match is not None
            actual_headers[match.group("name")] = match.group("value")

        self.assertEqual(actual_headers, MODULE.EXPECTED_COMMON_SECURITY_HEADERS)

    def test_exact_report_only_and_enforced_document_policies_are_accepted(self) -> None:
        for mode in MODULE.CSP_HEADER_BY_MODE:
            with self.subTest(mode=mode):
                self.assertEqual(
                    MODULE.document_csp_header_errors(
                        document_headers(mode), document_html(), mode
                    ),
                    [],
                )

    def test_adsense_strict_csp_model_and_resource_fallbacks_are_exact(self) -> None:
        policy = MODULE.EXPECTED_CSP_POLICY_TEMPLATE.format(nonce=NONCE)
        directives = dict(
            directive.split(" ", 1)
            for directive in policy.split("; ")
        )

        self.assertEqual(
            directives["script-src"],
            f"'nonce-{NONCE}' 'unsafe-inline' 'unsafe-eval' 'strict-dynamic' https: http:",
        )
        self.assertEqual(directives["script-src-attr"], "'none'")
        self.assertEqual(directives["style-src"], f"'self' 'nonce-{NONCE}'")
        self.assertEqual(directives["style-src-attr"], "'unsafe-inline'")
        self.assertEqual(directives["default-src"], "'none'")
        self.assertEqual(directives["base-uri"], "'none'")
        self.assertEqual(directives["object-src"], "'none'")
        self.assertEqual(directives["frame-ancestors"], "'none'")
        self.assertEqual(directives["form-action"], "'self'")
        self.assertIn("https://pagead2.googlesyndication.com", directives["img-src"])
        self.assertIn("https://googleads.g.doubleclick.net", directives["img-src"])
        self.assertIn("https://csi.gstatic.com", directives["img-src"])
        self.assertIn("https://fundingchoicesmessages.google.com", directives["connect-src"])
        self.assertIn("https://csi.gstatic.com", directives["connect-src"])
        self.assertIn("https://tpc.googlesyndication.com", directives["frame-src"])
        self.assertNotIn("pagead2.googlesyndication.com", directives["script-src"])
        self.assertNotIn("doubleclick.net", directives["script-src"])

    def test_adsense_style_attributes_are_allowed_without_relaxing_style_elements(self) -> None:
        html = (
            f'<style nonce="{NONCE}">.ad{{display:block}}</style>'
            '<ins class="adsbygoogle" style="display:block"></ins>'
        )

        self.assertEqual(
            MODULE.document_csp_header_errors(
                document_headers("enforce"), html, "enforce"
            ),
            [],
        )
        self.assertIn(
            "style tags without document nonce: 1",
            MODULE.document_csp_header_errors(
                document_headers("enforce"),
                '<style>.ad{display:block}</style>',
                "enforce",
            ),
        )

    def test_adsense_markup_requires_one_canonical_loader(self) -> None:
        html = (
            '<script async src="https://pagead2.googlesyndication.com/pagead/js/'
            'adsbygoogle.js?client=ca-pub-7185165276065459"></script>'
        )

        self.assertEqual(MODULE.adsense_markup_errors(html), [])
        self.assertIn(
            "unexpected AdSense loader source",
            MODULE.adsense_markup_errors(html.replace("https://pagead2", "https://evil")),
        )
        self.assertIn(
            "expected exactly one AdSense loader script, found 2",
            MODULE.adsense_markup_errors(html + html),
        )

    def test_document_policy_rejects_wrong_mode_unsafe_source_and_short_nonce(self) -> None:
        headers = document_headers("report-only", "c2hvcnQ=")
        headers["Content-Security-Policy-Report-Only"] += " script-src 'unsafe-inline'"
        headers["Content-Security-Policy"] = "default-src 'none'"

        errors = MODULE.document_csp_header_errors(
            headers,
            '<script style="color:red" onclick="alert(1)">alert(1)</script>',
            "report-only",
        )

        self.assertIn("unexpected Content-Security-Policy", errors)
        self.assertIn("CSP nonce has less than 128 bits of encoded entropy", errors)
        self.assertIn("unexpected CSP policy", errors)
        self.assertIn("inline scripts without document nonce: 1", errors)
        self.assertIn("inline event handlers present: 1", errors)

    def test_duplicate_document_policy_is_rejected(self) -> None:
        policy = MODULE.EXPECTED_CSP_POLICY_TEMPLATE.format(nonce=NONCE)
        headers = httpx.Headers(
            [
                ("Content-Security-Policy", policy),
                ("Content-Security-Policy", policy),
                ("Reporting-Endpoints", MODULE.EXPECTED_REPORTING_ENDPOINTS),
            ]
        )

        self.assertIn(
            "duplicate Content-Security-Policy",
            MODULE.document_csp_header_errors(headers, document_html(), "enforce"),
        )

    def test_non_document_responses_reject_csp_and_reporting_headers(self) -> None:
        headers = document_headers("enforce")

        errors = MODULE.non_document_csp_header_errors(headers)

        self.assertIn(
            "unexpected Content-Security-Policy on non-document response", errors
        )
        self.assertIn("unexpected Reporting-Endpoints on non-document response", errors)

    def test_live_smoke_covers_the_next_apple_icon_as_a_non_document(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn('"edge_security_apple_icon_cache_busted"', source)
        self.assertIn('f"{args.edge_origin}/apple-icon.png"', source)

    def test_edge_owned_hsts_is_ignored_without_weakening_origin_policy(self) -> None:
        headers = dict(MODULE.EXPECTED_COMMON_SECURITY_HEADERS)
        headers["Strict-Transport-Security"] = "max-age=15552000"

        self.assertEqual(MODULE.common_security_header_errors(headers), [])

    def test_security_header_http_check_requires_expected_status(self) -> None:
        async def run_check() -> dict[str, object]:
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    404,
                    headers=MODULE.EXPECTED_COMMON_SECURITY_HEADERS,
                    request=request,
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                return await MODULE.check_http_security_headers(
                    client,
                    "security_404",
                    "https://old-sparky.com/api/v1/missing",
                    expected_status=404,
                )

        result = MODULE.asyncio.run(run_check())

        self.assertTrue(result["ok"])

    def test_document_http_check_validates_header_and_html_nonce(self) -> None:
        async def run_check() -> dict[str, object]:
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    headers=document_headers("enforce"),
                    text=document_html(),
                    request=request,
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                return await MODULE.check_http_document(
                    client,
                    "document",
                    "https://old-sparky.com/",
                    expected_status=200,
                    expected_csp_mode="enforce",
                    contains="Old Sparky Arena",
                    require_common_security_headers=True,
                )

        self.assertTrue(MODULE.asyncio.run(run_check())["ok"])

    def test_nonce_rotation_requires_distinct_values(self) -> None:
        nonces = [NONCE, "ZmVkY2JhOTg3NjU0MzIxMA=="]

        async def run_check() -> dict[str, object]:
            calls = 0

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal calls
                nonce = nonces[calls]
                calls += 1
                return httpx.Response(
                    200,
                    headers=document_headers("enforce", nonce),
                    text=document_html(nonce),
                    request=request,
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                return await MODULE.check_csp_nonce_rotation(
                    client,
                    "rotation",
                    "https://old-sparky.com/",
                    expected_csp_mode="enforce",
                )

        self.assertTrue(MODULE.asyncio.run(run_check())["ok"])

    def test_report_endpoint_posts_a_bounded_harmless_legacy_report(self) -> None:
        seen: dict[str, object] = {}

        async def run_check() -> dict[str, object]:
            def handler(request: httpx.Request) -> httpx.Response:
                seen["content_type"] = request.headers["Content-Type"]
                seen["payload"] = json.loads(request.content)
                return httpx.Response(204, request=request)

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                return await MODULE.check_csp_report_endpoint(
                    client,
                    "report",
                    "https://old-sparky.com",
                    expected_csp_mode="enforce",
                )

        self.assertTrue(MODULE.asyncio.run(run_check())["ok"])
        self.assertEqual(seen["content_type"], "application/csp-report")
        payload = seen["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["csp-report"]["disposition"], "enforce")
        self.assertNotIn("script-sample", payload["csp-report"])


if __name__ == "__main__":
    unittest.main()
