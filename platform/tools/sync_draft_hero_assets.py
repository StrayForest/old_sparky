#!/usr/bin/env python3
from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path
import re

from PIL import Image

from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.object_storage import ObjectStorage


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PLATFORM_ROOT / "apps" / "platform_web" / "public" / "assets" / "heroes"
KEY_PREFIX = "draft/heroes/v1"


def hero_slug(path: Path) -> str:
    slug = path.stem.casefold().replace("&", "and").replace("_", "-").replace(" ", "-")
    slug = re.sub(r"[^a-z0-9-]+", "-", slug)
    return re.sub(r"-+", "-", slug).strip("-")


def encode_webp(path: Path, max_width: int, max_height: int, quality: int) -> bytes:
    with Image.open(path) as image:
        image.load()
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, "WEBP", quality=quality, method=6)
        return output.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate compact immutable Deadlock hero thumbnails and upload them to the existing R2/CDN bucket."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-width", type=int, default=256)
    parser.add_argument("--max-height", type=int, default=320)
    parser.add_argument("--quality", type=int, default=80)
    args = parser.parse_args()

    if not (64 <= args.max_width <= 1024 and 64 <= args.max_height <= 1024):
        parser.error("thumbnail bounds must be between 64 and 1024 pixels")
    if not 50 <= args.quality <= 95:
        parser.error("quality must be between 50 and 95")

    sources = sorted(path for path in SOURCE_DIR.glob("*.png") if path.is_file())
    if not sources:
        raise SystemExit(f"No hero PNGs found under {SOURCE_DIR}")

    settings = get_settings()
    if settings.platform_object_storage_backend.strip().lower() != "r2" and not args.dry_run:
        raise SystemExit("Refusing upload: PLATFORM_OBJECT_STORAGE_BACKEND is not r2")
    storage = ObjectStorage(settings) if not args.dry_run else None

    total_source = 0
    total_output = 0
    seen: set[str] = set()
    for source in sources:
        slug = hero_slug(source)
        if not slug or slug in seen:
            raise SystemExit(f"Duplicate/invalid hero slug for {source.name}: {slug!r}")
        seen.add(slug)
        payload = encode_webp(source, args.max_width, args.max_height, args.quality)
        key = f"{KEY_PREFIX}/{slug}.webp"
        source_bytes = source.stat().st_size
        total_source += source_bytes
        total_output += len(payload)
        reduction = 100.0 * (1.0 - len(payload) / source_bytes) if source_bytes else 0.0
        print(f"{source.name:24} -> {key:42} {source_bytes:7d} -> {len(payload):7d} bytes ({reduction:5.1f}% smaller)")
        if storage is not None:
            storage.put(key, payload, "image/webp")

    print(
        f"heroes={len(sources)} source={total_source} output={total_output} "
        f"reduction={(100.0 * (1.0 - total_output / total_source) if total_source else 0.0):.1f}%"
    )
    if settings.platform_media_public_base_url:
        print(f"public base: {settings.platform_media_public_base_url.rstrip('/')}/{KEY_PREFIX}/")
    if args.dry_run:
        print("dry-run: no R2 objects were written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
