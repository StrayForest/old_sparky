from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException, Response
from pydantic import ValidationError
from starlette.requests import Request

from apps.platform_api.app.api.routes import content as content_routes
from apps.platform_api.app.api.schemas import (
    DeadlockGameAssetsResponse,
    PatchDetailResponse,
    PatchSectionResponse,
    SupportMessageRequest,
)
from apps.platform_api.app.services import home_content
from python_packages.platform_infra.config import PlatformSettings
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class _Response:
    def __init__(self, *, json_payload: object | None = None, content: bytes = b"") -> None:
        self._json_payload = json_payload or {}
        self.content = content
        self.text = content.decode("utf-8")

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._json_payload


class _Pipeline:
    def __init__(self) -> None:
        self.set_calls: list[tuple[tuple, dict]] = []
        self.executed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))
        return self

    async def execute(self) -> list[bool]:
        self.executed = True
        return [True] * len(self.set_calls)


class _Cache:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.pipeline_transactions: list[bool] = []
        self.pipeline_instance = _Pipeline()

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, *args, **kwargs) -> bool:
        return True

    def pipeline(self, *, transaction: bool):
        self.pipeline_transactions.append(transaction)
        return self.pipeline_instance

    async def eval(self, *args, **kwargs) -> int:
        return 1

    async def aclose(self) -> None:
        return None


class PlatformPublicContentTests(PlatformIsolatedAsyncioTestCase):
    async def test_game_assets_uses_etag_and_returns_not_modified_without_body(self) -> None:
        catalog = {
            "heroes": {"abrams": {"name": "Abrams"}},
            "items": {},
            "ranks": {},
            "objectives": {},
        }
        with patch.object(
            content_routes,
            "get_deadlock_asset_catalog",
            AsyncMock(return_value=catalog),
        ):
            first_response = Response()
            first = await content_routes.deadlock_game_assets(
                Request(
                    {
                        "type": "http",
                        "method": "GET",
                        "path": "/api/v1/content/game-assets",
                        "headers": [],
                    }
                ),
                first_response,
            )
            etag = first_response.headers["etag"]
            self.assertNotIsInstance(first, Response)
            self.assertTrue(etag.startswith('"'))

            not_modified = await content_routes.deadlock_game_assets(
                Request(
                    {
                        "type": "http",
                        "method": "GET",
                        "path": "/api/v1/content/game-assets",
                        "headers":[
                            (b"if-none-match", f"W/{etag}, \"other\"".encode("ascii")),
                        ],
                    }
                ),
                Response(),
            )

        self.assertIsInstance(not_modified, Response)
        self.assertEqual(not_modified.status_code, 304)
        self.assertEqual(not_modified.body, b"")
        self.assertEqual(not_modified.headers["etag"], etag)

    def test_support_rejects_whitespace_only_message_after_normalization(self) -> None:
        with self.assertRaises(ValidationError):
            SupportMessageRequest(
                name="Player",
                email="player@example.com",
                category="other",
                message="          ",
            )

    async def test_steam_parser_keeps_all_official_announcements_and_plain_text(self) -> None:
        client = Mock()
        client.get = AsyncMock(
            return_value=_Response(
                json_payload={
                    "appnews": {
                        "newsitems": [
                            {
                                "gid": "123",
                                "title": "Minor Update",
                                "contents": "<p>Balance</p><ul><li>Changed hero</li></ul>",
                                "date": 1_700_000_000,
                                "url": "https://store.steampowered.com/news/app/1422450/view/123",
                                "feedlabel": "Community Announcements",
                                "tags": ["patchnotes"],
                            },
                            {
                                "gid": "124",
                                "title": "Matchmaking Update",
                                "contents": "New ranked mode",
                                "date": 1_700_000_001,
                                "url": "https://steamstore-a.akamaihd.net/news/externalpost/steam_community_announcements/124",
                                "feedlabel": "Community Announcements",
                                "tags": [],
                            },
                            {
                                "gid": "125",
                                "title": "Third-party article",
                                "contents": "Ignored",
                                "date": 1_700_000_002,
                                "url": "https://steamstore-a.akamaihd.net/news/externalpost/PC_Gamer/125",
                                "feedlabel": "PC Gamer",
                                "tags": [],
                            },
                        ]
                    }
                }
            )
        )

        patches, details = await home_content._fetch_steam_patches(client)

        self.assertEqual([patch["id"] for patch in patches], ["124", "123"])
        self.assertEqual(details["123"]["content"], "Balance\nChanged hero")
        self.assertEqual(details["124"]["content"], "New ranked mode")
        self.assertNotIn("125", details)
        self.assertNotIn("<p>", patches[0]["excerpt"])

    async def test_home_feed_sorts_and_limits_patch_cards_to_four(self) -> None:
        client = Mock()
        client.get = AsyncMock(
            return_value=_Response(
                json_payload={
                    "appnews": {
                        "newsitems": [
                            {
                                "gid": str(1000 + index),
                                "title": f"Minor Update {index}",
                                "contents": "Balance update",
                                "date": 1_700_000_000 + index,
                                "feedlabel": "Community Announcements",
                                "tags": ["patchnotes"],
                            }
                            for index in range(7)
                        ]
                    }
                }
            )
        )

        patches, details = await home_content._fetch_steam_patches(client)

        self.assertEqual([patch["id"] for patch in patches], ["1006", "1005", "1004", "1003"])
        self.assertEqual(len(details), 4)

    def test_long_single_line_announcement_is_not_truncated(self) -> None:
        sentences = [f"Sentence {index} contains enough text for chunking." for index in range(80)]
        content = " ".join(sentences)

        structured = home_content._structure_patch_detail(
            {"content": content},
            {"heroes": {}, "items": {}},
        )

        changes = structured["sections"][0]["changes"]
        self.assertGreater(len(changes), 1)
        self.assertTrue(all(len(change) <= 1000 for change in changes))
        self.assertEqual(" ".join(changes), content)

    def test_patch_image_token_is_preserved_in_source_position(self) -> None:
        asset = "f6a6d5724077ee5ea7b3b3701f4af907c9517df4.png"
        content = (
            "RANKED MODE\n"
            f"{{STEAM_CLAN_LOC_IMAGE}}/45164767/{asset}\n"
            "UPDATED RANKS & BADGES"
        )

        detail = home_content._structure_patch_detail(
            {"content": content},
            {"heroes": {}, "items": {}},
        )

        self.assertEqual(
            detail["sections"][0]["changes"],
            [
                "RANKED MODE",
                f"https://clan.fastly.steamstatic.com/images/45164767/{asset}",
                "UPDATED RANKS & BADGES",
            ],
        )

    def test_patch_image_in_html_attribute_is_preserved(self) -> None:
        asset = "f6a6d5724077ee5ea7b3b3701f4af907c9517df4.png"

        plain = home_content._plain_text(
            f'<p>Before</p><img src="{{STEAM_CLAN_IMAGE}}/45164767/{asset}"><p>After</p>',
            max_length=30000,
        )

        self.assertIn(f"{{STEAM_CLAN_IMAGE}}/45164767/{asset}", plain)
        self.assertLess(plain.index("Before"), plain.index(asset))
        self.assertLess(plain.index(asset), plain.index("After"))

    async def test_youtube_parser_builds_safe_video_cards(self) -> None:
        feed = b"""<?xml version='1.0' encoding='UTF-8'?>
        <feed xmlns='http://www.w3.org/2005/Atom'
              xmlns:yt='http://www.youtube.com/xml/schemas/2015'
              xmlns:media='http://search.yahoo.com/mrss/'>
          <entry>
            <yt:videoId>video123456</yt:videoId>
            <title>Deadlock guide</title>
            <published>2026-07-20T12:00:00+00:00</published>
            <media:group><media:thumbnail url='https://i3.ytimg.com/vi/video123/hqdefault.jpg'/></media:group>
          </entry>
        </feed>"""
        client = Mock()
        videos_tab = b'<script>{"videoId":"video123456"}</script>'
        client.get = AsyncMock(side_effect=[_Response(content=feed), _Response(content=videos_tab)])

        videos = await home_content._fetch_youtube_videos(client)

        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["id"], "video123456")
        self.assertEqual(videos[0]["url"], "https://www.youtube.com/watch?v=video123456")
        self.assertEqual(
            client.get.await_args_list[1].kwargs["headers"]["Cookie"],
            "SOCS=CAI",
        )

    async def test_youtube_parser_excludes_feed_entries_missing_from_videos_tab(self) -> None:
        feed = b"""<feed xmlns='http://www.w3.org/2005/Atom'
              xmlns:yt='http://www.youtube.com/xml/schemas/2015'
              xmlns:media='http://search.yahoo.com/mrss/'>
          <entry><yt:videoId>short123456</yt:videoId><title>Short</title><published>2026-07-20T12:00:00Z</published></entry>
          <entry><yt:videoId>video123456</yt:videoId><title>Video</title><published>2026-07-19T12:00:00Z</published></entry>
        </feed>"""
        videos_tab = b'<script>{"videoId":"video123456"}</script>'
        client = Mock()
        client.get = AsyncMock(side_effect=[_Response(content=feed), _Response(content=videos_tab)])

        videos = await home_content._fetch_youtube_videos(client)

        self.assertEqual([video["id"] for video in videos], ["video123456"])
        self.assertEqual(
            videos[0]["thumbnail_url"],
            "https://i3.ytimg.com/vi/video123456/hqdefault.jpg",
        )

    async def test_home_feed_limits_regular_videos_to_four(self) -> None:
        entries = "".join(
            f"""
            <entry>
              <yt:videoId>video{index:06d}</yt:videoId>
              <title>Video {index}</title>
              <published>2026-07-{20 - index:02d}T12:00:00Z</published>
            </entry>
            """
            for index in range(6)
        )
        feed = f"""
        <feed xmlns='http://www.w3.org/2005/Atom'
              xmlns:yt='http://www.youtube.com/xml/schemas/2015'
              xmlns:media='http://search.yahoo.com/mrss/'>{entries}</feed>
        """.encode()
        videos_tab = "".join(
            f'<script>{{"videoId":"video{index:06d}"}}</script>'
            for index in range(6)
        ).encode()
        client = Mock()
        client.get = AsyncMock(side_effect=[_Response(content=feed), _Response(content=videos_tab)])

        videos = await home_content._fetch_youtube_videos(client)

        self.assertEqual(len(videos), 4)

    async def test_youtube_parser_rejects_untrusted_thumbnail_host(self) -> None:
        feed = b"""<feed xmlns='http://www.w3.org/2005/Atom'
              xmlns:yt='http://www.youtube.com/xml/schemas/2015'
              xmlns:media='http://search.yahoo.com/mrss/'>
          <entry>
            <yt:videoId>video123456</yt:videoId>
            <title>Video</title>
            <published>2026-07-20T12:00:00Z</published>
            <media:group><media:thumbnail url='https://example.com/untrusted.jpg'/></media:group>
          </entry>
        </feed>"""
        client = Mock()
        client.get = AsyncMock(
            side_effect=[
                _Response(content=feed),
                _Response(content=b'<script>{"videoId":"video123456"}</script>'),
            ]
        )

        with self.assertRaisesRegex(ValueError, "host is not allowed"):
            await home_content._fetch_youtube_videos(client)

    async def test_youtube_parser_rejects_non_csp_thumbnail_host(self) -> None:
        feed = b"""<feed xmlns='http://www.w3.org/2005/Atom'
              xmlns:yt='http://www.youtube.com/xml/schemas/2015'
              xmlns:media='http://search.yahoo.com/mrss/'>
          <entry>
            <yt:videoId>video123456</yt:videoId>
            <title>Video</title>
            <published>2026-07-20T12:00:00Z</published>
            <media:group><media:thumbnail url='https://i.ytimg.com/vi/video123456/hqdefault.jpg'/></media:group>
          </entry>
        </feed>"""
        client = Mock()
        client.get = AsyncMock(
            side_effect=[
                _Response(content=feed),
                _Response(content=b'<script>{"videoId":"video123456"}</script>'),
            ]
        )

        with self.assertRaisesRegex(ValueError, "host is not allowed"):
            await home_content._fetch_youtube_videos(client)

    async def test_refresh_keeps_stale_source_and_publishes_atomically(self) -> None:
        stale_patch = {
            "id": "123",
            "title": "Previous valid patch",
            "excerpt": "Valid stale content",
            "published_at": "2026-07-20T12:00:00+00:00",
            "url": "https://store.steampowered.com/news/app/1422450/view/123",
        }
        cache = _Cache(
            {
                home_content.HOME_CONTENT_STALE_KEY: json.dumps(
                    {
                        "patches": [stale_patch],
                        "videos": [],
                        "generated_at": "2026-07-20T12:00:00+00:00",
                        "patches_available": True,
                        "videos_available": True,
                    }
                )
            }
        )
        fresh_video = {
            "id": "video123456",
            "title": "Fresh video",
            "published_at": "2026-07-21T12:00:00+00:00",
            "url": "https://www.youtube.com/watch?v=video123456",
            "thumbnail_url": "https://i3.ytimg.com/vi/video123456/hqdefault.jpg",
        }
        settings = PlatformSettings(
            _env_file=None,
            platform_home_content_cache_seconds=2100,
        )
        with (
            patch.object(home_content, "redis_client", return_value=cache),
            patch.object(home_content, "get_settings", return_value=settings),
            patch.object(
                home_content,
                "_fetch_steam_patches",
                AsyncMock(side_effect=ValueError("rejected candidate")),
            ),
            patch.object(home_content, "_fetch_youtube_videos", AsyncMock(return_value=[fresh_video])),
            patch.object(home_content, "_fetch_deadlock_asset_catalog", AsyncMock(return_value={})),
        ):
            payload = await home_content.refresh_home_content(force=True)

        self.assertEqual(payload["patches"], [stale_patch])
        self.assertEqual(payload["videos"], [fresh_video])
        self.assertFalse(payload["patches_available"])
        self.assertEqual(cache.pipeline_transactions, [True])
        self.assertTrue(cache.pipeline_instance.executed)
        published_keys = [call[0][0] for call in cache.pipeline_instance.set_calls]
        self.assertIn(home_content.HOME_CONTENT_KEY, published_keys)
        self.assertIn(home_content.HOME_CONTENT_STALE_KEY, published_keys)

    def test_patch_detail_groups_hero_abilities(self) -> None:
        detail = home_content._structure_patch_detail(
            {
                "id": "123",
                "title": "Patch",
                "published_at": "2026-07-20T12:00:00Z",
                "url": "https://example.com/patch",
                "content": "- Map speed reduced- Haze: Sleep Dagger cooldown reduced- Haze: Bullet damage increased",
            },
            {
                "haze": {
                    "name": "Haze",
                    "slug": "haze",
                    "abilities": {
                        "sleep dagger": {"name": "Sleep Dagger", "icon_url": "https://example.com/icon.png"}
                    },
                }
            },
        )

        self.assertEqual(detail["sections"][0]["kind"], "general")
        self.assertEqual(detail["sections"][1]["hero_name"], "Haze")
        self.assertEqual(detail["sections"][1]["abilities"][0]["name"], "Sleep Dagger")
        self.assertEqual(detail["sections"][1]["abilities"][0]["changes"], ["Cooldown reduced"])
        self.assertEqual(detail["sections"][1]["changes"], ["Bullet damage increased"])

    def test_patch_detail_groups_live_urn_and_rift_scope_in_fixed_order(self) -> None:
        urn_icon = (
            "https://assets-bucket.deadlock-api.com/"
            "assets-api-res/icons/minimap/soul_jar_marker_psd.png"
        )
        rift_icon = (
            "https://assets-bucket.deadlock-api.com/"
            "assets-api-res/icons/minimap/minimap_icon_koth.png"
        )
        detail = home_content._structure_patch_detail(
            {
                "id": "objectives",
                "title": "Patch",
                "published_at": "2026-08-07T12:00:00Z",
                "url": "https://example.com/patch",
                "content": "\n".join(
                    (
                        "[ General ]",
                        "- Map lane timing adjusted",
                        "[ Urn / King of the Hill ]",
                        "- Urn Runner sprint bonus reduced from +2m to 0",
                        "- Urn runner move speed bonus reduced from +3.5m to +2m",
                        "- Urn Runner Stamina Recovery increased from +15% to +25%",
                        "- Urn talking frequency increased from every 8s to every 6s",
                        "- Urn talking sound distance increased near a nearby Urn Runner",
                        "- Unstable Rift warning time reduced from 25s to 20s",
                        "- Rift Troopers now have Spirit Resist",
                        "- Rift Troopers now have Melee Resistance",
                        "- Rift Troopers spawn interval increased",
                        "- Unstable Rift comeback resist aura radius increased",
                        "- Rift Troopers max comeback count increased from 12 to 14",
                        "- Objectives bounty split for nearby heroes reduced",
                        "- Urn and Unstable Rift rewards are unchanged",
                        "[ Items ]",
                        "- Spirit Burn: Damage reduced",
                        "[ Heroes ]",
                        "- Haze: Bullet damage increased",
                    )
                ),
            },
            {
                "heroes": {
                    "haze": {"name": "Haze", "slug": "haze", "abilities": {}},
                },
                "items": {
                    "spirit burn": {
                        "name": "Spirit Burn",
                        "slug": "spirit-burn",
                        "category": "spirit",
                        "icon_url": "https://deadlock.io/assets/items/spirit-burn.png",
                    },
                },
                "objectives": {
                    "urn": {"name": "Urn", "icon_url": urn_icon},
                    "unstable_rift": {"name": "Unstable Rift", "icon_url": rift_icon},
                },
            },
        )

        self.assertEqual(
            [(section["kind"], section["title"]) for section in detail["sections"]],
            [
                ("general", "Общие изменения"),
                ("objective", "Urn"),
                ("objective", "Unstable Rift"),
                ("item", "Spirit Burn"),
                ("hero", "Haze"),
            ],
        )
        self.assertEqual(len(detail["sections"][1]["changes"]), 5)
        self.assertEqual(len(detail["sections"][2]["changes"]), 6)
        self.assertEqual(
            detail["sections"][0]["changes"],
            [
                "Map lane timing adjusted",
                "Objectives bounty split for nearby heroes reduced",
                "Urn and Unstable Rift rewards are unchanged",
            ],
        )
        self.assertEqual(detail["sections"][1]["objective_icon_url"], urn_icon)
        self.assertEqual(detail["sections"][2]["objective_icon_url"], rift_icon)
        all_changes = [
            change
            for section in detail["sections"]
            for change in section["changes"]
        ]
        self.assertNotIn("[ Urn / King of the Hill ]", all_changes)
        validated = PatchDetailResponse.model_validate(detail)
        self.assertEqual(validated.sections[1].objective_key, "urn")
        self.assertEqual(validated.sections[2].objective_key, "unstable_rift")

    def test_objective_aliases_are_exact_and_require_prefix_boundaries(self) -> None:
        urn_aliases = (
            "spirit urn",
            "the urn runner",
            "urn runner",
            "urn running",
            "the urn",
            "urn",
        )
        rift_aliases = (
            "the unstable rift",
            "unstable rift zone",
            "unstable rift",
            "rift troopers",
            "rift trooper",
            "king of the hill objective",
            "king of the hill",
        )
        rejected_lines = (
            "Churn rate reduced",
            "Urnish carrier speed reduced",
            "Urn? carrier speed reduced",
            "Unstable Rifted units changed",
            "King of the Hills changed",
            "Rift rewards changed",
            "Idol rewards changed",
            "soul_jar rewards changed",
            "koth rewards changed",
        )
        content = "\n".join(
            [
                *(f"- {alias}: urn change {index}" for index, alias in enumerate(urn_aliases)),
                *(f"- {alias} - rift change {index}" for index, alias in enumerate(rift_aliases)),
                *(f"- {line}" for line in rejected_lines),
            ]
        )

        detail = home_content._structure_patch_detail(
            {"content": content},
            {"heroes": {}, "items": {}, "objectives": {}},
        )

        self.assertEqual(
            [section["kind"] for section in detail["sections"]],
            ["general", "objective", "objective"],
        )
        self.assertEqual(detail["sections"][0]["changes"], list(rejected_lines))
        self.assertEqual(len(detail["sections"][1]["changes"]), len(urn_aliases))
        self.assertEqual(len(detail["sections"][2]["changes"]), len(rift_aliases))
        self.assertEqual(detail["sections"][1]["changes"][0], "Urn change 0")
        self.assertEqual(detail["sections"][2]["changes"][0], "Rift change 0")

    def test_item_and_hero_prefixes_take_precedence_over_objectives(self) -> None:
        detail = home_content._structure_patch_detail(
            {
                "content": "\n".join(
                    (
                        "- Spirit Urn: Item change",
                        "- Urn Runner: Hero change",
                        "- Urn running - Objective change",
                    )
                ),
            },
            {
                "heroes": {
                    "spirit urn": {"name": "Hero Item", "slug": "hero-item", "abilities": {}},
                    "urn runner": {"name": "Runner", "slug": "runner", "abilities": {}},
                },
                "items": {
                    "spirit urn": {
                        "name": "Spirit Urn",
                        "slug": "spirit-urn",
                        "category": "spirit",
                        "icon_url": None,
                    },
                },
                "objectives": {},
            },
        )

        self.assertEqual(
            [(section["kind"], section["title"]) for section in detail["sections"]],
            [
                ("objective", "Urn"),
                ("item", "Spirit Urn"),
                ("hero", "Runner"),
            ],
        )
        self.assertEqual(detail["sections"][0]["changes"], ["Objective change"])
        self.assertEqual(detail["sections"][1]["changes"], ["Item change"])
        self.assertEqual(detail["sections"][2]["changes"], ["Hero change"])

    def test_patch_section_schema_enforces_kind_specific_metadata(self) -> None:
        valid = PatchSectionResponse.model_validate(
            {
                "kind": "objective",
                "title": "Urn",
                "objective_key": "urn",
                "objective_icon_url": (
                    "https://assets-bucket.deadlock-api.com/"
                    "assets-api-res/icons/minimap/soul_jar_marker_psd.png"
                ),
                "changes": ["Urn changed"],
            }
        )

        self.assertEqual(valid.objective_key, "urn")
        invalid_sections = (
            {"kind": "objective", "title": "Urn"},
            {"kind": "general", "title": "General", "objective_key": "urn"},
            {"kind": "item", "title": "Item", "item_name": "Item"},
            {
                "kind": "hero",
                "title": "Hero",
                "hero_name": "Hero",
                "objective_icon_url": "https://example.com/icon.png",
            },
        )
        for payload in invalid_sections:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                PatchSectionResponse.model_validate(payload)

    def test_objective_icon_catalog_uses_exact_allowlisted_minimap_keys(self) -> None:
        urn_icon = (
            "https://assets-bucket.deadlock-api.com/"
            "assets-api-res/icons/minimap/soul_jar_marker_psd.png"
        )
        rift_icon = (
            "https://assets-bucket.deadlock-api.com/"
            "assets-api-res/icons/minimap/minimap_icon_koth.png"
        )
        catalog = home_content._build_asset_catalog(
            {"heroes": []},
            {"items": []},
            icon_payload={
                "soul_jar_marker_psd.png": "https://example.com/wrong-level.png",
                "minimap": {
                    "soul_jar_marker_psd.png": urn_icon,
                    "minimap_icon_koth.png": rift_icon,
                    "soul_jar_marker.png": "https://example.com/wrong-key.png",
                },
            },
        )

        self.assertEqual(catalog["objectives"]["urn"]["icon_url"], urn_icon)
        self.assertEqual(catalog["objectives"]["unstable_rift"]["icon_url"], rift_icon)

        rejected = home_content._build_asset_catalog(
            {"heroes": []},
            {"items": []},
            icon_payload={
                "minimap": {
                    "soul_jar_marker_psd.png": "https://example.com/untrusted.png",
                },
            },
        )
        self.assertIsNone(rejected["objectives"]["urn"]["icon_url"])

    async def test_icon_source_failure_does_not_block_catalog_and_keeps_previous_icons(self) -> None:
        previous_catalog = {
            "objectives": {
                "urn": {
                    "name": "Urn",
                    "icon_url": (
                        "https://assets-bucket.deadlock-api.com/"
                        "assets-api-res/icons/minimap/previous-urn.png"
                    ),
                },
                "unstable_rift": {
                    "name": "Unstable Rift",
                    "icon_url": (
                        "https://assets-bucket.deadlock-api.com/"
                        "assets-api-res/icons/minimap/previous-rift.png"
                    ),
                },
            },
        }
        client = Mock()
        client.get = AsyncMock(
            side_effect=[
                _Response(json_payload={"heroes": []}),
                _Response(json_payload={"items": []}),
                _Response(json_payload=[]),
                RuntimeError("icon source unavailable"),
            ]
        )

        catalog = await home_content._fetch_deadlock_asset_catalog(
            client,
            previous_catalog=previous_catalog,
        )

        self.assertEqual(catalog["objectives"], previous_catalog["objectives"])
        self.assertEqual(client.get.await_count, 4)

    def test_public_content_cache_versions_keep_home_contract_stable(self) -> None:
        self.assertEqual(home_content.HOME_CONTENT_KEY, "platform:home-content:v4")
        self.assertEqual(home_content.HOME_CONTENT_STALE_KEY, "platform:home-content:stale:v4")
        self.assertEqual(home_content.PATCH_DETAIL_KEY_PREFIX, "platform:home-content:patch:v6:")
        self.assertEqual(
            home_content.PATCH_ASSET_CATALOG_KEY,
            "platform:home-content:patch-assets:v4",
        )

    def test_asset_catalog_supports_both_doorman_names(self) -> None:
        catalog = home_content._build_asset_catalog(
            {
                "heroes": [
                    {
                        "displayName": {"english": "The Doorman"},
                        "slug": "doorman",
                        "assets": {
                            "card": {"publicPath": "/assets/game/heroes/doorman_card.png"}
                        },
                        "abilities": [],
                    }
                ]
            },
            {
                "items": [
                    {
                        "displayName": {"english": "New Weapon"},
                        "slug": "new-weapon",
                        "shop": {"category": "weapon"},
                        "assets": {
                            "shopIcon": {"publicPath": "/assets/game/items/new_weapon.png"}
                        },
                    }
                ]
            },
            [
                {
                    "tier": 11,
                    "images": {
                        "large_webp": "https://assets-bucket.deadlock-api.com/assets-api-res/images/ranks/rank11.webp"
                    },
                }
            ],
        )

        self.assertIs(catalog["heroes"]["doorman"], catalog["heroes"]["the doorman"])
        self.assertEqual(catalog["heroes"]["doorman"]["name"], "The Doorman")
        self.assertEqual(
            catalog["heroes"]["doorman"]["icon_url"],
            "https://deadlock.io/assets/game/heroes/doorman_card.png",
        )
        self.assertEqual(catalog["items"]["new weapon"]["category"], "weapon")
        self.assertEqual(
            catalog["items"]["new weapon"]["icon_url"],
            "https://deadlock.io/assets/game/items/new_weapon.png",
        )
        self.assertEqual(catalog["ranks"]["eternus"]["name"], "Eternus")

        public_assets = DeadlockGameAssetsResponse.model_validate(
            home_content.public_deadlock_game_assets(catalog)
        )
        self.assertEqual([hero.name for hero in public_assets.heroes], ["The Doorman"])
        self.assertEqual(public_assets.ranks[0].name, "Eternus")
        self.assertTrue(public_assets.ranks[0].source_available)

    def test_patch_detail_places_items_between_general_and_heroes_by_category(self) -> None:
        detail = home_content._structure_patch_detail(
            {
                "id": "items",
                "title": "Patch",
                "published_at": "2026-07-28T12:00:00Z",
                "url": "https://example.com/patch",
                "content": (
                    "- Map changed"
                    "- Spirit Burn: Damage reduced"
                    "- Doorman: Doorway duration reduced"
                    "- Restorative Locket: Heal increased"
                    "- Crushing Fists: Stun increased"
                    "- Basic Magazine: Ammo increased"
                ),
            },
            {
                "heroes": {
                    "doorman": {"name": "The Doorman", "slug": "doorman", "abilities": {}},
                    "the doorman": {"name": "The Doorman", "slug": "doorman", "abilities": {}},
                },
                "items": {
                    "crushing fists": {
                        "name": "Crushing Fists", "slug": "crushing-fists", "category": "weapon", "cost": 3000, "icon_url": "weapon.png"
                    },
                    "basic magazine": {
                        "name": "Basic Magazine", "slug": "basic-magazine", "category": "weapon", "cost": 500, "icon_url": "weapon-basic.png"
                    },
                    "restorative locket": {
                        "name": "Restorative Locket", "slug": "restorative-locket", "category": "vitality", "icon_url": "vitality.png"
                    },
                    "spirit burn": {
                        "name": "Spirit Burn", "slug": "spirit-burn", "category": "spirit", "icon_url": "spirit.png"
                    },
                },
            },
        )

        self.assertEqual(
            [(section["kind"], section["title"]) for section in detail["sections"]],
            [
                ("general", "Общие изменения"),
                ("item", "Basic Magazine"),
                ("item", "Crushing Fists"),
                ("item", "Restorative Locket"),
                ("item", "Spirit Burn"),
                ("hero", "The Doorman"),
            ],
        )
        validated = PatchDetailResponse.model_validate(detail)
        self.assertEqual(validated.sections[1].item_category, "weapon")
        self.assertEqual(validated.sections[-1].hero_name, "The Doorman")

    def test_patch_detail_removes_repeated_ability_name_and_normalizes_case(self) -> None:
        detail = home_content._structure_patch_detail(
            {
                "id": "ava",
                "title": "Patch",
                "published_at": "2026-07-20T12:00:00Z",
                "url": "https://example.com/patch",
                "content": "- Hero: Ava T2 reduced- Hero: Ava: t3 reduced",
            },
            {
                "hero": {
                    "name": "Hero",
                    "slug": "hero",
                    "abilities": {
                        "ava": {"name": "Ava", "icon_url": None},
                    },
                }
            },
        )

        self.assertEqual(
            detail["sections"][0]["abilities"][0]["changes"],
            ["T2 reduced", "T3 reduced"],
        )

    async def test_support_is_rejected_until_smtp_is_configured(self) -> None:
        payload = SupportMessageRequest(
            name="Player",
            email="player@example.com",
            category="tournament",
            message="Не могу подтвердить участие.",
        )
        request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1)})
        with patch.object(
            content_routes,
            "get_settings",
            return_value=PlatformSettings(_env_file=None),
        ):
            with self.assertRaises(HTTPException) as raised:
                await content_routes.submit_support_message(payload, request)

        self.assertEqual(raised.exception.status_code, 503)

    async def test_support_is_rate_limited_without_logging_or_persistence(self) -> None:
        settings = PlatformSettings(
            _env_file=None,
            platform_support_recipient_email="owner@example.com",
            platform_support_smtp_host="smtp.example.com",
            platform_support_smtp_sender_email="support@example.com",
            platform_support_rate_limit_per_hour=3,
        )
        payload = SupportMessageRequest(
            name="Player",
            email="player@example.com",
            category="technical",
            message="Страница турнира не открывается.",
        )
        request = Request(
            {
                "type": "http",
                "headers": [(b"x-forwarded-for", b"198.51.100.20")],
                "client": ("127.0.0.1", 1),
            }
        )
        cache = Mock()
        cache.incr = AsyncMock(return_value=4)
        cache.expire = AsyncMock()
        cache.aclose = AsyncMock()
        with (
            patch.object(content_routes, "get_settings", return_value=settings),
            patch.object(content_routes, "redis_client", return_value=cache),
            patch.object(content_routes, "send_support_message", AsyncMock()) as send,
        ):
            with self.assertRaises(HTTPException) as raised:
                await content_routes.submit_support_message(payload, request)

        self.assertEqual(raised.exception.status_code, 429)
        send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
