#!/usr/bin/env python3
"""Publish one bounded, non-authoritative SSE recovery probe event."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sys
import time

from redis.asyncio import Redis, from_url

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from apps.platform_api.app.services.bracket_events import bracket_channel
from python_packages.platform_infra.config import get_settings
from tools.platform_production_qa import load_env_file


UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--tournament-id", required=True)
    parser.add_argument("--revision", type=int, required=True)
    args = parser.parse_args()
    if not UUID_PATTERN.fullmatch(args.tournament_id):
        parser.error("--tournament-id must be a UUID")
    if args.revision < 1:
        parser.error("--revision must be positive")
    return args


async def publish(args: argparse.Namespace) -> int:
    load_env_file(args.env_file)
    settings = get_settings()
    published_at_ms = int(time.time() * 1000)
    payload = json.dumps(
        {
            "type": "qa_sse_recovery_probe",
            "revision": args.revision,
            "qa_published_at_ms": published_at_ms,
        },
        separators=(",", ":"),
    )
    client: Redis = from_url(settings.platform_redis_url, decode_responses=True)
    try:
        subscribers = int(await client.publish(bracket_channel(args.tournament_id), payload))
    finally:
        await client.aclose()
    print(
        json.dumps(
            {
                "published": True,
                "subscribers": subscribers,
                "published_at": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(publish(parse_args())))
