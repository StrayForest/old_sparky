from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from celery import Celery

from apps.platform_api.app.services.home_content_runtime import refresh_home_content
from apps.platform_api.app.services.patch_translation import PATCH_TRANSLATION_TASK_NAME
from python_packages.platform_infra.config import get_settings


logger = logging.getLogger(__name__)


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


def _enqueue_patch_translations(payload: dict[str, Any]) -> int:
    settings = get_settings()
    producer = Celery(
        "deadlock_content_refresh",
        broker=settings.platform_celery_broker_url,
    )
    enqueued = 0
    seen: set[str] = set()
    try:
        for patch in payload.get("patches") or []:
            if not isinstance(patch, dict):
                continue
            patch_id = str(patch.get("id") or "").strip()
            if not patch_id.isdigit() or patch_id in seen:
                continue
            seen.add(patch_id)
            try:
                producer.send_task(
                    PATCH_TRANSLATION_TASK_NAME,
                    args=[patch_id],
                    queue="deadlock-platform",
                    expires=1800,
                )
                enqueued += 1
            except Exception:
                logger.exception(
                    "Failed to enqueue startup patch translation patch_id=%s.",
                    patch_id,
                )
    finally:
        producer.close()
    return enqueued


async def _main() -> None:
    payload = await refresh_home_content(force=True)
    translation_jobs = _enqueue_patch_translations(payload)
    summary = _summary(payload)
    summary["patch_translations_enqueued"] = translation_jobs
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
