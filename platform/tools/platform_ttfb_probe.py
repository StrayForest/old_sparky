#!/usr/bin/env python3
"""Measure the same authenticated HTML request at multiple network hops.

This is an operator diagnostic, not a load generator. It reports time to the
HTTP response headers (the same boundary used by curl's time_starttransfer),
selected transport headers and total body time without printing cookies or the
response body.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import stat
import time
from urllib.parse import urlsplit


SAFE_RESPONSE_HEADERS = (
    "cache-control",
    "content-encoding",
    "content-length",
    "content-type",
    "cf-cache-status",
    "cf-ray",
    "server",
    "transfer-encoding",
    "x-accel-buffering",
    "x-request-id",
)
MAX_COOKIE_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def parse_hop(raw_value: str) -> tuple[str, str]:
    name, separator, url = raw_value.partition("=")
    if not separator or not name or not url:
        raise ValueError("--hop must use NAME=URL")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Unsupported hop URL for {name!r}")
    return name, url


def read_cookie_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Cookie file must be a regular file, not a symlink.")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("Cookie file must not be group/world accessible.")
    if metadata.st_size > MAX_COOKIE_BYTES:
        raise ValueError("Cookie file is unexpectedly large.")
    cookie = path.read_text(encoding="utf-8").strip()
    if not cookie or "\n" in cookie or "\r" in cookie:
        raise ValueError("Cookie file must contain one non-empty Cookie header value.")
    return cookie


def _connection(parsed, timeout_seconds: float):
    port = parsed.port
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(parsed.hostname, port, timeout=timeout_seconds)
    return http.client.HTTPConnection(parsed.hostname, port, timeout=timeout_seconds)


def measure_hop(
    name: str,
    url: str,
    *,
    cookie: str | None,
    host_header: str | None,
    request_id: str,
    accept_encoding: str,
    timeout_seconds: float,
) -> dict[str, object]:
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    headers = {
        "Accept": "text/html",
        "Accept-Encoding": accept_encoding,
        "Connection": "close",
        "X-Platform-SSR-Trace": "1",
        "X-Request-ID": request_id,
    }
    if cookie:
        headers["Cookie"] = cookie
    if host_header:
        headers["Host"] = host_header
    connection = _connection(parsed, timeout_seconds)
    started_at = time.perf_counter()
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        ttfb_ms = (time.perf_counter() - started_at) * 1_000
        body = response.read(MAX_RESPONSE_BYTES + 1)
        total_ms = (time.perf_counter() - started_at) * 1_000
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError(f"Response from {name!r} exceeded the bounded body limit.")
        selected_headers = {
            header: response.headers.get(header, "")
            for header in SAFE_RESPONSE_HEADERS
            if response.headers.get(header) is not None
        }
        return {
            "name": name,
            "status": response.status,
            "reason": response.reason,
            "ttfb_ms": round(ttfb_ms, 3),
            "total_ms": round(total_ms, 3),
            "body_bytes": len(body),
            "headers": selected_headers,
        }
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hop",
        action="append",
        required=True,
        metavar="NAME=URL",
        help="Repeat for the direct Next, local Nginx and public Cloudflare hops.",
    )
    parser.add_argument("--cookie-file", type=Path)
    parser.add_argument("--host-header")
    parser.add_argument("--request-id", default="ttfb-probe")
    parser.add_argument("--accept-encoding", default="gzip, br")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.timeout_seconds <= 0 or args.timeout_seconds > 120:
        raise ValueError("--timeout-seconds must be between 0 and 120.")
    cookie = read_cookie_file(args.cookie_file) if args.cookie_file else None
    hops = [parse_hop(value) for value in args.hop]
    results = [
        measure_hop(
            name,
            url,
            cookie=cookie,
            host_header=args.host_header,
            request_id=args.request_id,
            accept_encoding=args.accept_encoding,
            timeout_seconds=args.timeout_seconds,
        )
        for name, url in hops
    ]
    print(json.dumps({"hops": results}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, http.client.HTTPException) as error:
        print(f"TTFB probe failed: {type(error).__name__}: {error}", file=os.sys.stderr)
        raise SystemExit(1)
