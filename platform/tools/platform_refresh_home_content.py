from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from apps.platform_api.app.services.home_content_runtime import refresh_home_content


def _summary(payload: dict[str, Any]) -> dict[str, Any]:
    videos = list(payload.get("videos") or [])
    latest = videos[0] if videos else {}
    return {
        "patches_count": len(payload.get("patches") or []),
        "videos_available": bool(payload.get("videos_available")),
        "videos_count": len(videos),
        "latest_video_id": latest.get("id"),
        "latest_video_title": latest.get("title"),
        "latest_video_published_at": latest.get("published_at"),
        "generated_at": payload.get("generated_at"),
    }


async def _main() -> None:
    payload = await refresh_home_content(force=True)
    summary = _summary(payload)
    print(
        "HOME_CONTENT_REFRESH "
        + json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_main())
