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
from apps.platform_api.app.services import home_content_security
from apps.platform_api.app.services import patch_detail
from apps.platform_api.app.services import patch_detail_security
from apps.platform_api.app.services import patch_translation
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

    def delete(self, *keys):
        self.delete_calls = getattr(self, "delete_calls", []) + [keys]
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

    async def test_pending_hero_asset_retry_respects_shared_backoff_key(self) -> None:
        stale_home = {
            "patches": [{"id": "1846651706683085"}],
            "videos": [],
            "generated_at": "2026-10-23T00:00:00+00:00",
            "patches_available": True,
            "videos_available": True,
        }
        cached_catalog = {"heroes": {}, "items": {}, "ranks": {}, "objectives": {}}
        cached_detail = {"hero_metadata_pending": True, "hero_catalog_pending": True, "sections": []}
        cache = _Cache(
            {
                home_content.HOME_CONTENT_STALE_KEY: json.dumps(stale_home),
                home_content.PATCH_ASSET_CATALOG_KEY: json.dumps(cached_catalog),
                home_content.PATCH_DETAIL_KEY_PREFIX + "1846651706683085": json.dumps(cached_detail),
                home_content.PATCH_ASSET_PENDING_RETRY_KEY: "pending",
            }
        )
        patches = list(stale_home["patches"])
        steam_details = {
            "1846651706683085": {
                "id": "1846651706683085",
                "title": "Paint the Town",
                "content": "Violet is available in-game now.",
                "published_at": "2026-10-20T00:00:00Z",
                "url": "https://store.steampowered.com/news/app/1422450/view/1846651706683085",
            }
        }
        asset_fetch = AsyncMock(return_value=cached_catalog)
        settings = PlatformSettings(_env_file=None)
        with (
            patch.object(home_content_security, "redis_client", return_value=cache),
            patch.object(home_content_security, "get_settings", return_value=settings),
            patch.object(home_content, "_fetch_steam_patches", AsyncMock(return_value=(patches, steam_details))),
            patch.object(home_content, "_fetch_youtube_videos", AsyncMock(return_value=[])),
            patch.object(home_content, "_fetch_deadlock_asset_catalog", asset_fetch),
            patch.object(
                home_content_security,
                "ensure_patch_translation_records",
                AsyncMock(return_value={"registered": 0, "enqueued": 0, "enqueue_failures": 0}),
            ),
        ):
            await home_content_security.refresh_home_content(
                force=True,
                force_asset_refresh=False,
            )

        asset_fetch.assert_not_awaited()

    async def test_translation_only_pending_hero_does_not_schedule_asset_retry(self) -> None:
        feed = {"patches": [{"id": "1846651706683085"}]}
        cache = _Cache(
            {
                home_content.PATCH_DETAIL_KEY_PREFIX + "1846651706683085": json.dumps(
                    {
                        "hero_metadata_pending": True,
                        "hero_catalog_pending": False,
                        "sections": [],
                    }
                )
            }
        )

        pending_catalog = await home_content_security._cached_home_has_pending_hero_catalog(
            cache,
            feed,
        )

        self.assertFalse(pending_catalog)

    def test_catalog_english_descriptions_enter_translation_fields_directly(self) -> None:
        icon_url = (
            "https://assets-bucket.deadlock-api.com/"
            "assets-api-res/images/ability_test.png"
        )
        catalog = home_content._build_asset_catalog(
            {"heroes": []},
            {"items": []},
            supplemental_hero_payload=[
                {
                    "id": 88,
                    "class_name": "hero_test",
                    "name": "Test Hero",
                    "player_selectable": True,
                    "development_state": "release",
                    "disabled": False,
                    "items": {"signature1": "ability_test"},
                    "description": {"lore": "English hero lore."},
                    "images": {"icon_hero_card": "https://assets-bucket.deadlock-api.com/heroes/test.png"},
                }
            ],
            supplemental_item_payload=[
                {
                    "id": 881,
                    "class_name": "ability_test",
                    "type": "ability",
                    "name": "Test Ability",
                    "description": {"desc": "English ability description."},
                    "image": icon_url,
                }
            ],
            localized_supplemental_hero_payload=[
                {
                    "id": 88,
                    "class_name": "hero_test",
                    "name": "Тестовый герой",
                    "description": {},
                }
            ],
            localized_supplemental_item_payload=[
                {
                    "id": 881,
                    "class_name": "ability_test",
                    "name": "Тестовое умение",
                    "description": {},
                }
            ],
        )
        hero = catalog["heroes"]["test hero"]
        ability = hero["abilities"]["test ability"]

        self.assertEqual(hero["description"], "English hero lore.")
        self.assertTrue(hero["description_requires_translation"])
        self.assertEqual(ability["description"], "English ability description.")
        self.assertTrue(ability["description_requires_translation"])
        self.assertFalse(ability["russian_metadata_available"])

        detail = patch_detail.structure_patch_detail(
            {
                "id": "test-release",
                "title": "Test Hero is Here",
                "content": "Test Hero is available in-game now.",
                "published_at": "2026-10-20T00:00:00Z",
                "url": "https://store.steampowered.com/news/app/1422450/view/test-release",
            },
            catalog,
        )
        translation_segments = patch_translation.extract_translation_segments(detail)
        self.assertIn("English hero lore.", {segment["text"] for segment in translation_segments})
        self.assertIn(
            "English ability description.",
            {segment["text"] for segment in translation_segments},
        )

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

    def test_hero_catalog_enrichment_is_localized_and_plain_text(self) -> None:
        catalog = home_content._build_asset_catalog(
            {
                "heroes": [
                    {
                        "slug": "unicorn",
                        "displayName": {
                            "english": "Celeste",
                            "byLanguage": {"russian": "Селеста"},
                        },
                        "lore": {
                            "english": "English lore",
                            "byLanguage": {
                                "russian": "<p>Русская <b>история</b> {s:hero_name}</p>"
                            },
                        },
                        "assets": {
                            "card": {"publicPath": "/assets/game/celeste-card.png"}
                        },
                        "abilities": [
                            {
                                "displayName": {
                                    "english": "Light Eater",
                                    "byLanguage": {"russian": "Пожиратель света"},
                                },
                                "description": {
                                    "english": "English ability",
                                    "byLanguage": {
                                        "russian": "<p>Наносит {g:citadel_inline_attribute:'SpiritDamage'} урон.</p>"
                                    },
                                },
                                "assets": {
                                    "icon": {"publicPath": "/assets/game/light-eater.png"}
                                },
                            }
                        ],
                    }
                ]
            },
            {"items": []},
        )

        hero = catalog["heroes"]["celeste"]
        ability = hero["abilities"]["light eater"]
        self.assertEqual(hero["display_name"], "Селеста")
        self.assertEqual(hero["description"], "Русская история")
        self.assertEqual(
            hero["hero_image_url"], "https://deadlock.io/assets/game/celeste-card.png"
        )
        self.assertEqual(ability["name"], "Light Eater")
        self.assertEqual(ability["display_name"], "Пожиратель света")
        self.assertEqual(ability["description"], "Наносит урон.")
        self.assertEqual(
            ability["icon_url"], "https://deadlock.io/assets/game/light-eater.png"
        )

    def test_patch_parser_carries_hero_and_ability_enrichment(self) -> None:
        detail = patch_detail.structure_patch_detail(
            {"id": "123", "content": "[Heroes]\nCeleste\nLight Eater cooldown reduced"},
            {
                "heroes": {
                    "celeste": {
                        "name": "Celeste",
                        "display_name": "Селеста",
                        "hero_image_url": "https://deadlock.io/assets/celeste.png",
                        "description": "История героя",
                        "abilities": {
                            "light eater": {
                                "name": "Light Eater",
                                "display_name": "Пожиратель света",
                                "description": "Описание способности",
                                "icon_url": "https://deadlock.io/assets/light-eater.png",
                            }
                        },
                    }
                },
                "items": {},
                "objectives": {},
            },
        )
        section = detail["sections"][0]
        ability = section["abilities"][0]
        self.assertEqual(section["hero_display_name"], "Селеста")
        self.assertEqual(section["hero_description"], "История героя")
        self.assertEqual(ability["display_name"], "Пожиратель света")
        self.assertEqual(ability["description"], "Описание способности")
        self.assertEqual(ability["changes"], ["cooldown reduced"])

    def test_unstructured_hero_announcement_gets_full_ability_roster(self) -> None:
        detail = patch_detail.structure_patch_detail(
            {
                "id": "1824459501608168",
                "title": "Apollo - A Cut Above",
                "content": "Apollo's release is here. Flawless Advance starts his approach.",
            },
            {
                "heroes": {
                    "apollo": {
                        "id": 77,
                        "name": "Apollo",
                        "slug": "apollo",
                        "display_name": "Аполлон",
                        "hero_image_url": "https://deadlock.io/assets/apollo.png",
                        "description": "История Аполлона",
                        "abilities": {
                            "flawless advance": {
                                "name": "Flawless Advance",
                                "display_name": "Безупречный выпад",
                                "description": "Описание выпада",
                                "icon_url": "https://deadlock.io/assets/advance.png",
                            },
                            "riposte": {
                                "name": "Riposte",
                                "display_name": "Рипост",
                                "description": "Описание рипоста",
                                "icon_url": "https://deadlock.io/assets/riposte.png",
                            },
                        },
                    }
                },
                "items": {},
                "objectives": {},
            },
        )
        section = next(item for item in detail["sections"] if item["kind"] == "hero")
        self.assertEqual(section["hero_name"], "Apollo")
        self.assertEqual(section["hero_display_name"], "Аполлон")
        self.assertEqual(section["hero_image_url"], "https://deadlock.io/assets/apollo.png")
        self.assertEqual(
            [ability["name"] for ability in section["abilities"]],
            ["Flawless Advance", "Riposte"],
        )
        self.assertTrue(all(not ability["changes"] for ability in section["abilities"]))

    async def test_live_home_hero_announcements_use_current_localized_catalog_and_translation(self) -> None:
        heroes = {
            "Nurse Harrow": (
                87,
                "hero_nurse",
                "Сестра Хэрроу",
                ("ability_nurse_leech_therapy", "ability_nurse_adrenaline_rush", "ability_nurse_healing_needles", "ability_nurse_fear"),
                ("Leech Therapy", "Wretched Vapors", "Healing Needles", "Lurking Terrors"),
                ("Гирудотерапия", "Гнусные пары", "Лечебные иглы", "Затаённые ужасы"),
                "https://assets-bucket.deadlock-api.com/assets-api-res/images/heroes/nurse_card.webp",
                (
                    "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/nurse/leech.png",
                    "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/nurse/nurse_vapors.png",
                    "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/nurse/needle.png",
                    "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/nurse/medicine_bag.png",
                ),
            ),
            "Violet": (
                86,
                "hero_artist",
                "Виолетта",
                ("ability_artist_homingbrush", "ability_artist_rollingprojectile", "ability_artist_tether", "ability_artist_bamboogrove"),
                ("Living Ink", "Flow State", "Still Life", "Abstract Landscape"),
                ("Живая тушь", "В потоке", "Портрет с натуры", "Абстрактный пейзаж"),
                "https://assets-bucket.deadlock-api.com/assets-api-res/images/heroes/artist_card.webp",
                (
                    "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/artist/artist_livingink.png",
                    "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/artist/artist_brush.png",
                    "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/artist/artist_portrait.png",
                    "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/artist/artist_landscape.png",
                ),
            ),
            "Deadman Danny": (
                78,
                "hero_deadpack",
                "Дохлый Дэнни",
                ("ability_deadpack_chargeblast", "ability_deadpack_rocketeer", "ability_deadpack_flamethrower", "ability_deadpack_ultimate"),
                ("Strix Salvo", "Chimera Mk1", "Jettison Fuel", "Close The Deal"),
                ("Стригийский залп", "Химера М1", "Сброс топлива", "Закрыть сделку"),
                "https://assets-bucket.deadlock-api.com/assets-api-res/images/heroes/deadpack_card.webp",
                (
                    "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/deadpack/deadpack_salvo.png",
                    "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/deadpack/deadpack_chimera.png",
                    "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/deadpack/deadpack_thermalmass.png",
                    "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/deadpack/deadpack_flight.png",
                ),
            ),
        }
        english_heroes = []
        russian_heroes = []
        english_items = []
        russian_items = []
        for hero_name, (
            hero_id,
            class_name,
            localized_name,
            ability_classes,
            ability_names,
            localized_abilities,
            hero_image,
            ability_images,
        ) in heroes.items():
            hero_abilities = {}
            for slot, (
                ability_class,
                ability_name,
                localized_ability,
                ability_image,
            ) in enumerate(
                zip(ability_classes, ability_names, localized_abilities, ability_images, strict=True),
                start=1,
            ):
                hero_abilities[f"signature{slot}"] = ability_class
                english_items.append(
                    {
                        "id": hero_id * 10 + slot,
                        "class_name": ability_class,
                        "type": "ability",
                        "name": ability_name,
                        "image": ability_image,
                    }
                )
                russian_items.append(
                    {
                        "id": hero_id * 10 + slot,
                        "class_name": ability_class,
                        "type": "ability",
                        "name": localized_ability,
                        "description": (
                            {"passive": "Пассивное описание."}
                            if hero_name == "Violet" and slot == 1
                            else {"desc": f"<p>Описание: {localized_ability}.</p>"}
                        ),
                        "image": ability_image,
                    }
                )
            english_heroes.append(
                {
                    "id": hero_id,
                    "class_name": class_name,
                    "name": hero_name,
                    "player_selectable": True,
                    "development_state": "release",
                    "disabled": False,
                    "items": hero_abilities,
                    "images": {
                        "icon_hero_card_webp": hero_image
                    },
                }
            )
            russian_heroes.append(
                {
                    "id": hero_id,
                    "class_name": class_name,
                    "name": localized_name,
                    "description": {"lore": f"<p>История: {localized_name}.</p>"},
                }
            )

        catalog = home_content._build_asset_catalog(
            {"heroes": []},
            {"items": []},
            supplemental_hero_payload=english_heroes,
            supplemental_item_payload=english_items,
            localized_supplemental_hero_payload=russian_heroes,
            localized_supplemental_item_payload=russian_items,
        )
        announcements = (
            {
                "id": "1846651706696737",
                "title": "Shh... This Won't Hurt a Bit.",
                "content": "That chill may be from the withering glare of Nurse Harrow, who now joins us. With Leech Therapy, she drains enemies and energy. Cracking open a vial, she releases Wretched Vapors. She loads Healing Needles to heal her team. When her patience runs out, she releases Lurking Terrors.",
            },
            {
                "id": "1846651706683085",
                "title": "Paint the Town",
                "content": "Brush in hand, Violet approaches the ritual with precision, wielding Living Ink that empowers her bullets and draws inspiration into reality. When entering Flow State, her ink glides towards danger or away from it. By creating a Still Life she captures her enemies' likeness. With Abstract Landscape she surrounds nearby allies with her biggest piece. Violet is available in-game now!",
            },
            {
                "id": "1846018067945303",
                "title": "Fairfax Industries Employee Spotlight",
                "content": "Today we introduce the salesman: Deadman Danny. Strix Salvo marks foes.",
            },
        )
        structured = []
        for announcement in announcements:
            value = patch_detail.structure_patch_detail(
                {
                    **announcement,
                    "published_at": "2026-10-20T00:00:00Z",
                    "url": "https://store.steampowered.com/news/app/1422450",
                },
                catalog,
            )
            hero = next(section for section in value["sections"] if section["kind"] == "hero")
            self.assertEqual(len(hero["abilities"]), 4)
            self.assertTrue(all(ability["icon_url"] for ability in hero["abilities"]))
            self.assertTrue(all(ability["display_name"] for ability in hero["abilities"]))
            structured.append(value)

        violet = next(section for section in structured[1]["sections"] if section["kind"] == "hero")
        living_ink = next(item for item in violet["abilities"] if item["name"] == "Living Ink")
        self.assertTrue(living_ink["description_from_announcement"])
        self.assertEqual(
            living_ink["description"],
            "Brush in hand, Violet approaches the ritual with precision, wielding Living Ink that empowers her bullets and draws inspiration into reality.",
        )
        self.assertNotIn("Flow State", living_ink["description"])
        self.assertNotIn("available in-game", living_ink["description"])
        self.assertTrue(violet["hero_metadata_pending"])
        self.assertFalse(
            violet["hero_catalog_pending"],
            {
                "hero": {key: violet.get(key) for key in ("hero_id", "hero_image_url", "hero_display_name", "hero_description")},
                "abilities": [{key: ability.get(key) for key in ("name", "display_name", "description", "description_requires_translation", "icon_url")} for ability in violet["abilities"]],
            },
        )
        fallback_segment = next(
            segment
            for segment in patch_translation.extract_translation_segments(structured[1])
            if segment["text"] == living_ink["description"]
        )
        self.assertTrue(fallback_segment["id"].endswith("-d"))

        refreshed_catalog = json.loads(json.dumps(catalog, ensure_ascii=False))
        refreshed_violet = next(
            hero
            for hero in refreshed_catalog["heroes"].values()
            if hero.get("hero_id") == 86
        )
        refreshed_living_ink = next(
            ability
            for ability in refreshed_violet["abilities"].values()
            if ability.get("name") == "Living Ink"
        )
        refreshed_living_ink["description"] = "Живая тушь усиливает пули Виолетты."
        refreshed_living_ink["russian_metadata_available"] = True
        caught_up = patch_detail.structure_patch_detail(
            {
                **announcements[1],
                "published_at": "2026-10-20T00:00:00Z",
                "url": "https://store.steampowered.com/news/app/1422450",
            },
            refreshed_catalog,
        )
        caught_up_violet = next(
            section for section in caught_up["sections"] if section["kind"] == "hero"
        )
        self.assertFalse(caught_up_violet["hero_metadata_pending"])

        balance_only = patch_detail.structure_patch_detail(
            {
                "id": "balance-harrow",
                "title": "Harrow Balance Update",
                "content": "Leech Therapy now has a longer cooldown.",
                "published_at": "2026-10-20T00:00:00Z",
                "url": "https://store.steampowered.com/news/app/1422450",
            },
            catalog,
        )
        self.assertFalse(any(item["kind"] == "hero" for item in balance_only["sections"]))

        async def translate_cached(value: dict[str, object]) -> dict[str, object]:
            copied = json.loads(json.dumps(value, ensure_ascii=False))
            segment = next(
                item
                for item in patch_translation.extract_translation_segments(copied)
                if item["text"] == living_ink["description"]
            )
            return patch_translation.merge_translation(
                copied,
                {segment["id"]: "Усиливает пули Виолетты и позволяет направлять их в полёте."},
            )

        with (
            patch.object(patch_detail, "get_patch_detail_source", AsyncMock(return_value=structured[1])),
            patch.object(patch_detail, "apply_cached_patch_translation", translate_cached),
        ):
            translated = await patch_detail.get_patch_detail("1846651706683085")

        response = PatchDetailResponse.model_validate(translated)
        violet_response = next(section for section in response.sections if section.hero_name == "Violet")
        living_ink_response = next(ability for ability in violet_response.abilities if ability.name == "Living Ink")
        self.assertEqual(living_ink_response.description, "Усиливает пули Виолетты и позволяет направлять их в полёте.")
        self.assertFalse(
            violet_response.hero_metadata_pending,
            {
                "hero_display_name": violet_response.hero_display_name,
                "hero_image_url": violet_response.hero_image_url,
                "hero_description": violet_response.hero_description,
                "abilities": [ability.model_dump() for ability in violet_response.abilities],
            },
        )
        self.assertFalse(response.hero_metadata_pending)

        catalog_description_patch = json.loads(
            json.dumps(structured[1], ensure_ascii=False)
        )
        catalog_violet = next(
            section
            for section in catalog_description_patch["sections"]
            if section["kind"] == "hero"
        )
        catalog_living_ink = next(
            ability
            for ability in catalog_violet["abilities"]
            if ability["name"] == "Living Ink"
        )
        english_catalog_description = (
            "Brush in hand, Violet approaches the ritual with the same "
            "precision she does her art, wielding Living Ink that both "
            "empowers her bullets, and draws mere inspiration into reality."
        )
        catalog_living_ink.update(
            {
                "description": english_catalog_description,
                "description_requires_translation": True,
                "russian_metadata_available": False,
                "_description_source_text": english_catalog_description,
            }
        )
        catalog_living_ink.pop("description_from_announcement", None)

        async def translate_catalog_description(value: dict[str, object]) -> dict[str, object]:
            copied = json.loads(json.dumps(value, ensure_ascii=False))
            segment = next(
                item
                for item in patch_translation.extract_translation_segments(copied)
                if item["text"] == catalog_living_ink["description"]
            )
            return patch_translation.merge_translation(
                copied,
                {
                    segment["id"]: (
                        "Виолетта обращается с Живой тушью с точностью художника: "
                        "она усиливает её пули и воплощает вдохновение в реальность."
                    )
                },
            )

        with (
            patch.object(
                patch_detail_security,
                "_cached_patch_source",
                AsyncMock(return_value=catalog_description_patch),
            ),
            patch.object(
                patch_detail_security,
                "_cached_asset_catalog",
                AsyncMock(return_value=None),
            ),
            patch.object(
                patch_detail_security,
                "apply_cached_patch_translation",
                translate_catalog_description,
            ),
        ):
            catalog_translated = await patch_detail_security.get_patch_detail(
                "1846651706683085"
            )

        catalog_response = PatchDetailResponse.model_validate(catalog_translated)
        catalog_violet_response = next(
            section
            for section in catalog_response.sections
            if section.hero_name == "Violet"
        )
        catalog_living_ink_response = next(
            ability
            for ability in catalog_violet_response.abilities
            if ability.name == "Living Ink"
        )
        self.assertTrue(catalog_living_ink_response.description.startswith("Виолетта"))
        self.assertFalse(catalog_violet_response.hero_metadata_pending)
        self.assertFalse(catalog_response.hero_metadata_pending)

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
            {"kind": "general", "title": "General", "hero_description": "Hero text"},
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

        hero = PatchSectionResponse.model_validate(
            {
                "kind": "hero",
                "title": "Celeste",
                "hero_name": "Celeste",
                "hero_display_name": "Селеста",
                "hero_image_url": "https://deadlock.io/assets/celeste.png",
                "hero_description": "История героя",
                "abilities": [
                    {
                        "name": "Light Eater",
                        "display_name": "Пожиратель света",
                        "description": "Наносит урон.",
                        "icon_url": "https://deadlock.io/assets/light-eater.png",
                    }
                ],
            }
        )
        self.assertEqual(hero.hero_display_name, "Селеста")
        self.assertEqual(hero.abilities[0].display_name, "Пожиратель света")

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
                _Response(json_payload=[]),
                _Response(json_payload=[]),
                _Response(json_payload=[]),
                _Response(json_payload=[]),
                RuntimeError("icon source unavailable"),
            ]
        )

        catalog = await home_content._fetch_deadlock_asset_catalog(
            client,
            previous_catalog=previous_catalog,
        )

        self.assertEqual(catalog["objectives"], previous_catalog["objectives"])
        self.assertEqual(client.get.await_count, 8)

    def test_public_content_cache_versions_keep_home_contract_stable(self) -> None:
        self.assertEqual(home_content.HOME_CONTENT_KEY, "platform:home-content:v4")
        self.assertEqual(home_content.HOME_CONTENT_STALE_KEY, "platform:home-content:stale:v4")
        self.assertEqual(home_content.PATCH_DETAIL_KEY_PREFIX, "platform:home-content:patch:v9:")
        self.assertEqual(
            home_content.PATCH_ASSET_CATALOG_KEY,
            "platform:home-content:patch-assets:v6",
        )
        self.assertEqual(
            home_content.PATCH_ASSET_CATALOG_STALE_KEY,
            "platform:home-content:patch-assets-stale:v1",
        )

    def test_supplemental_asset_catalog_adds_baba_once_with_its_four_abilities(self) -> None:
        baba_hero = {
            "id": 88,
            "class_name": "hero_baba",
            "name": "Baba",
            "player_selectable": True,
            "development_state": "release",
            "disabled": False,
            "in_development": True,
            "items": {
                "signature1": "ability_baba_bubbling_brew",
                "signature2": "ability_baba_bench_run",
                "signature3": "ability_baba_hexing_brew",
                "signature4": "ability_baba_ultimate_2",
            },
            "images": {
                "icon_hero_card_webp": (
                    "https://assets-bucket.deadlock-api.com/assets-api-res/images/heroes/baba_card.webp"
                ),
            },
        }
        baba_abilities = [
            {
                "id": ability_id,
                "class_name": class_name,
                "name": ability_name,
                "type": "ability",
                "hero": 88,
                "image": (
                    "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/baba/"
                    f"{slug}.png"
                ),
            }
            for ability_id, class_name, ability_name, slug in (
                (1282689113, "ability_baba_bubbling_brew", "Threadsap", "threadsap"),
                (3122124161, "ability_baba_bench_run", "Granny Long Legs", "strafing_run"),
                (2003165630, "ability_baba_hexing_brew", "Baba's Brew", "teapot_brew"),
                (299875845, "ability_baba_ultimate_2", "Feed The Birds", "pigeon_launcher"),
            )
        ]

        catalog = home_content._build_asset_catalog(
            {"heroes": []},
            {"items": []},
            supplemental_hero_payload=[baba_hero],
            supplemental_item_payload=baba_abilities,
        )
        public_assets = home_content.public_deadlock_game_assets(catalog)

        self.assertEqual([hero["name"] for hero in public_assets["heroes"]], ["Baba"])
        self.assertEqual(public_assets["heroes"][0]["draft_id"], "baba")
        self.assertEqual(catalog["heroes"]["baba"]["id"], 88)
        self.assertEqual(catalog["heroes"]["baba"]["slug"], "baba")
        self.assertEqual(
            set(catalog["heroes"]["baba"]["abilities"]),
            {"threadsap", "granny long legs", "baba's brew", "feed the birds"},
        )
        self.assertEqual(
            catalog["heroes"]["baba"]["abilities"]["baba's brew"]["slug"],
            "ability_baba_hexing_brew",
        )

        merged_primary = home_content._build_asset_catalog(
            {"heroes": [{"displayName": {"english": "Baba"}, "slug": "baba", "abilities": []}]},
            {"items": []},
            supplemental_hero_payload=[baba_hero],
            supplemental_item_payload=baba_abilities,
        )
        self.assertEqual(list(merged_primary["heroes"]), ["baba"])
        self.assertEqual(merged_primary["heroes"]["baba"]["slug"], "baba")

        renamed_primary = home_content._build_asset_catalog(
            {
                "heroes": [
                    {
                        "id": 88,
                        "displayName": {"english": "Baba the Brave"},
                        "slug": "hero_baba_alias",
                        "abilities": [],
                    }
                ]
            },
            {"items": []},
            supplemental_hero_payload=[baba_hero],
            supplemental_item_payload=baba_abilities,
        )
        self.assertEqual(
            len({
                id(hero)
                for hero in renamed_primary["heroes"].values()
                if isinstance(hero, dict)
            }),
            1,
        )

    def test_supplemental_catalog_ignores_not_playable_or_not_released_heroes(self) -> None:
        catalog = home_content._build_asset_catalog(
            {"heroes": []},
            {"items": []},
            supplemental_hero_payload=[
                {
                    "id": 88,
                    "class_name": "hero_baba",
                    "name": "Baba",
                    "player_selectable": True,
                    "development_state": "release",
                    "disabled": False,
                },
                {
                    "id": 89,
                    "class_name": "hero_unreleased",
                    "name": "Unreleased",
                    "player_selectable": True,
                    "development_state": "development",
                    "disabled": False,
                },
                {
                    "id": 90,
                    "class_name": "hero_disabled",
                    "name": "Disabled",
                    "player_selectable": True,
                    "development_state": "release",
                    "disabled": True,
                },
            ],
            supplemental_item_payload=[],
        )

        self.assertEqual(list(catalog["heroes"]), ["baba"])

    def test_supplemental_catalog_adds_new_abilities_to_primary_heroes_without_id_drift(self) -> None:
        primary_hero = {
            "displayName": {"english": "Apollo"},
            "slug": "apollo",
            "abilities": [
                {
                    "displayName": {"english": "Old Skill"},
                    "slug": "ability_apollo_new_skill",
                    "assets": {"icon": {"publicPath": "/assets/game/abilities/old_skill.png"}},
                }
            ],
        }
        supplemental_hero = {
            "id": 2,
            "class_name": "hero_apollo",
            "name": "Apollo",
            "player_selectable": True,
            "development_state": "release",
            "disabled": False,
            "items": {"signature1": "ability_apollo_new_skill"},
        }
        supplemental_item = {
            "id": 777,
            "class_name": "ability_apollo_new_skill",
            "name": "New Skill",
            "type": "ability",
            "hero": 2,
            "image": "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/new_skill.png",
        }
        catalog = home_content._build_asset_catalog(
            {"heroes": [primary_hero]},
            {"items": []},
            supplemental_hero_payload=[supplemental_hero],
            supplemental_item_payload=[supplemental_item],
        )

        self.assertEqual(list(catalog["heroes"]), ["apollo"])
        self.assertEqual(catalog["heroes"]["apollo"]["id"], 2)
        self.assertEqual(catalog["heroes"]["apollo"]["slug"], "apollo")
        self.assertEqual(set(catalog["heroes"]["apollo"]["abilities"]), {"new skill"})
        self.assertEqual(
            catalog["heroes"]["apollo"]["abilities"]["new skill"]["slug"],
            "ability_apollo_new_skill",
        )
        self.assertEqual(catalog["heroes"]["apollo"]["abilities"]["new skill"]["id"], 777)
        self.assertEqual(
            catalog["heroes"]["apollo"]["abilities"]["new skill"]["aliases"],
            ["Old Skill"],
        )

    def test_supplemental_source_outage_keeps_last_good_hero_but_valid_removal_drops_it(self) -> None:
        previous = home_content._build_asset_catalog(
            {"heroes": []},
            {"items": []},
            supplemental_hero_payload=[
                {
                    "id": 88,
                    "class_name": "hero_baba",
                    "name": "Baba",
                    "player_selectable": True,
                    "development_state": "release",
                    "disabled": False,
                }
            ],
            supplemental_item_payload=[],
        )
        outage = home_content._build_asset_catalog(
            {"heroes": []},
            {"items": []},
            supplemental_hero_payload=None,
            supplemental_item_payload=[],
            previous_catalog=previous,
        )
        removed = home_content._build_asset_catalog(
            {"heroes": []},
            {"items": []},
            supplemental_hero_payload=[
                {
                    "id": 88,
                    "class_name": "hero_baba",
                    "name": "Baba",
                    "player_selectable": False,
                    "development_state": "release",
                    "disabled": True,
                }
            ],
            supplemental_item_payload=[],
            previous_catalog=previous,
        )

        self.assertEqual(list(outage["heroes"]), ["baba"])
        self.assertEqual(outage["heroes"]["baba"]["id"], 88)
        self.assertEqual(removed["heroes"], {})

    async def test_empty_supplemental_api_payload_is_treated_as_outage(self) -> None:
        previous = {
            "heroes": {
                "baba": {
                    "id": 88,
                    "class_name": "hero_baba",
                    "name": "Baba",
                    "slug": "baba",
                    "source": "deadlock-api",
                    "abilities": {},
                }
            },
            "items": {},
            "ranks": {},
            "objectives": {},
        }
        client = Mock()
        client.get = AsyncMock(
            side_effect=[
                _Response(json_payload={"heroes": []}),
                _Response(json_payload={"items": []}),
                _Response(json_payload=[]),
                _Response(json_payload=[]),
                _Response(json_payload=[]),
                _Response(json_payload=[]),
                _Response(json_payload=[]),
                _Response(json_payload={}),
            ]
        )

        catalog = await home_content._fetch_deadlock_asset_catalog(
            client,
            previous_catalog=previous,
        )

        self.assertEqual(catalog["heroes"]["baba"]["id"], 88)

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
            return_value=PlatformSettings(
                _env_file=None,
                platform_support_recipient_email="support@example.com",
                platform_email_sender_email=None,
                platform_resend_api_key=None,
                platform_support_smtp_host=None,
                platform_support_smtp_username=None,
                platform_support_smtp_password=None,
                platform_support_smtp_sender_email=None,
            ),
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
