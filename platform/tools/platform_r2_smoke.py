#!/usr/bin/env python3
from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
import urllib.parse
from uuid import uuid4

from botocore.config import Config

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from python_packages.platform_infra.config import get_settings
from tools.platform_check_cdn import (
    ALLOWED_CACHE_STATUSES,
    fetch as fetch_cdn,
    validate_response as validate_cdn_response,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check R2 connectivity without printing credentials. With --apply, upload one "
            "short-lived prepared WebP object, verify it through the CDN, and delete it."
        )
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--cdn-attempts", type=int, default=6)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def load_env_file(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("R2 smoke env file must be a regular file.")
    if stat.S_IMODE(path.stat().st_mode) & 0o007:
        raise PermissionError("R2 smoke env file must not be accessible to other users.")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'\"")


def require(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise RuntimeError(f"Required setting {name} is not configured.")
    return value.strip()


def prepared_webp() -> bytes:
    from PIL import Image

    buffer = BytesIO()
    image = Image.new("RGB", (2, 2), (38, 58, 74))
    image.save(buffer, format="WEBP", quality=80, method=4, exif=b"")
    return buffer.getvalue()


def main() -> int:
    args = parse_args()
    configured_env_file = args.env_file or (
        Path(os.environ["PLATFORM_ENV_FILE"])
        if os.environ.get("PLATFORM_ENV_FILE")
        else None
    )
    if configured_env_file is not None:
        load_env_file(configured_env_file)
    if args.timeout <= 0 or args.timeout > 60:
        raise ValueError("--timeout must be greater than zero and at most 60 seconds.")
    if args.cdn_attempts < 1 or args.cdn_attempts > 20:
        raise ValueError("--cdn-attempts must be between 1 and 20.")

    settings = get_settings()
    endpoint = require(settings.platform_r2_endpoint_url, "PLATFORM_R2_ENDPOINT_URL")
    access_key = require(settings.platform_r2_access_key_id, "PLATFORM_R2_ACCESS_KEY_ID")
    secret_key = require(settings.platform_r2_secret_access_key, "PLATFORM_R2_SECRET_ACCESS_KEY")
    bucket = require(settings.platform_r2_bucket_name, "PLATFORM_R2_BUCKET_NAME")
    cdn_base = require(
        getattr(settings, "platform_media_public_base_url", None),
        "PLATFORM_MEDIA_PUBLIC_BASE_URL",
    ).rstrip("/")
    parsed_cdn = urllib.parse.urlsplit(cdn_base)
    if parsed_cdn.scheme != "https" or parsed_cdn.hostname != "cdn.old-sparky.com":
        raise RuntimeError("PLATFORM_MEDIA_PUBLIC_BASE_URL must be https://cdn.old-sparky.com.")

    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=getattr(settings, "platform_r2_region", "auto"),
        config=Config(
            connect_timeout=min(args.timeout, 10),
            read_timeout=args.timeout,
            retries={"mode": "standard", "total_max_attempts": 3},
            signature_version="s3v4",
        ),
    )
    client.head_bucket(Bucket=bucket)

    result: dict[str, object] = {"ok": True, "mode": "apply" if args.apply else "connectivity"}
    if not args.apply:
        result["operations"] = {"class_a": 0, "class_b": 1, "delete": 0}
    else:
        content = prepared_webp()
        digest = hashlib.sha256(content).hexdigest()
        key = f"smoke/{uuid4()}/prepared.webp"
        public_url = f"{cdn_base}/{key}"
        uploaded = False
        try:
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=content,
                ContentType="image/webp",
                CacheControl="public, max-age=60, s-maxage=60",
                Metadata={"sha256": digest, "purpose": "platform-smoke"},
            )
            uploaded = True
            head = client.head_object(Bucket=bucket, Key=key)
            if head.get("Metadata", {}).get("sha256") != digest:
                raise RuntimeError("R2 HeadObject metadata SHA-256 mismatch.")

            first_cdn = None
            last_error: Exception | None = None
            for attempt in range(args.cdn_attempts):
                try:
                    first_cdn = fetch_cdn(public_url, args.timeout)
                    validate_cdn_response(first_cdn, expected_sha256=digest)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < args.cdn_attempts:
                        time.sleep(min(1 + attempt, 3))
            if last_error is not None:
                raise last_error
            if first_cdn is None:
                raise RuntimeError("CDN smoke did not receive the prepared object.")
            second_cdn = fetch_cdn(public_url, args.timeout)
            validate_cdn_response(second_cdn, expected_sha256=digest)
            if second_cdn.cache_status not in ALLOWED_CACHE_STATUSES:
                raise RuntimeError(
                    "Repeated CDN GET did not produce a cache hit; review the R2 custom-domain cache rule."
                )
            result.update(
                {
                    "sha256": digest,
                    "bytes": len(content),
                    "cache": {
                        "first": first_cdn.cache_status,
                        "second": second_cdn.cache_status,
                        "age": second_cdn.age,
                    },
                    "operations": {"class_a": 1, "class_b": 2, "delete": 1},
                }
            )
        finally:
            if uploaded:
                client.delete_object(Bucket=bucket, Key=key)

    if args.as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"R2 smoke passed: mode={result['mode']}; operations={result['operations']}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"R2 smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
