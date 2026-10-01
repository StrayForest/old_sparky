"""Refresh public home/patch distribution without translation side effects."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from apps.platform_api.app.services import home_content_security


def _summary(payload: dict[str, Any]) -> dict[str, Any]:
    patches = payload.get("patches")
    videos = payload.get("videos")
    return {
        "patches_available": payload.get("patches_available") is True,
        "patches_count": len(patches) if isinstance(patches, list) else 0,
        "videos_available": payload.get("videos_available") is True,
        "videos_count": len(videos) if isinstance(videos, list) else 0,
    }


async def _main() -> None:
    payload = await home_content_security.refresh_content_distribution(force=True)
    if not isinstance(payload, dict) or payload.get("patches_available") is not True:
        raise RuntimeError("translation-free content distribution refresh failed")
    print(
        "CONTENT_DISTRIBUTION_REFRESH "
        + json.dumps(_summary(payload), ensure_ascii=True, separators=(",", ":"))
    )


if __name__ == "__main__":
    asyncio.run(_main())
