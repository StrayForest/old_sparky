from __future__ import annotations

from datetime import UTC, datetime
import unittest
from unittest.mock import AsyncMock, Mock, patch

from apps.platform_api.app.services import home_content_runtime


class _Response:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.text = content.decode("utf-8")

    def raise_for_status(self) -> None:
        return None


def _lockup_page(*videos: tuple[str, str, str]) -> bytes:
    items = []
    for video_id, title, published_text in videos:
        items.append(
            {
                "richItemRenderer": {
                    "content": {
                        "lockupViewModel": {
                            "contentId": video_id,
                            "rendererContext": {
                                "commandContext": {
                                    "onTap": {
                                        "watchEndpoint": {"videoId": video_id}
                                    }
                                }
                            },
                            "metadata": {
                                "lockupMetadataViewModel": {
                                    "title": {"content": title},
                                    "metadata": {
                                        "contentMetadataViewModel": {
                                            "metadataRows": [
                                                {
                                                    "metadataParts": [
                                                        {"text": {"content": "4.8K views"}},
                                                        {
                                                            "text": {
                                                                "content": published_text,
                                                                "accessibilityLabel": published_text,
                                                            }
                                                        },
                                                    ]
                                                }
                                            ]
                                        }
                                    },
                                }
                            },
                        }
                    }
                }
            }
        )
    import json

    return (
        "<script>var ytInitialData="
        + json.dumps({"contents": items}, ensure_ascii=False, separators=(",", ":"))
        + ";</script>"
    ).encode("utf-8")


class PlatformHomeContentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_video_renderer_parser_preserves_channel_order(self) -> None:
        page = """<script>var ytInitialData={"contents":[
          {"videoRenderer":{"videoId":"fresh123456","title":{"runs":[{"text":"Fresh video"}]},"publishedTimeText":{"simpleText":"2 hours ago"}}},
          {"videoRenderer":{"videoId":"older123456","title":{"simpleText":"Older video"},"publishedTimeText":{"simpleText":"1 day ago"}}}
        ]};</script>"""

        videos = home_content_runtime._video_renderer_videos(
            page,
            now=datetime(2026, 8, 18, 9, 0, tzinfo=UTC),
        )

        self.assertEqual(
            [video["id"] for video in videos],
            ["fresh123456", "older123456"],
        )
        self.assertEqual(videos[0]["title"], "Fresh video")
        self.assertEqual(videos[0]["published_at"], "2026-08-18T07:00:00+00:00")

    def test_lockup_view_model_parser_matches_current_youtube_shape(self) -> None:
        page = _lockup_page(
            ("fresh123456", "You’ve Got Good Aim", "14 hours ago"),
            ("older123456", "Older video", "2 days ago"),
        ).decode("utf-8")

        videos = home_content_runtime._video_renderer_videos(
            page,
            now=datetime(2026, 8, 18, 9, 0, tzinfo=UTC),
        )

        self.assertEqual(
            [video["id"] for video in videos],
            ["fresh123456", "older123456"],
        )
        self.assertEqual(videos[0]["title"], "You’ve Got Good Aim")
        self.assertEqual(videos[0]["published_at"], "2026-08-17T19:00:00+00:00")

    async def test_structured_videos_tab_works_when_atom_feed_is_down(self) -> None:
        page = _lockup_page(("fresh123456", "Fresh video", "1 hour ago"))
        client = Mock()
        client.get = AsyncMock(
            side_effect=[
                RuntimeError("feed blocked"),
                _Response(page),
            ]
        )

        videos = await home_content_runtime.fetch_youtube_videos(client)

        self.assertEqual([video["id"] for video in videos], ["fresh123456"])
        self.assertEqual(videos[0]["title"], "Fresh video")

    async def test_atom_feed_enriches_structured_video_with_original_metadata(self) -> None:
        feed = b"""<feed xmlns='http://www.w3.org/2005/Atom'
              xmlns:yt='http://www.youtube.com/xml/schemas/2015'>
          <entry>
            <yt:videoId>fresh123456</yt:videoId>
            <title>Original Russian title</title>
            <published>2026-08-17T17:01:32Z</published>
          </entry>
        </feed>"""
        page = _lockup_page(
            ("fresh123456", "Translated English title", "14 hours ago")
        )
        client = Mock()
        client.get = AsyncMock(side_effect=[_Response(feed), _Response(page)])

        videos = await home_content_runtime.fetch_youtube_videos(client)

        self.assertEqual(videos[0]["id"], "fresh123456")
        self.assertEqual(videos[0]["title"], "Original Russian title")
        self.assertEqual(videos[0]["published_at"], "2026-08-17T17:01:32+00:00")

    async def test_parser_normalizes_youtube_thumbnail_to_csp_safe_host(self) -> None:
        feed = b"""<feed xmlns='http://www.w3.org/2005/Atom'
              xmlns:yt='http://www.youtube.com/xml/schemas/2015'
              xmlns:media='http://search.yahoo.com/mrss/'>
          <entry>
            <yt:videoId>video123456</yt:videoId>
            <title>Fresh video</title>
            <published>2026-08-18T04:00:00Z</published>
            <media:group><media:thumbnail url='https://i.ytimg.com/vi/video123456/hqdefault.jpg'/></media:group>
          </entry>
        </feed>"""
        client = Mock()
        client.get = AsyncMock(
            side_effect=[
                _Response(feed),
                _Response(b'<script>{"videoId":"video123456"}</script>'),
            ]
        )

        videos = await home_content_runtime.fetch_youtube_videos(client)

        self.assertEqual([video["id"] for video in videos], ["video123456"])
        self.assertEqual(
            videos[0]["thumbnail_url"],
            "https://i3.ytimg.com/vi/video123456/hqdefault.jpg",
        )

    async def test_parser_falls_back_to_atom_feed_when_videos_tab_shape_changes(self) -> None:
        feed = b"""<feed xmlns='http://www.w3.org/2005/Atom'
              xmlns:yt='http://www.youtube.com/xml/schemas/2015'>
          <entry>
            <yt:videoId>video123456</yt:videoId>
            <title>Fresh video</title>
            <published>2026-08-18T04:00:00Z</published>
          </entry>
        </feed>"""
        client = Mock()
        client.get = AsyncMock(
            side_effect=[
                _Response(feed),
                _Response(b"<html>YouTube changed this page</html>"),
            ]
        )

        videos = await home_content_runtime.fetch_youtube_videos(client)

        self.assertEqual([video["id"] for video in videos], ["video123456"])

    async def test_parser_keeps_regular_video_filter_when_videos_tab_is_current(self) -> None:
        feed = b"""<feed xmlns='http://www.w3.org/2005/Atom'
              xmlns:yt='http://www.youtube.com/xml/schemas/2015'>
          <entry>
            <yt:videoId>video123456</yt:videoId>
            <title>Regular video</title>
            <published>2026-08-18T05:00:00Z</published>
          </entry>
          <entry>
            <yt:videoId>short123456</yt:videoId>
            <title>Older short</title>
            <published>2026-08-18T04:00:00Z</published>
          </entry>
        </feed>"""
        client = Mock()
        client.get = AsyncMock(
            side_effect=[
                _Response(feed),
                _Response(b'<script>{"videoId":"video123456"}</script>'),
            ]
        )

        videos = await home_content_runtime.fetch_youtube_videos(client)

        self.assertEqual([video["id"] for video in videos], ["video123456"])

    async def test_parser_keeps_newer_feed_entry_when_videos_tab_is_behind(self) -> None:
        feed = b"""<feed xmlns='http://www.w3.org/2005/Atom'
              xmlns:yt='http://www.youtube.com/xml/schemas/2015'>
          <entry>
            <yt:videoId>fresh123456</yt:videoId>
            <title>Just published</title>
            <published>2026-08-18T06:00:00Z</published>
          </entry>
          <entry>
            <yt:videoId>video123456</yt:videoId>
            <title>Previous regular video</title>
            <published>2026-08-18T04:00:00Z</published>
          </entry>
        </feed>"""
        client = Mock()
        client.get = AsyncMock(
            side_effect=[
                _Response(feed),
                _Response(b'<script>{"videoId":"video123456"}</script>'),
            ]
        )

        videos = await home_content_runtime.fetch_youtube_videos(client)

        self.assertEqual(
            [video["id"] for video in videos],
            ["fresh123456", "video123456"],
        )

    async def test_structured_page_keeps_video_missing_from_atom_feed(self) -> None:
        feed = b"""<feed xmlns='http://www.w3.org/2005/Atom'
              xmlns:yt='http://www.youtube.com/xml/schemas/2015'>
          <entry>
            <yt:videoId>video123456</yt:videoId>
            <title>Previous regular video</title>
            <published>2026-08-17T12:00:00Z</published>
          </entry>
        </feed>"""
        page = _lockup_page(
            ("fresh123456", "Newest regular video", "1 hour ago"),
            ("video123456", "Previous translated title", "1 day ago"),
        )
        client = Mock()
        client.get = AsyncMock(side_effect=[_Response(feed), _Response(page)])

        videos = await home_content_runtime.fetch_youtube_videos(client)

        self.assertEqual(
            [video["id"] for video in videos],
            ["fresh123456", "video123456"],
        )
        self.assertEqual(videos[0]["title"], "Newest regular video")
        self.assertEqual(videos[1]["title"], "Previous regular video")

    async def test_parser_uses_watch_pages_when_atom_feed_is_unavailable(self) -> None:
        client = Mock()
        client.get = AsyncMock(
            side_effect=[
                RuntimeError("feed blocked"),
                _Response(b'<script>{"videoId":"fresh123456"}</script>'),
                _Response(
                    b'<meta property="og:title" content="Fresh fallback video">'
                    b'<script>{"publishDate":"2026-08-18"}</script>'
                ),
            ]
        )

        videos = await home_content_runtime.fetch_youtube_videos(client)

        self.assertEqual([video["id"] for video in videos], ["fresh123456"])
        self.assertEqual(videos[0]["title"], "Fresh fallback video")
        self.assertEqual(videos[0]["published_at"], "2026-08-18T00:00:00+00:00")

    async def test_request_path_returns_cached_payload_without_recovery_call(self) -> None:
        cached_payload = {
            "patches": [],
            "videos": [],
            "generated_at": "2026-08-18T04:00:00+00:00",
            "patches_available": True,
            "videos_available": False,
        }
        recovery = AsyncMock(
            side_effect=AssertionError("request path must not call YouTube recovery")
        )
        with (
            patch.object(
                home_content_runtime.home_content_security,
                "refresh_home_content",
                AsyncMock(return_value=cached_payload),
            ),
            patch.object(home_content_runtime, "fetch_youtube_videos", recovery),
        ):
            payload = await home_content_runtime.refresh_home_content(force=False)

        self.assertEqual(payload, cached_payload)
        recovery.assert_not_awaited()

    async def test_refresh_preserves_stale_payload_when_recovery_also_fails(self) -> None:
        stale_payload = {
            "patches": [],
            "videos": [
                {
                    "id": "oldvideo001",
                    "title": "Older video",
                    "published_at": "2026-08-01T12:00:00+00:00",
                    "url": "https://www.youtube.com/watch?v=oldvideo001",
                    "thumbnail_url": "https://i3.ytimg.com/vi/oldvideo001/hqdefault.jpg",
                }
            ],
            "generated_at": "2026-08-18T04:00:00+00:00",
            "patches_available": True,
            "videos_available": False,
        }
        with (
            patch.object(
                home_content_runtime.home_content_security,
                "refresh_home_content",
                AsyncMock(return_value=stale_payload),
            ),
            patch.object(
                home_content_runtime,
                "fetch_youtube_videos",
                AsyncMock(side_effect=ValueError("YouTube unavailable")),
            ),
        ):
            payload = await home_content_runtime.refresh_home_content(force=True)

        self.assertEqual(payload, stale_payload)
        self.assertEqual(payload["videos"][0]["id"], "oldvideo001")


if __name__ == "__main__":
    unittest.main()
