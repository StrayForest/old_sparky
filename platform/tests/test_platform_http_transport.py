from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

from tools.platform_http_transport import HTTP11KeepAliveClient, PHASE_TIMING_KEYS


class _KeepAliveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        body = b"transport-test"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "keep-alive")
        self.send_header("ETag", '"transport-test"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class PlatformHttpTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _KeepAliveHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_get_reports_phases_and_reuses_a_connection(self) -> None:
        client = HTTP11KeepAliveClient(
            self.origin,
            timeout=2,
            max_response_bytes=1024,
        )
        try:
            first = client.get("/first", headers={"Accept": "text/plain"})
            second = client.get("/second", headers={"Accept": "text/plain"})
        finally:
            client.close()

        self.assertEqual(first.status, 200)
        self.assertEqual(first.body, b"transport-test")
        self.assertEqual(first.headers["etag"], '"transport-test"')
        self.assertEqual(first.http_version, "1.1")
        self.assertFalse(first.connection_reused)
        self.assertTrue(second.connection_reused)
        self.assertEqual(second.http_version, "1.1")
        self.assertTrue(
            all(
                isinstance(first.timing.get(key), (int, float))
                and first.timing[key] >= 0
                for key in PHASE_TIMING_KEYS
            )
        )
        self.assertTrue(
            all(
                isinstance(second.timing.get(key), (int, float))
                and second.timing[key] >= 0
                for key in ("request_write_ms", "edge_wait_ms", "ttfb_ms", "body_receive_ms", "total_ms")
            )
        )

    def test_rejects_unsafe_origin_and_path(self) -> None:
        with self.assertRaises(ValueError):
            HTTP11KeepAliveClient(
                "https://user:password@example.test",
                timeout=2,
                max_response_bytes=1024,
            )
        client = HTTP11KeepAliveClient(
            self.origin,
            timeout=2,
            max_response_bytes=1024,
        )
        with self.assertRaises(ValueError):
            client.get("not-absolute", headers={})
        client.close()


if __name__ == "__main__":
    unittest.main()
