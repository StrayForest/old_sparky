#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import sys
import urllib.parse
import urllib.request


ALLOWED_CACHE_STATUSES = {"HIT", "REVALIDATED", "UPDATING"}


@dataclass(frozen=True)
class CdnResponse:
    status: int
    cache_status: str
    age: str | None
    cache_control: str
    content_type: str
    content_length: int
    etag: str | None
    cf_ray: str | None
    sha256: str
    has_set_cookie: bool
    server: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GET one immutable R2 custom-domain object twice and validate CDN behavior."
    )
    parser.add_argument("url", help="An immutable https://cdn.old-sparky.com object URL.")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--expected-host", default="cdn.old-sparky.com")
    parser.add_argument("--expected-sha256", default=None)
    parser.add_argument(
        "--allow-second-miss",
        action="store_true",
        help="Diagnostic only: do not fail when the second response is not an edge HIT.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def validate_url(url: str, expected_host: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != expected_host:
        raise ValueError(f"URL must use https://{expected_host}.")
    if parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError("URL must not contain credentials or a non-standard port.")
    if not parsed.path.startswith("/") or parsed.path.startswith("/api/"):
        raise ValueError("URL must be a direct CDN object path, not an application route.")
    if parsed.query or parsed.fragment:
        raise ValueError("Immutable CDN verification URLs must not use a query string or fragment.")


def fetch(url: str, timeout: float) -> CdnResponse:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "OldSparky-CDN-Check/1", "Accept": "image/webp,image/*"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - validated HTTPS URL
        body = response.read(8 * 1024 * 1024)
        if response.read(1):
            raise RuntimeError("CDN object exceeds the 8 MiB verification bound.")
        headers = response.headers
        declared_length = headers.get("Content-Length")
        if declared_length is not None and int(declared_length) != len(body):
            raise RuntimeError("Content-Length does not match the downloaded object size.")
        return CdnResponse(
            status=int(response.status),
            cache_status=(headers.get("CF-Cache-Status") or "").upper(),
            age=headers.get("Age"),
            cache_control=headers.get("Cache-Control") or "",
            content_type=(headers.get_content_type() or "").lower(),
            content_length=len(body),
            etag=headers.get("ETag"),
            cf_ray=headers.get("CF-Ray"),
            sha256=hashlib.sha256(body).hexdigest(),
            has_set_cookie=headers.get("Set-Cookie") is not None,
            server=headers.get("Server"),
        )


def validate_response(response: CdnResponse, *, expected_sha256: str | None = None) -> None:
    if response.status != 200:
        raise RuntimeError(f"CDN returned HTTP {response.status}; expected 200.")
    if not response.content_type.startswith("image/"):
        raise RuntimeError(f"CDN Content-Type is not an image: {response.content_type!r}.")
    directives = {part.strip().lower() for part in response.cache_control.split(",") if part.strip()}
    if "public" not in directives or {"private", "no-store"} & directives:
        raise RuntimeError(f"Unsafe or non-public Cache-Control: {response.cache_control!r}.")
    if response.has_set_cookie:
        raise RuntimeError("CDN object response unexpectedly sets a cookie.")
    if not response.cf_ray:
        raise RuntimeError("CDN response is missing CF-Ray.")
    if (response.server or "").lower() != "cloudflare":
        raise RuntimeError("Object response did not traverse the Cloudflare custom domain.")
    if response.content_length <= 0:
        raise RuntimeError("CDN object is empty.")
    if expected_sha256 and response.sha256.lower() != expected_sha256.lower():
        raise RuntimeError("CDN object SHA-256 does not match the expected value.")


def main() -> int:
    args = parse_args()
    if args.timeout <= 0 or args.timeout > 60:
        raise ValueError("--timeout must be greater than zero and at most 60 seconds.")
    validate_url(args.url, args.expected_host)
    first = fetch(args.url, args.timeout)
    validate_response(first, expected_sha256=args.expected_sha256)
    second = fetch(args.url, args.timeout)
    validate_response(second, expected_sha256=args.expected_sha256 or first.sha256)
    if first.sha256 != second.sha256:
        raise RuntimeError("Repeated CDN GETs returned different object bytes.")
    if not args.allow_second_miss and second.cache_status not in ALLOWED_CACHE_STATUSES:
        raise RuntimeError(
            f"Second CDN GET was {second.cache_status or 'missing CF-Cache-Status'}, expected a cache hit."
        )

    result = {"ok": True, "url": args.url, "first": asdict(first), "second": asdict(second)}
    if args.as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            "CDN check passed: "
            f"first={first.cache_status or 'UNKNOWN'}, second={second.cache_status or 'UNKNOWN'}, "
            f"bytes={second.content_length}, sha256={second.sha256}."
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"CDN check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
