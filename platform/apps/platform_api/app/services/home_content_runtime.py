from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from html import unescape
import json
import logging
import re
from typing import Any, Callable
from xml.etree import ElementTree

import httpx

from apps.platform_api.app.services import home_content, home_content_security
from apps.platform_api.app.services.external_content_http import (
    BoundedNoRedirectAsyncClient,
)
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.redis import redis_client


logger = logging.getLogger(__name__)
_YOUTUBE_VIDEO_ID_RE = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')
_YOUTUBE_ESCAPED_VIDEO_ID_RE = re.compile(r'\\"videoId\\":\\"([A-Za-z0-9_-]{11})\\"')
_YOUTUBE_RELATIVE_TIME_RE = re.compile(
    r"(?:(?:streamed|premiered)\s+)?(\d+)\s+"
    r"(second|minute|hour|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)
_YOUTUBE_WATCH_TITLE_RE = re.compile(
    r'<meta\s+(?:name="title"|property="og:title")\s+content="([^"]+)"',
    re.IGNORECASE,
)
_YOUTUBE_WATCH_DATE_RE = re.compile(r'"publishDate":"(\d{4}-\d{2}-\d{2})"')
_YOUTUBE_WATCH_ESCAPED_DATE_RE = re.compile(
    r'\\"publishDate\\":\\"(\d{4}-\d{2}-\d{2})\\"'
)
_YOUTUBE_WATCH_META_DATE_RE = re.compile(
    r'<meta\s+itemprop="datePublished"\s+content="(\d{4}-\d{2}-\d{2})"',
    re.IGNORECASE,
)
_YOUTUBE_BROWSER_HEADERS = {
    "Accept-Language": "en-US,en;q=0.8",
    "Cookie": "SOCS=CAI",
    "User-Agent": "Mozilla/5.0 (compatible; OldSparkyArena/1.0)",
}


def _is_video_id(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_-]{11}", value) is not None


def _regular_video_id_order(page_text: str) -> list[str]:
    matches = _YOUTUBE_VIDEO_ID_RE.findall(page_text)
    if not matches:
        matches = _YOUTUBE_ESCAPED_VIDEO_ID_RE.findall(page_text)
    return list(dict.fromkeys(matches))


def _regular_video_ids(page_text: str) -> set[str]:
    return set(_regular_video_id_order(page_text))


def _renderer_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    simple_text = value.get("simpleText")
    if isinstance(simple_text, str):
        return simple_text.strip()
    content = value.get("content")
    if isinstance(content, str):
        return content.strip()
    runs = value.get("runs")
    if not isinstance(runs, list):
        return ""
    return "".join(
        str(run.get("text") or "")
        for run in runs
        if isinstance(run, dict)
    ).strip()


def _relative_published_at(label: str, *, now: datetime) -> datetime | None:
    normalized = " ".join(label.strip().lower().split())
    if not normalized:
        return None
    if normalized in {"just now", "today"}:
        return now
    if normalized == "yesterday":
        return now - timedelta(days=1)

    match = _YOUTUBE_RELATIVE_TIME_RE.fullmatch(normalized)
    if match is not None:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        seconds_by_unit = {
            "second": 1,
            "minute": 60,
            "hour": 60 * 60,
            "day": 24 * 60 * 60,
            "week": 7 * 24 * 60 * 60,
            "month": 30 * 24 * 60 * 60,
            "year": 365 * 24 * 60 * 60,
        }
        return now - timedelta(seconds=amount * seconds_by_unit[unit])

    for format_string in ("%b %d, %Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(label.strip(), format_string)
        except ValueError:
            continue
        return parsed.replace(tzinfo=UTC)
    return None


def _json_objects_after_marker(page_text: str, marker: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    cursor = 0
    objects: list[dict[str, Any]] = []
    while True:
        marker_index = page_text.find(marker, cursor)
        if marker_index < 0:
            break
        object_start = page_text.find("{", marker_index + len(marker))
        if object_start < 0:
            break
        try:
            value, consumed = decoder.raw_decode(page_text[object_start:])
        except json.JSONDecodeError:
            cursor = marker_index + len(marker)
            continue
        cursor = object_start + consumed
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _first_nested_video_id(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("contentId", "videoId"):
            candidate = value.get(key)
            if isinstance(candidate, str) and _is_video_id(candidate.strip()):
                return candidate.strip()
        for nested in value.values():
            candidate = _first_nested_video_id(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for nested in value:
            candidate = _first_nested_video_id(nested)
            if candidate:
                return candidate
    return ""


def _nested_text_candidates(value: Any) -> list[str]:
    candidates: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"content", "simpleText", "accessibilityLabel"} and isinstance(nested, str):
                text = nested.strip()
                if text:
                    candidates.append(text)
            candidates.extend(_nested_text_candidates(nested))
    elif isinstance(value, list):
        for nested in value:
            candidates.extend(_nested_text_candidates(nested))
    return candidates


def _video_from_video_renderer(
    renderer: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any] | None:
    video_id = str(renderer.get("videoId") or "").strip()
    if not _is_video_id(video_id):
        return None
    title = _renderer_text(renderer.get("title"))
    published_label = _renderer_text(renderer.get("publishedTimeText"))
    published = _relative_published_at(published_label, now=now)
    if not title or published is None:
        return None
    return {
        "id": video_id,
        "title": title[:180],
        "published_at": published.isoformat(),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail_url": f"https://i3.ytimg.com/vi/{video_id}/hqdefault.jpg",
    }


def _video_from_lockup_view_model(
    lockup: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any] | None:
    video_id = _first_nested_video_id(lockup)
    if not video_id:
        return None

    metadata = lockup.get("metadata")
    lockup_metadata = (
        metadata.get("lockupMetadataViewModel")
        if isinstance(metadata, dict)
        else None
    )
    if not isinstance(lockup_metadata, dict):
        return None

    title = _renderer_text(lockup_metadata.get("title"))
    if not title:
        return None

    published: datetime | None = None
    for candidate in _nested_text_candidates(lockup_metadata.get("metadata")):
        published = _relative_published_at(candidate, now=now)
        if published is not None:
            break
    if published is None:
        return None

    return {
        "id": video_id,
        "title": title[:180],
        "published_at": published.isoformat(),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail_url": f"https://i3.ytimg.com/vi/{video_id}/hqdefault.jpg",
    }


def _videos_from_structured_objects(
    objects: list[dict[str, Any]],
    parser: Callable[..., dict[str, Any] | None],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    videos: list[dict[str, Any]] = []
    for value in objects:
        video = parser(value, now=now)
        if video is None or video["id"] in seen:
            continue
        seen.add(video["id"])
        videos.append(video)
        if len(videos) >= home_content.HOME_VIDEO_LIMIT:
            break
    return videos


def _video_renderer_videos(
    page_text: str,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    current_time = now or datetime.now(UTC)

    legacy_videos = _videos_from_structured_objects(
        _json_objects_after_marker(page_text, '"videoRenderer":'),
        _video_from_video_renderer,
        now=current_time,
    )
    if legacy_videos:
        return legacy_videos

    return _videos_from_structured_objects(
        _json_objects_after_marker(page_text, '"lockupViewModel":'),
        _video_from_lockup_view_model,
        now=current_time,
    )


def _video_published_at(video: dict[str, Any]) -> datetime:
    return home_content._source_datetime(video["published_at"])


def _prefer_regular_videos(
    feed_videos: list[dict[str, Any]],
    regular_video_ids: set[str] | None,
) -> list[dict[str, Any]]:
    if not regular_video_ids:
        return feed_videos[: home_content.HOME_VIDEO_LIMIT]

    regular_videos = [
        video for video in feed_videos if video["id"] in regular_video_ids
    ]
    if not regular_videos:
        logger.warning("youtube_videos_tab_no_feed_matches fallback=atom-feed")
        return feed_videos[: home_content.HOME_VIDEO_LIMIT]

    newest_regular_at = max(_video_published_at(video) for video in regular_videos)
    newer_unclassified = [
        video
        for video in feed_videos
        if video["id"] not in regular_video_ids
        and _video_published_at(video) > newest_regular_at
    ]
    if newer_unclassified:
        logger.warning(
            "youtube_videos_tab_behind_feed newer_unclassified=%s fallback=merge",
            len(newer_unclassified),
        )

    merged_by_id = {
        video["id"]: video
        for video in [*regular_videos, *newer_unclassified]
    }
    return sorted(
        merged_by_id.values(),
        key=_video_published_at,
        reverse=True,
    )[: home_content.HOME_VIDEO_LIMIT]


def _watch_page_video(video_id: str, page_text: str) -> dict[str, Any] | None:
    title_match = _YOUTUBE_WATCH_TITLE_RE.search(page_text)
    date_match = (
        _YOUTUBE_WATCH_DATE_RE.search(page_text)
        or _YOUTUBE_WATCH_ESCAPED_DATE_RE.search(page_text)
        or _YOUTUBE_WATCH_META_DATE_RE.search(page_text)
    )
    if title_match is None or date_match is None:
        return None
    try:
        published = datetime.fromisoformat(date_match.group(1)).replace(tzinfo=UTC)
    except ValueError:
        return None
    title = unescape(title_match.group(1)).strip()
    if not title:
        return None
    return {
        "id": video_id,
        "title": title[:180],
        "published_at": published.isoformat(),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail_url": f"https://i3.ytimg.com/vi/{video_id}/hqdefault.jpg",
    }


async def _fetch_videos_from_watch_pages(
    client: httpx.AsyncClient,
    video_ids: list[str],
) -> list[dict[str, Any]]:
    selected_ids = video_ids[: home_content.HOME_VIDEO_LIMIT]
    if not selected_ids:
        raise ValueError("YouTube videos tab did not expose regular video identifiers.")

    responses = await asyncio.gather(
        *(
            client.get(
                f"https://www.youtube.com/watch?v={video_id}",
                headers=_YOUTUBE_BROWSER_HEADERS,
            )
            for video_id in selected_ids
        ),
        return_exceptions=True,
    )
    videos: list[dict[str, Any]] = []
    for video_id, response in zip(selected_ids, responses, strict=True):
        if isinstance(response, Exception):
            logger.warning(
                "youtube_watch_unavailable video_id=%s error=%s detail=%s",
                video_id,
                type(response).__name__,
                str(response)[:240],
            )
            continue
        try:
            response.raise_for_status()
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "youtube_watch_unavailable video_id=%s error=%s detail=%s",
                video_id,
                type(error).__name__,
                str(error)[:240],
            )
            continue
        video = _watch_page_video(video_id, response.text)
        if video is not None:
            videos.append(video)

    if not videos:
        raise ValueError("YouTube watch pages did not contain usable video metadata.")
    return videos


def _parse_atom_videos(feed_content: bytes) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(feed_content)
    atom = "{http://www.w3.org/2005/Atom}"
    yt = "{http://www.youtube.com/xml/schemas/2015}"
    videos_by_id: dict[str, dict[str, Any]] = {}
    for entry in root.findall(f"{atom}entry"):
        video_id = (entry.findtext(f"{yt}videoId") or "").strip()
        title = (entry.findtext(f"{atom}title") or "").strip()
        published_at = (entry.findtext(f"{atom}published") or "").strip()
        if (
            not _is_video_id(video_id)
            or video_id in videos_by_id
            or not title
            or not published_at
        ):
            continue
        published = home_content._source_datetime(published_at)
        videos_by_id[video_id] = {
            "id": video_id,
            "title": title[:180],
            "published_at": published.isoformat(),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail_url": f"https://i3.ytimg.com/vi/{video_id}/hqdefault.jpg",
        }
    return sorted(
        videos_by_id.values(),
        key=_video_published_at,
        reverse=True,
    )


def _enrich_structured_with_atom(
    structured_videos: list[dict[str, Any]],
    atom_videos: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    atom_by_id = {video["id"]: video for video in atom_videos}
    return [
        atom_by_id.get(video["id"], video)
        for video in structured_videos
    ][: home_content.HOME_VIDEO_LIMIT]


async def fetch_youtube_videos(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    feed_headers = {
        **_YOUTUBE_BROWSER_HEADERS,
        "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
    }
    feed_result, videos_result = await asyncio.gather(
        client.get(home_content.YOUTUBE_FEED_URL, headers=feed_headers),
        client.get(
            home_content.YOUTUBE_VIDEOS_URL,
            headers=_YOUTUBE_BROWSER_HEADERS,
        ),
        return_exceptions=True,
    )

    structured_videos: list[dict[str, Any]] = []
    regular_video_order: list[str] = []
    regular_video_ids: set[str] | None = None
    if isinstance(videos_result, Exception):
        logger.warning(
            "youtube_videos_tab_unavailable error=%s detail=%s fallback=atom-feed",
            type(videos_result).__name__,
            str(videos_result)[:240],
        )
    else:
        try:
            videos_result.raise_for_status()
            structured_videos = _video_renderer_videos(videos_result.text)
            if structured_videos:
                regular_video_order = [video["id"] for video in structured_videos]
                regular_video_ids = set(regular_video_order)
                logger.info(
                    "youtube_videos_tab_structured videos=%s latest_video_id=%s",
                    len(structured_videos),
                    structured_videos[0]["id"],
                )
            else:
                regular_video_order = _regular_video_id_order(videos_result.text)
                if regular_video_order:
                    regular_video_ids = set(regular_video_order)
                    logger.warning(
                        "youtube_videos_tab_structured_unavailable ids=%s fallback=atom-feed",
                        len(regular_video_order),
                    )
                else:
                    logger.warning("youtube_videos_tab_no_ids fallback=atom-feed")
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "youtube_videos_tab_unavailable error=%s detail=%s fallback=atom-feed",
                type(error).__name__,
                str(error)[:240],
            )

    if isinstance(feed_result, Exception):
        if structured_videos:
            logger.warning(
                "youtube_atom_feed_unavailable error=%s detail=%s fallback=videos-tab",
                type(feed_result).__name__,
                str(feed_result)[:240],
            )
            return structured_videos
        logger.warning(
            "youtube_atom_feed_unavailable error=%s detail=%s fallback=watch-pages",
            type(feed_result).__name__,
            str(feed_result)[:240],
        )
        return await _fetch_videos_from_watch_pages(client, regular_video_order)

    try:
        feed_result.raise_for_status()
        atom_videos = _parse_atom_videos(feed_result.content)
    except Exception as error:  # noqa: BLE001
        if structured_videos:
            logger.warning(
                "youtube_atom_feed_unavailable error=%s detail=%s fallback=videos-tab",
                type(error).__name__,
                str(error)[:240],
            )
            return structured_videos
        logger.warning(
            "youtube_atom_feed_unavailable error=%s detail=%s fallback=watch-pages",
            type(error).__name__,
            str(error)[:240],
        )
        return await _fetch_videos_from_watch_pages(client, regular_video_order)

    if structured_videos:
        return _enrich_structured_with_atom(structured_videos, atom_videos)

    if not atom_videos:
        logger.warning("youtube_atom_feed_empty fallback=watch-pages")
        return await _fetch_videos_from_watch_pages(client, regular_video_order)

    return _prefer_regular_videos(atom_videos, regular_video_ids)


async def _publish_recovered_payload(payload: dict[str, Any]) -> None:
    settings = get_settings()
    cache = redis_client()
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        async with cache.pipeline(transaction=True) as pipeline:
            pipeline.set(
                home_content.HOME_CONTENT_KEY,
                encoded,
                ex=settings.platform_home_content_cache_seconds,
            )
            pipeline.set(
                home_content.HOME_CONTENT_STALE_KEY,
                encoded,
                ex=settings.platform_home_content_stale_seconds,
            )
            await pipeline.execute()
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "home_content_youtube_recovery_cache_write_failed error=%s detail=%s",
            type(error).__name__,
            str(error)[:240],
        )
    finally:
        await cache.aclose()


async def refresh_home_content(*, force: bool = False) -> dict[str, Any]:
    payload = await home_content_security.refresh_home_content(force=force)
    if not force:
        return payload

    settings = get_settings()
    timeout = httpx.Timeout(settings.platform_external_content_timeout_seconds)
    try:
        async with BoundedNoRedirectAsyncClient(timeout=timeout) as client:
            videos = await fetch_youtube_videos(client)
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "home_content_youtube_recovery_failed error=%s detail=%s stale_preserved=%s",
            type(error).__name__,
            str(error)[:240],
            bool(payload.get("videos")),
        )
        return payload

    if videos == list(payload.get("videos") or []) and bool(payload.get("videos_available")):
        return payload

    recovered = {
        **payload,
        "videos": videos,
        "videos_available": True,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    await _publish_recovered_payload(recovered)
    logger.info(
        "home_content_youtube_recovered videos=%s latest_video_id=%s",
        len(videos),
        videos[0]["id"],
    )
    return recovered
