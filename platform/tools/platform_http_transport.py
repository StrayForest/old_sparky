#!/usr/bin/env python3
"""Bounded HTTP/1.1 transport with phase timing for the external load client.

The canonical v1 load client intentionally remains urllib-based.  This module
is an explicit candidate transport for a versioned page-load profile.  It uses
one connection per external-runner worker thread, drains every response body,
and never serializes request headers or response bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import socket
import ssl
import time
from typing import Any
from urllib.parse import urlsplit


SAFE_RESPONSE_HEADERS = frozenset(
    {
        "cf-error-origin",
        "cf-error-type",
        "cf-ray",
        "connection",
        "etag",
        "retry-after",
    }
)
PHASE_TIMING_KEYS = (
    "dns_ms",
    "tcp_connect_ms",
    "tls_handshake_ms",
    "request_write_ms",
    "edge_wait_ms",
    "ttfb_ms",
    "body_receive_ms",
    "total_ms",
)


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status: int
    reason: str
    headers: dict[str, str]
    body: bytes
    http_version: str
    connection_reused: bool
    timing: dict[str, Any]
    # Closed projection only; never include the raw response header value.
    content_encoding: str | None = None
    content_type_is_html: bool | None = None


def _closed_content_encoding(value: Any) -> str:
    if value is None or value == "":
        return "identity"
    if not isinstance(value, str):
        return "other"
    normalized = value.strip().lower()
    return normalized if normalized in {"identity", "gzip", "deflate", "br"} else "other"


def _closed_content_type_is_html(value: Any) -> bool:
    media_type = value.split(";", 1)[0].strip().lower() if isinstance(value, str) else ""
    return media_type == "text/html"


class _TimedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that separates name resolution and TCP connection time."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        deadline: float | None = None,
    ) -> None:
        super().__init__(host, port, timeout=timeout)
        self.phase_timing: dict[str, float] = {}
        self._last_connect_finished: float | None = None
        self.deadline = deadline

    def _remaining_timeout(self) -> float:
        if self.deadline is None:
            return float(self.timeout)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTP transport absolute deadline exceeded")
        return min(float(self.timeout), remaining)

    def endheaders(
        self,
        message_body: bytes | str | None = None,
        *,
        encode_chunked: bool = False,
    ) -> None:
        write_started = time.perf_counter()
        super().endheaders(message_body, encode_chunked=encode_chunked)
        effective_start = self._last_connect_finished or write_started
        self.phase_timing["request_write_ms"] = (
            time.perf_counter() - effective_start
        ) * 1_000
        self._last_connect_finished = None

    def connect(self) -> None:
        dns_started = time.perf_counter()
        addresses = socket.getaddrinfo(
            self.host,
            self.port,
            type=socket.SOCK_STREAM,
        )
        dns_finished = time.perf_counter()
        self.phase_timing["dns_ms"] = (dns_finished - dns_started) * 1_000

        last_error: OSError | None = None
        tcp_started = time.perf_counter()
        for family, socktype, proto, _canonname, sockaddr in addresses:
            candidate = socket.socket(family, socktype, proto)
            candidate.settimeout(self._remaining_timeout())
            try:
                candidate.connect(sockaddr)
            except OSError as error:
                candidate.close()
                last_error = error
                continue
            self.sock = candidate
            break
        else:
            if last_error is not None:
                raise last_error
            raise OSError(f"Unable to resolve a connection address for {self.host!r}")
        self.phase_timing["tcp_connect_ms"] = (time.perf_counter() - tcp_started) * 1_000
        self.phase_timing["tls_handshake_ms"] = 0.0
        self._last_connect_finished = time.perf_counter()


class _TimedHTTPSConnection(_TimedHTTPConnection):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        context: ssl.SSLContext,
        deadline: float | None = None,
    ) -> None:
        super().__init__(host, port, timeout=timeout, deadline=deadline)
        self.context = context

    def connect(self) -> None:
        super().connect()
        assert self.sock is not None
        self.sock.settimeout(self._remaining_timeout())
        tls_started = time.perf_counter()
        try:
            self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
        except BaseException:
            self.close()
            raise
        self.phase_timing["tls_handshake_ms"] = (time.perf_counter() - tls_started) * 1_000
        self._last_connect_finished = time.perf_counter()


class HTTP11KeepAliveClient:
    """Issue bounded HTTP/1.1 requests over one optional per-thread socket."""

    def __init__(
        self,
        origin: str,
        *,
        timeout: float,
        max_response_bytes: int,
        reuse_connection: bool = True,
    ) -> None:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("HTTP transport origin must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query:
            raise ValueError("HTTP transport origin must not contain credentials or a path")
        self.origin = origin.rstrip("/")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._reuse_connection = reuse_connection
        self._connection: _TimedHTTPConnection | None = None
        self._ssl_context = ssl.create_default_context()
        self.last_timing: dict[str, Any] = {}
        self.last_failure_stage: str | None = None
        self.last_observed_status: int | None = None

    def set_timeout(self, timeout: float) -> None:
        """Apply the current request timeout to a reused socket as well."""

        numeric = float(timeout)
        if not numeric > 0:
            raise ValueError("HTTP transport timeout must be positive")
        self._timeout = numeric
        if self._connection is not None:
            self._connection.timeout = numeric
            if self._connection.sock is not None:
                self._connection.sock.settimeout(numeric)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _new_connection(self, *, deadline: float | None = None) -> _TimedHTTPConnection:
        if self._scheme == "https":
            return _TimedHTTPSConnection(
                self._host,
                self._port,
                timeout=self._timeout,
                context=self._ssl_context,
                deadline=deadline,
            )
        return _TimedHTTPConnection(
            self._host,
            self._port,
            timeout=self._timeout,
            deadline=deadline,
        )

    @staticmethod
    def _http_version(response: http.client.HTTPResponse) -> str:
        version = response.version
        if version == 11:
            return "1.1"
        if version == 10:
            return "1.0"
        return str(version)

    def get(
        self,
        path: str,
        *,
        headers: dict[str, str],
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> TransportResponse:
        return self.request(
            "GET",
            path,
            headers=headers,
            timeout=timeout,
            deadline=deadline,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> TransportResponse:
        # Timing belongs to this physical request attempt only. In particular,
        # a rejected deadline must not surface a previous request's timings.
        self.last_timing = {}
        self.last_failure_stage = None
        self.last_observed_status = None
        if method not in {"GET", "POST"}:
            raise ValueError("HTTP transport method is unsupported")
        if not path.startswith("/") or "\r" in path or "\n" in path:
            raise ValueError("HTTP transport path is invalid")
        if timeout is not None:
            self.set_timeout(timeout)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HTTP transport absolute deadline exceeded")
            self.set_timeout(min(self._timeout, remaining))
        connection = self._connection
        reused = connection is not None and connection.sock is not None
        if connection is None:
            connection = self._new_connection(deadline=deadline)
            self._connection = connection
        elif deadline is not None:
            connection.deadline = deadline
            connection.timeout = min(connection.timeout, max(1e-6, deadline - time.monotonic()))
            if connection.sock is not None:
                connection.sock.settimeout(connection.timeout)

        started = time.perf_counter()
        # connection.request() can include DNS/TCP/TLS setup as well as the
        # request write, so this is intentionally one combined stage.
        failure_stage = "connect_or_request_write"
        connection.phase_timing = {}
        timing: dict[str, Any] = {
            "transport": (
                "http1-keepalive" if self._reuse_connection else "http1-close"
            ),
            "connection_reused": reused,
        }
        try:
            connection.request(
                method,
                path,
                body=body,
                headers={
                    **headers,
                    "Connection": (
                        "keep-alive" if self._reuse_connection else "close"
                    ),
                },
            )
            write_finished = time.perf_counter()
            timing.update(connection.phase_timing)
            timing.setdefault(
                "request_write_ms",
                (write_finished - started) * 1_000,
            )

            failure_stage = "response_headers"
            response = connection.getresponse()
            self.last_observed_status = int(response.status)
            failure_stage = "body_receive"
            headers_finished = time.perf_counter()
            timing["edge_wait_ms"] = (headers_finished - write_finished) * 1_000
            timing["ttfb_ms"] = (headers_finished - started) * 1_000
            if deadline is not None and connection.sock is not None:
                connection.sock.settimeout(connection._remaining_timeout())
            body = response.read(self._max_response_bytes + 1)
            body_finished = time.perf_counter()
            timing["body_receive_ms"] = (body_finished - headers_finished) * 1_000
            timing["total_ms"] = (body_finished - started) * 1_000
            timing["http_version"] = self._http_version(response)
            if len(body) > self._max_response_bytes:
                raise ValueError("HTTP transport response exceeded its size limit")

            safe_headers = {
                name: value[:512]
                for name, value in (
                    (header.lower(), response.headers.get(header, ""))
                    for header in SAFE_RESPONSE_HEADERS
                )
                if value
            }
            content_encoding = _closed_content_encoding(
                response.headers.get("content-encoding"),
            )
            content_type_is_html = _closed_content_type_is_html(
                response.headers.get("content-type"),
            )
            if response.will_close or safe_headers.get("connection", "").lower() == "close":
                self.close()
            elif not self._reuse_connection:
                self.close()
            self.last_timing = timing
            self.last_failure_stage = None
            return TransportResponse(
                status=int(response.status),
                reason=str(response.reason),
                headers=safe_headers,
                body=body,
                http_version=str(timing["http_version"]),
                connection_reused=reused,
                timing=timing,
                content_encoding=content_encoding,
                content_type_is_html=content_type_is_html,
            )
        except BaseException:
            timing.update(connection.phase_timing)
            timing["total_ms"] = (time.perf_counter() - started) * 1_000
            self.last_timing = timing
            self.last_failure_stage = failure_stage
            self.close()
            raise
