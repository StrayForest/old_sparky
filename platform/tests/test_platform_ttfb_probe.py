from __future__ import annotations

import http.server
import importlib.util
from pathlib import Path
import stat
import threading
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "platform_ttfb_probe.py"
SPEC = importlib.util.spec_from_file_location("platform_ttfb_probe", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class _ProbeHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Encoding", "br")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.write(b"<html></html>")

    def log_message(self, _format: str, *_args: object) -> None:
        return


class PlatformTtfbProbeTests(unittest.TestCase):
    def test_measure_hop_reports_ttfb_and_transport_headers_without_body(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = MODULE.measure_hop(
                "next",
                f"http://127.0.0.1:{server.server_port}/tournaments/fixture",
                cookie="__Host-old_sparky_session=secret",
                host_header="old-sparky.com",
                request_id="probe-1",
                accept_encoding="gzip, br",
                timeout_seconds=5,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result["status"], 200)
        self.assertGreaterEqual(result["ttfb_ms"], 0)
        self.assertEqual(result["body_bytes"], 13)
        self.assertEqual(result["headers"]["content-encoding"], "br")
        self.assertEqual(result["headers"]["x-accel-buffering"], "no")
        self.assertNotIn("secret", str(result))

    def test_cookie_file_must_be_private_regular_file(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "cookie"
            path.write_text("session=secret\n", encoding="utf-8")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            self.assertEqual(MODULE.read_cookie_file(path), "session=secret")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
            with self.assertRaisesRegex(ValueError, "group/world"):
                MODULE.read_cookie_file(path)

    def test_parse_hop_requires_named_http_url(self) -> None:
        self.assertEqual(MODULE.parse_hop("next=http://127.0.0.1:3000"), ("next", "http://127.0.0.1:3000"))
        with self.assertRaises(ValueError):
            MODULE.parse_hop("http://127.0.0.1:3000")


if __name__ == "__main__":
    unittest.main()
