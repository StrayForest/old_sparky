from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
import json
import ssl
import time

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

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        records = getattr(self.server, "request_records")
        record_lock = getattr(self.server, "request_records_lock")
        with record_lock:
            records.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "body": body,
                    "cookie": self.headers.get("Cookie"),
                    "csrf": self.headers.get("X-CSRF-Token"),
                    "phase": self.headers.get("X-Platform-QA-Phase"),
                    "connection": self.headers.get("Connection"),
                    "client_port": self.client_address[1],
                }
            )
        response = json.dumps(
            {"code": "READY", "retryable": False}, separators=(",", ":")
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.send_header(
            "Connection",
            "close" if self.headers.get("Connection", "").lower() == "close" else "keep-alive",
        )
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class PlatformHttpTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _KeepAliveHandler)
        self.server.request_records = []
        self.server.request_records_lock = threading.Lock()
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

    def test_post_close_and_keepalive_share_request_contract_and_report_reuse(self) -> None:
        headers = {
            "Accept": "application/json",
            "Origin": self.origin,
            "User-Agent": "old-sparky-external-load/1",
            "Cookie": "session=synthetic-session; csrf=synthetic-csrf",
            "X-CSRF-Token": "synthetic-csrf",
            "X-Platform-QA-Phase": "write_external_vote_keepalive_b",
            "Content-Type": "application/json",
        }
        body = b'{"choice":"yes"}'
        path = "/api/v1/tournaments/synthetic/deadlock/ready-check/vote"
        close_client = HTTP11KeepAliveClient(
            self.origin,
            timeout=2,
            max_response_bytes=1024,
            reuse_connection=False,
        )
        try:
            close_first = close_client.request("POST", path, headers=headers, body=body)
            self.assertIsNone(close_client._connection)
            close_second = close_client.request("POST", path, headers=headers, body=body)
            self.assertIsNone(close_client._connection)
        finally:
            close_client.close()

        keepalive_client = HTTP11KeepAliveClient(
            self.origin,
            timeout=2,
            max_response_bytes=1024,
            reuse_connection=True,
        )
        try:
            keepalive_first = keepalive_client.request("POST", path, headers=headers, body=body)
            keepalive_second = keepalive_client.request("POST", path, headers=headers, body=body)
        finally:
            keepalive_client.close()

        self.assertEqual(
            [close_first.status, close_second.status, keepalive_first.status, keepalive_second.status],
            [200, 200, 200, 200],
        )
        self.assertEqual(
            [close_first.timing["transport"], close_second.timing["transport"]],
            ["http1-close", "http1-close"],
        )
        self.assertEqual(
            [keepalive_first.timing["transport"], keepalive_second.timing["transport"]],
            ["http1-keepalive", "http1-keepalive"],
        )
        self.assertEqual(
            [close_first.connection_reused, close_second.connection_reused],
            [False, False],
        )
        self.assertEqual(
            [keepalive_first.connection_reused, keepalive_second.connection_reused],
            [False, True],
        )
        self.assertTrue(
            all(
                isinstance(keepalive_first.timing.get(key), (int, float))
                and keepalive_first.timing[key] >= 0
                for key in PHASE_TIMING_KEYS
            )
        )
        records = self.server.request_records
        self.assertEqual(len(records), 4)
        self.assertEqual({record["method"] for record in records}, {"POST"})
        self.assertEqual({record["path"] for record in records}, {path})
        self.assertEqual({record["body"] for record in records}, {body})
        self.assertEqual(
            {record["cookie"] for record in records},
            {"session=synthetic-session; csrf=synthetic-csrf"},
        )
        self.assertEqual({record["csrf"] for record in records}, {"synthetic-csrf"})
        self.assertEqual(
            {record["phase"] for record in records},
            {"write_external_vote_keepalive_b"},
        )
        self.assertEqual(
            [record["connection"] for record in records],
            ["close", "close", "keep-alive", "keep-alive"],
        )
        self.assertEqual(records[2]["client_port"], records[3]["client_port"])

    def test_default_transport_still_uses_verified_tls_context(self) -> None:
        client = HTTP11KeepAliveClient(
            self.origin,
            timeout=2,
            max_response_bytes=1024,
        )
        try:
            self.assertIs(client._ssl_context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(client._ssl_context.check_hostname)
        finally:
            client.close()

    def test_post_response_limit_closes_socket_without_replaying_request(self) -> None:
        client = HTTP11KeepAliveClient(
            self.origin,
            timeout=2,
            max_response_bytes=4,
            reuse_connection=True,
        )
        try:
            with self.assertRaisesRegex(ValueError, "response exceeded its size limit"):
                client.request(
                    "POST",
                    "/api/v1/tournaments/synthetic/deadlock/ready-check/vote",
                    headers={"Content-Type": "application/json"},
                    body=b'{"choice":"yes"}',
                )
            self.assertIsNone(client._connection)
        finally:
            client.close()
        self.assertEqual(len(self.server.request_records), 1)
        self.assertEqual(self.server.request_records[0]["method"], "POST")

    def test_stale_reused_post_is_not_replayed_and_closes_connection(self) -> None:
        class StaleConnection:
            def __init__(self) -> None:
                self.sock = object()
                self.phase_timing: dict[str, float] = {}
                self.timeout = 2.0
                self.deadline = None
                self.request_calls = 0

            def request(self, *_args: object, **_kwargs: object) -> None:
                self.request_calls += 1
                raise BrokenPipeError("stale keep-alive socket")

            def close(self) -> None:
                self.sock = None

        client = HTTP11KeepAliveClient(
            self.origin,
            timeout=2,
            max_response_bytes=1024,
            reuse_connection=True,
        )
        stale = StaleConnection()
        client._connection = stale  # type: ignore[assignment]
        client.last_timing = {"ttfb_ms": 999.0}
        try:
            with self.assertRaises(BrokenPipeError):
                client.request(
                    "POST",
                    "/api/v1/tournaments/synthetic/deadlock/ready-check/vote",
                    headers={"Content-Type": "application/json"},
                    body=b'{"choice":"yes"}',
                )
            self.assertEqual(stale.request_calls, 1)
            self.assertIsNone(stale.sock)
            self.assertIsNone(client._connection)
            self.assertTrue(client.last_timing["connection_reused"])
            self.assertNotEqual(client.last_timing.get("ttfb_ms"), 999.0)
        finally:
            client.close()

    def test_expired_deadline_does_not_retain_previous_request_timing(self) -> None:
        client = HTTP11KeepAliveClient(
            self.origin,
            timeout=2,
            max_response_bytes=1024,
        )
        client.last_timing = {"ttfb_ms": 999.0}
        try:
            with self.assertRaises(TimeoutError):
                client.request(
                    "POST",
                    "/api/v1/tournaments/synthetic/deadlock/ready-check/vote",
                    headers={"Content-Type": "application/json"},
                    body=b'{"choice":"yes"}',
                    deadline=time.monotonic() - 1,
                )
            self.assertEqual(client.last_timing, {})
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
