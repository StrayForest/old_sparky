from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from apps.platform_api.app.services import (
    home_content,
    patch_detail,
    patch_detail_security,
)
from apps.platform_api.app.api.schemas import PatchDetailResponse
from apps.platform_api.app.services.patch_translation import translation_source_hash
from python_packages.platform_infra.config import PlatformSettings
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


_REFRESH_OBSERVER_TIMEOUT_SECONDS = 0.5
_SOURCE_REPLAY_TIMEOUT_SECONDS = 15.0


def _capture_refresh_tasks() -> tuple[list[asyncio.Task[None]], asyncio.Event, object]:
    tasks: list[asyncio.Task[None]] = []
    registered = asyncio.Event()
    original = patch_detail_security._track_background_refresh

    def capture(task: asyncio.Task[None]) -> None:
        tasks.append(task)
        original(task)
        registered.set()

    return tasks, registered, patch.object(
        patch_detail_security, "_track_background_refresh", side_effect=capture
    )


async def _await_refresh_tasks(
    tasks: list[asyncio.Task[None]],
    *,
    timeout: float = _REFRESH_OBSERVER_TIMEOUT_SECONDS,
) -> None:
    if not tasks:
        return
    results = await asyncio.wait_for(
        asyncio.gather(*tasks, return_exceptions=True),
        timeout=timeout,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result


class _Pipeline:
    def __init__(self, cache: "_Cache") -> None:
        self.cache = cache
        self.pending: list[tuple[str, str | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def set(self, key: str, value: str, **kwargs):
        self.pending.append((key, value))
        return self

    def delete(self, key: str):
        self.pending.append((key, None))
        return self

    async def execute(self) -> list[bool]:
        for key, value in self.pending:
            if value is None:
                self.cache.values.pop(key, None)
            else:
                self.cache.values[key] = value
        return [True] * len(self.pending)


class _Cache:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.set_calls: list[tuple[str, str, dict]] = []
        self.get_calls: list[str] = []

    async def get(self, key: str) -> str | None:
        self.get_calls.append(key)
        return self.values.get(key)

    async def set(self, key: str, value: str, **kwargs) -> bool:
        self.set_calls.append((key, value, kwargs))
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True

    def pipeline(self, *, transaction: bool) -> _Pipeline:
        self.assert_transaction = transaction
        return _Pipeline(self)

    async def aclose(self) -> None:
        return None


class PlatformPatchMissSecurityTests(PlatformIsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._refresh_baseline = set(patch_detail_security._BACKGROUND_REFRESH_TASKS)

    async def asyncTearDown(self) -> None:
        tasks = list(
            set(patch_detail_security._BACKGROUND_REFRESH_TASKS)
            - self._refresh_baseline
        )
        if tasks:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=_REFRESH_OBSERVER_TIMEOUT_SECONDS,
            )
        self.assertEqual(
            set(patch_detail_security._BACKGROUND_REFRESH_TASKS),
            self._refresh_baseline,
        )

    async def test_cached_patch_is_returned_without_miss_refresh(self) -> None:
        detail = {"id": "123", "title": "Patch", "sections": []}
        cache = _Cache(
            {
                home_content.PATCH_DETAIL_KEY_PREFIX + "123": json.dumps(detail),
            }
        )
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "_refresh_patch_cache_after_miss",
                AsyncMock(),
            ) as refresh,
        ):
            result = await patch_detail_security._cached_patch_source("123")

        self.assertEqual(result, detail)
        refresh.assert_not_awaited()

    async def test_unknown_patch_sets_negative_cache_and_schedules_background_refresh(self) -> None:
        fixture_root = Path(__file__).parent / "fixtures" / "patch-acceptance"
        source_path = fixture_root / "steam-news-four-patches.json"
        source_digest, _ = (fixture_root / "steam-news-four-patches.sha256").read_text().split()
        source_bytes = source_path.read_bytes()
        self.assertEqual(
            hashlib.sha256(source_bytes).hexdigest(),
            "291ba3b941df4758a2840554aed857c832afd580aad458df7b7b16e32cb20b5c",
        )
        self.assertEqual(hashlib.sha256(source_bytes).hexdigest(), source_digest)
        source_fixture = json.loads(source_bytes)
        self.assertEqual(
            source_fixture["source"]["response_sha256"],
            "0621e418c64c4fbabe92ce8008a43a7591dfadaafd39dfca868c52e781a0aaf8",
        )
        self.assertEqual(
            source_fixture["source"]["url"],
            "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid=1422450&count=20&maxlength=0&format=json",
        )
        catalog_path = fixture_root / "deadlock-catalog-inputs.json.gz"
        catalog_digest, _ = (fixture_root / "deadlock-catalog-inputs.sha256").read_text().split()
        catalog_bytes = catalog_path.read_bytes()
        self.assertEqual(hashlib.sha256(catalog_bytes).hexdigest(), catalog_digest)
        self.assertEqual(
            catalog_digest,
            "b7b12e4dafec35729768f438bb851f2f0fad5edf9ac92d60140ff74ebd75cf22",
        )
        catalog_fixture = json.loads(gzip.decompress(catalog_bytes))
        catalog_inputs = catalog_fixture["inputs"]
        expected_catalog_sources = {
            "heroes_english": {
                "url": "https://api.deadlock-api.com/v1/assets/heroes?language=english",
                "status": 200,
                "records": 65,
                "response_bytes": 1_911_878,
                "response_sha256": "b81ee8edc0584a18d5d7dda746f58092a2d88db42fa246131cd4ac474bd64c6a",
            },
            "items_english": {
                "url": "https://api.deadlock-api.com/v1/assets/items?language=english",
                "status": 200,
                "records": 782,
                "response_bytes": 6_458_065,
                "response_sha256": "ed77a501256bee9cdd862948ea736e89afad62392461bc3a978dc9d0bbc7a4f4",
            },
            "heroes_russian": {
                "url": "https://api.deadlock-api.com/v1/assets/heroes?language=russian",
                "status": 200,
                "records": 65,
                "response_bytes": 1_947_806,
                "response_sha256": "5779a7eac0ade8111d609206a412fe9e6d625e011befb187f34315e4f3efcaa7",
            },
            "items_russian": {
                "url": "https://api.deadlock-api.com/v1/assets/items?language=russian",
                "status": 200,
                "records": 782,
                "response_bytes": 6_913_589,
                "response_sha256": "1acea03299d595b4553732464a2fe1304ab4c962c8ce8468a990961ed39e6e65",
            },
            "icons": {
                "url": "https://api.deadlock-api.com/v1/assets/icons",
                "status": 200,
                "records": 73,
                "response_bytes": 187_335,
                "response_sha256": "9a88b66c9de031d761eaf19be7c76e0b096ae1f9e08d3b2ae4616b0e742afc4a",
            },
        }
        self.assertEqual(catalog_fixture["source"], expected_catalog_sources)
        provenance_path = fixture_root / "renderer-metadata-provenance.json"
        provenance = json.loads(provenance_path.read_text())
        self.assertEqual(provenance["schema"], 1)
        self.assertEqual(
            provenance["source_news_fixture"],
            {
                "path": "steam-news-four-patches.json",
                "sha256": "291ba3b941df4758a2840554aed857c832afd580aad458df7b7b16e32cb20b5c",
                "upstream_response_sha256": source_fixture["source"]["response_sha256"],
            },
        )
        self.assertEqual(
            provenance["catalog_fixture"],
            {
                "path": "deadlock-catalog-inputs.json.gz",
                "sha256": catalog_digest,
                "sources": expected_catalog_sources,
            },
        )
        expected_renderer_fixture_digests = {
            "1846651706696737": "7a238716e178175b455b6aed62d33c84f69ea600de252783da50c334b3a786d7",
            "1847920302240420": "28a058b28803db0021736ee0209775a3ef0635259e26ad124207db82b29302f8",
            "1848664643339845": "35cd23b9343e5ec76a26aa0709d18ebbef4a9dd581b2faa3c14b0b8a548416af",
            "1848664643342973": "7e17f338f1e74c96a537ba100e59aafab2c6c4f73ac9abd6a678a9828507315d",
        }
        self.assertEqual(provenance["renderer_fixtures"], expected_renderer_fixture_digests)
        self.assertEqual(
            provenance["metadata_updates"],
            [
                {
                    "patch_id": "1846651706696737",
                    "path": "sections[].abilities[].stats",
                    "ability_cards": 4,
                    "stat_rows": 67,
                    "unchanged_verified": [
                        "owners",
                        "ability names",
                        "descriptions",
                        "images",
                        "row counts",
                        "stats_pending",
                    ],
                },
                {
                    "patch_id": "1847920302240420",
                    "removed_fixture_node": {
                        "hero": "Paige",
                        "ability": "Bookwyrm",
                        "mode": None,
                        "changes": 0,
                    },
                    "retained_source_group": {
                        "hero": "Paige",
                        "ability": "Bookwyrm",
                        "mode": "Street Brawl",
                        "changes": 1,
                    },
                },
                {
                    "patch_id": "1847920302240420",
                    "path": "sections[hero=Silver].abilities[Go For The Throat]",
                    "source_feed_line_occurrences": 1,
                    "change_rows_moved": 1,
                    "catalog_class_name": "ability_werewolf_frenzy",
                    "icon_url": "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/werewolf/werewolf_bite.png",
                },
                {
                    "patch_id": "1847920302240420",
                    "path": "sections[hero=Solomon].abilities[Move Piece].display_name",
                    "catalog_class_name": "ability_chessmaster_movechesspiece",
                    "display_name": "Ход фигурой",
                },
                {
                    "patch_ids": [
                        "1846651706696737",
                        "1847920302240420",
                        "1848664643339845",
                        "1848664643342973",
                    ],
                    "path": "sections[kind=hero].hero_image_candidates",
                    "hero_sections": 47,
                    "remote_candidate_urls": 188,
                    "max_candidates": 4,
                    "primary_first_and_deduplicated": True,
                    "source": "actual bounded catalog fetch and public patch detail projection",
                },
            ],
        )
        expected_ids = {
            "1848664643342973",
            "1848664643339845",
            "1847920302240420",
            "1846651706696737",
        }
        self.assertEqual({str(row["gid"]) for row in source_fixture["items"]}, expected_ids)
        source_items_by_id = {
            str(row["gid"]): row for row in source_fixture["items"]
        }
        self.assertEqual(
            {name for name in catalog_fixture["source"]},
            {"heroes_english", "heroes_russian", "items_english", "items_russian", "icons"},
        )
        patch_ids = sorted(expected_ids, key=int)
        old_catalog = {"heroes": {}, "items": {}, "ranks": {}, "objectives": {}}
        old_values = {}
        for source_item in source_fixture["items"]:
            patch_id = str(source_item["gid"])
            compact_detail = patch_detail.structure_patch_detail(
                {
                    "id": patch_id,
                    "title": source_item["title"],
                    "published_at": home_content._source_datetime(source_item["date"]).isoformat(),
                    "url": source_item["url"],
                    "content": "",
                },
                old_catalog,
            )
            for version in range(6, 16):
                old_values[f"platform:home-content:patch:v{version}:{patch_id}"] = json.dumps(
                    compact_detail
                )
        old_catalog_keys = {
            "platform:home-content:patch-assets:v7": json.dumps(old_catalog),
            "platform:home-content:patch-assets:v8": json.dumps(old_catalog),
            "platform:home-content:patch-assets-stale:v1": json.dumps(old_catalog),
            "platform:home-content:patch-assets-stale:v2": json.dumps(old_catalog),
        }
        cache = _Cache({**old_values, **old_catalog_keys})
        requests: list[httpx.Request] = []
        steam_response_sizes: list[int] = []
        source_feed = json.dumps(
            {"appnews": {"newsitems": source_fixture["items"]}},
            ensure_ascii=False,
        ).encode()

        async def source_handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "api.steampowered.com":
                steam_response_sizes.append(len(source_feed))
                return httpx.Response(200, content=source_feed, request=request)
            if request.url.host == "api.deadlock-api.com":
                path = request.url.path
                if path == "/v1/assets/heroes":
                    language = request.url.params.get("language")
                    key = f"heroes_{language}"
                elif path == "/v1/assets/items":
                    language = request.url.params.get("language")
                    key = f"items_{language}"
                elif path == "/v1/assets/icons":
                    key = "icons"
                else:
                    key = ""
                if key in catalog_inputs:
                    return httpx.Response(
                        200,
                        json=catalog_inputs[key],
                        request=request,
                    )
            if request.url in {
                httpx.URL(home_content.DEADLOCK_HERO_INDEX_URL),
                httpx.URL(home_content.DEADLOCK_ITEM_INDEX_URL),
                httpx.URL(home_content.DEADLOCK_RANK_INDEX_URL),
            }:
                return httpx.Response(503, content=b"", request=request)
            raise AssertionError(
                f"unexpected patch-miss request host={request.url.host} path={request.url.path}"
            )

        bounded_client_type = patch_detail_security._BoundedPatchRefreshClient
        transport = httpx.MockTransport(source_handler)

        def bounded_client_factory(**kwargs):
            return bounded_client_type(transport=transport, **kwargs)

        tasks, registered, refresh_patch = _capture_refresh_tasks()
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "get_settings",
                return_value=PlatformSettings(_env_file=None),
            ),
            patch.object(
                patch_detail_security,
                "_BoundedPatchRefreshClient",
                side_effect=bounded_client_factory,
            ),
            patch.object(
                patch_detail_security,
                "ensure_patch_translation_records",
                AsyncMock(
                    return_value={
                        "registered": len(patch_ids),
                        "enqueued": len(patch_ids),
                        "enqueue_failures": 0,
                    }
                ),
            ) as register,
            patch.object(
                patch_detail_security,
                "apply_cached_patch_translation",
                AsyncMock(side_effect=lambda detail: detail),
            ) as apply_cached,
            refresh_patch,
        ):
            result = await patch_detail_security._cached_patch_source(patch_ids[0])
            await asyncio.wait_for(registered.wait(), timeout=_SOURCE_REPLAY_TIMEOUT_SECONDS)
            await _await_refresh_tasks(tasks, timeout=_SOURCE_REPLAY_TIMEOUT_SECONDS)
            refreshed = {
                patch_id: await patch_detail_security._cached_patch_source(patch_id)
                for patch_id in patch_ids
            }
            public_details = {
                patch_id: await patch_detail_security.get_patch_detail(patch_id)
                for patch_id in patch_ids
            }

        self.assertIsNone(result)
        self.assertEqual(len(tasks), 1)
        self.assertTrue(all(isinstance(detail, dict) for detail in refreshed.values()))
        self.assertEqual(len(steam_response_sizes), 1)
        self.assertLessEqual(
            steam_response_sizes[0],
            patch_detail_security.PATCH_MISS_RESPONSE_MAX_BYTES,
        )
        steam_requests = [
            request for request in requests
            if request.url.host == "api.steampowered.com"
        ]
        self.assertEqual(len(steam_requests), 1)
        self.assertEqual(steam_requests[0].url.params.get("maxlength"), "0")
        self.assertEqual(
            steam_requests[0].url.params.get("appid"),
            str(home_content.DEADLOCK_APP_ID),
        )
        self.assertEqual(
            {
                (request.url.path, request.url.params.get("language"))
                for request in requests
                if request.url.host == "api.deadlock-api.com"
                and request.url.path in {"/v1/assets/heroes", "/v1/assets/items"}
            },
            {
                ("/v1/assets/heroes", "english"),
                ("/v1/assets/heroes", "russian"),
                ("/v1/assets/items", "english"),
                ("/v1/assets/items", "russian"),
            },
        )
        self.assertNotIn("platform:home-content:patch-assets:v7", cache.get_calls)
        self.assertNotIn("platform:home-content:patch-assets:v8", cache.get_calls)
        self.assertNotIn("platform:home-content:patch-assets-stale:v1", cache.get_calls)
        self.assertNotIn("platform:home-content:patch-assets-stale:v2", cache.get_calls)
        for old_key, old_value in old_catalog_keys.items():
            self.assertEqual(cache.values[old_key], old_value)
        self.assertIn(home_content.PATCH_ASSET_CATALOG_KEY, cache.values)
        self.assertIn(home_content.PATCH_ASSET_CATALOG_STALE_KEY, cache.values)
        current_catalog = json.loads(cache.values[home_content.PATCH_ASSET_CATALOG_KEY])
        stale_catalog = json.loads(cache.values[home_content.PATCH_ASSET_CATALOG_STALE_KEY])
        self.assertEqual(current_catalog, stale_catalog)
        self.assertEqual(len(requests), 9)
        self.assertEqual(
            current_catalog["objectives"]["mid_boss"]["icon_url"],
            catalog_inputs["icons"]["minimap"]["boss_marker_neutral_psd.png"],
        )
        self.assertEqual(
            current_catalog["heroes"]["solomon"]["abilities"]["move piece"]["icon_url"],
            next(
                item["image"]
                for item in catalog_inputs["items_english"]
                if item.get("class_name") == "ability_chessmaster_movechesspiece"
            ),
        )
        for old_key, old_value in old_values.items():
            self.assertEqual(cache.values[old_key], old_value)
            self.assertNotIn(old_key, cache.get_calls)

        register.assert_awaited_once()
        registered_patches = register.await_args.args[0]
        self.assertEqual(set(registered_patches), set(patch_ids))
        self.assertEqual(apply_cached.await_count, len(patch_ids))
        renderer_fixture_root = (
            Path(__file__).parents[1]
            / "apps"
            / "platform_web"
            / "tests"
            / "fixtures"
            / "patch-details"
        )

        def structural_projection(detail: dict) -> dict:
            sections = []
            for section in detail["sections"]:
                section_metadata = {
                    key: section.get(key)
                    for key in (
                        "kind",
                        "title",
                        "hero_name",
                        "hero_display_name",
                        "hero_presentation_kind",
                        "mode_name",
                        "hero_image_url",
                        "hero_image_candidates",
                        "hero_description",
                        "hero_description_requires_translation",
                        "hero_description_russian_metadata_available",
                        "hero_metadata_pending",
                        "item_name",
                        "item_category",
                        "item_icon_url",
                        "objective_key",
                        "objective_icon_url",
                    )
                }
                section_metadata["changes_count"] = len(section.get("changes") or [])
                section_metadata["abilities"] = []
                for ability in section.get("abilities") or []:
                    ability_metadata = {
                        key: ability.get(key)
                        for key in (
                            "name",
                            "display_name",
                            "description",
                            "description_requires_translation",
                            "russian_metadata_available",
                            "presentation_kind",
                            "stats",
                            "stats_pending",
                            "icon_url",
                        )
                    }
                    ability_metadata["changes_count"] = len(ability.get("changes") or [])
                    section_metadata["abilities"].append(ability_metadata)
                sections.append(section_metadata)
            return {
                "id": detail["id"],
                "title": detail["title"],
                "sections": sections,
            }

        for patch_id in patch_ids:
            cached_key = home_content.PATCH_DETAIL_KEY_PREFIX + patch_id
            self.assertIn(cached_key, cache.values)
            updated_detail = json.loads(cache.values[cached_key])
            self.assertEqual(
                translation_source_hash(registered_patches[patch_id]),
                translation_source_hash(updated_detail),
            )
            if patch_id == "1848664643339845":
                baba_catalog = current_catalog["heroes"]["baba"]
                self.assertNotIn("melee", baba_catalog["abilities"])
                baba_section = next(
                    section
                    for section in updated_detail["sections"]
                    if section.get("kind") == "hero"
                    and section.get("hero_name") == "Baba"
                )
                self.assertTrue(
                    any("Melee damage per boon reduced by 10%" == change for change in baba_section["changes"])
                )
                self.assertFalse(
                    any(ability.get("name") == "Melee" for ability in baba_section["abilities"])
                )
            if patch_id == "1847920302240420":
                silver_catalog = current_catalog["heroes"]["silver"]
                go_for_the_throat_catalog = silver_catalog["abilities"]["go for the throat"]
                source_go_rows = [
                    line
                    for line in home_content._patch_lines(updated_detail["content"])
                    if line.casefold().startswith("silver: go for the throat")
                ]
                self.assertEqual(len(source_go_rows), 1)
                source_go_change = source_go_rows[0].split(":", 1)[1].strip()
                ability_prefix = "Go For The Throat - "
                self.assertTrue(source_go_change.startswith(ability_prefix))
                source_ability_change = source_go_change[len(ability_prefix):]
                silver_section = next(
                    section
                    for section in updated_detail["sections"]
                    if section.get("kind") == "hero"
                    and section.get("hero_name") == "Silver"
                    and not section.get("mode_name")
                )
                go_for_the_throat = [
                    ability
                    for ability in silver_section["abilities"]
                    if ability.get("name") == "Go For The Throat"
                ]
                self.assertEqual(len(go_for_the_throat), 1)
                self.assertEqual(go_for_the_throat[0]["changes"], [source_ability_change])
                self.assertEqual(
                    go_for_the_throat[0]["icon_url"],
                    next(
                        item["image"]
                        for item in catalog_inputs["items_english"]
                        if item.get("class_name") == "ability_werewolf_frenzy"
                    ),
                )
                self.assertTrue(go_for_the_throat_catalog["patch_only"])
                self.assertEqual(
                    go_for_the_throat[0]["display_name"],
                    next(
                        item["name"]
                        for item in catalog_inputs["items_russian"]
                        if item.get("class_name") == "ability_werewolf_frenzy"
                    ),
                )
                self.assertFalse(
                    any("Go For The Throat" in change for change in silver_section["changes"])
                )
                normal_nurse = next(
                    section
                    for section in updated_detail["sections"]
                    if section.get("kind") == "hero"
                    and section.get("hero_name") == "Nurse Harrow"
                    and not section.get("mode_name")
                )
                self.assertEqual(
                    [(ability["name"], len(ability["changes"])) for ability in normal_nurse["abilities"]],
                    [
                        ("Leech Therapy", 3),
                        ("Wretched Vapors", 1),
                        ("Healing Needles", 3),
                        ("Lurking Terrors", 3),
                    ],
                )
                self.assertFalse(
                    any(
                        change.casefold().startswith(("nurse:", "nurse harrow:"))
                        for section in updated_detail["sections"]
                        if section.get("kind") == "general"
                        for change in section.get("changes", [])
                    )
                )
                mid_boss = next(
                    section
                    for section in updated_detail["sections"]
                    if section.get("objective_key") == "mid_boss"
                )
                self.assertEqual(len(mid_boss["changes"]), 11)
                self.assertFalse(
                    any(change.casefold().startswith(("mid boss:", "mid-boss:")) for change in mid_boss["changes"])
                )
                self.assertTrue(
                    any("only 50% as effective" in change.casefold() for change in mid_boss["changes"])
                )
                mode_sections = [
                    section
                    for section in updated_detail["sections"]
                    if section.get("mode_name") == "Street Brawl"
                ]
                mode_rows = [
                    change
                    for section in mode_sections
                    for change in section.get("changes", [])
                    + [
                        change
                        for ability in section.get("abilities", [])
                        for change in ability.get("changes", [])
                    ]
                ]
                self.assertEqual(len(mode_rows), 13)
                self.assertEqual(len(set(mode_rows)), 13)
                self.assertFalse(any("Street Brawl:" in change for change in mode_rows))
                ordinary_paige = next(
                    section
                    for section in updated_detail["sections"]
                    if section.get("kind") == "hero"
                    and section.get("hero_name") == "Paige"
                    and not section.get("mode_name")
                )
                self.assertFalse(
                    any(ability.get("name") == "Bookwyrm" for ability in ordinary_paige["abilities"])
                )
                mode_paige = next(
                    section
                    for section in mode_sections
                    if section.get("hero_name") == "Paige"
                )
                bookwyrm = [
                    ability
                    for ability in mode_paige["abilities"]
                    if ability.get("name") == "Bookwyrm"
                ]
                self.assertEqual(len(bookwyrm), 1)
                self.assertEqual(len(bookwyrm[0]["changes"]), 1)
                normal_solomon = next(
                    section
                    for section in updated_detail["sections"]
                    if section.get("kind") == "hero"
                    and section.get("hero_name") == "Solomon"
                    and not section.get("mode_name")
                )
                move_piece = [
                    ability
                    for ability in normal_solomon["abilities"]
                    if ability.get("name") == "Move Piece"
                ]
                self.assertEqual(len(move_piece), 1)
                self.assertEqual(len(move_piece[0]["changes"]), 2)
                self.assertEqual(
                    move_piece[0]["display_name"],
                    next(
                        item["name"]
                        for item in catalog_inputs["items_russian"]
                        if item.get("class_name") == "ability_chessmaster_movechesspiece"
                    ),
                )
                self.assertTrue(any("50m" in change and "65m" in change for change in move_piece[0]["changes"]))
                self.assertTrue(any("improved placement" in change.casefold() for change in move_piece[0]["changes"]))
            if patch_id == "1846651706696737":
                nurse_intro = next(
                    section
                    for section in updated_detail["sections"]
                    if section.get("kind") == "hero"
                    and section.get("hero_name") == "Nurse Harrow"
                )
                self.assertEqual(len(nurse_intro["abilities"]), 4)
                self.assertTrue(
                    all(ability.get("description") for ability in nurse_intro["abilities"])
                )
                self.assertEqual(
                    sum(len(ability.get("stats") or []) for ability in nurse_intro["abilities"]),
                    67,
                )
                self.assertFalse(
                    any(ability.get("stats_pending") for ability in nurse_intro["abilities"])
                )
            self.assertEqual(
                updated_detail["content"],
                home_content._plain_text(
                    source_items_by_id[patch_id]["contents"],
                    max_length=30000,
                ),
            )
            self.assertIsInstance(public_details[patch_id], dict)
            expected_path = renderer_fixture_root / f"{patch_id}.json"
            expected_bytes = expected_path.read_bytes()
            self.assertEqual(
                hashlib.sha256(expected_bytes).hexdigest(),
                expected_renderer_fixture_digests[patch_id],
            )
            expected = json.loads(expected_bytes)
            generated = public_details[patch_id]
            generated_wire = PatchDetailResponse.model_validate(generated).model_dump(mode="json")
            generated_projection = structural_projection(generated_wire)
            expected_projection = structural_projection(expected)
            if generated_projection != expected_projection:
                differences = []
                for key in ("id", "title"):
                    if generated_projection[key] != expected_projection[key]:
                        differences.append(f"top-level.{key}")
                if (
                    len(generated_projection["sections"])
                    != len(expected_projection["sections"])
                ):
                    differences.append("top-level.sections_count")
                for section_index, (generated_section, expected_section) in enumerate(
                    zip(
                        generated_projection["sections"],
                        expected_projection["sections"],
                    )
                ):
                    if (
                        len(generated_section["abilities"])
                        != len(expected_section["abilities"])
                    ):
                        differences.append(f"section[{section_index}].abilities_count")
                    for key in generated_section:
                        if key == "abilities":
                            for ability_index, (generated_ability, expected_ability) in enumerate(
                                zip(generated_section[key], expected_section[key])
                            ):
                                for ability_key in generated_ability:
                                    if generated_ability[ability_key] != expected_ability[ability_key]:
                                        differences.append(
                                            f"section[{section_index}].ability[{ability_index}].{ability_key}"
                                        )
                        elif generated_section[key] != expected_section[key]:
                            differences.append(f"section[{section_index}].{key}")
                self.fail(f"{patch_id} renderer projection fields differ: {differences[:24]}")
            rendered_changes = "\n".join(
                change
                for section in generated["sections"]
                for change in (
                    section.get("changes", [])
                    + [
                        change
                        for ability in section.get("abilities", [])
                        for change in ability.get("changes", [])
                    ]
                )
            )
            self.assertNotRegex(
                rendered_changes,
                r"STEAM_CLAN_LOC_IMAGE|clan\.fastly\.steamstatic\.com|\[\s*\/?\s*(?:img|url)\b",
            )
        self.assertIn(
            patch_detail_security.PATCH_MISS_NEGATIVE_KEY_PREFIX + patch_ids[0],
            cache.values,
        )
        self.assertIn(patch_detail_security.PATCH_MISS_REFRESH_GATE_KEY, cache.values)

    async def test_negative_cache_prevents_repeat_refresh_for_same_patch(self) -> None:
        cache = _Cache()
        tasks, registered, refresh_patch = _capture_refresh_tasks()
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "_refresh_patch_cache_after_miss",
                AsyncMock(),
            ) as refresh,
            refresh_patch,
        ):
            await patch_detail_security._cached_patch_source("999")
            await patch_detail_security._cached_patch_source("999")
            await asyncio.wait_for(registered.wait(), timeout=_REFRESH_OBSERVER_TIMEOUT_SECONDS)
            await _await_refresh_tasks(tasks)

        self.assertEqual(len(tasks), 1)
        refresh.assert_awaited_once()

    async def test_distinct_unknown_ids_share_one_global_refresh_gate(self) -> None:
        cache = _Cache()
        tasks, registered, refresh_patch = _capture_refresh_tasks()
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "_refresh_patch_cache_after_miss",
                AsyncMock(),
            ) as refresh,
            refresh_patch,
        ):
            await patch_detail_security._cached_patch_source("901")
            await patch_detail_security._cached_patch_source("902")
            await asyncio.wait_for(registered.wait(), timeout=_REFRESH_OBSERVER_TIMEOUT_SECONDS)
            await _await_refresh_tasks(tasks)

        self.assertEqual(len(tasks), 1)
        refresh.assert_awaited_once()
        negative_keys = {
            key
            for key in cache.values
            if key.startswith(patch_detail_security.PATCH_MISS_NEGATIVE_KEY_PREFIX)
        }
        self.assertEqual(
            negative_keys,
            {
                patch_detail_security.PATCH_MISS_NEGATIVE_KEY_PREFIX + "901",
                patch_detail_security.PATCH_MISS_NEGATIVE_KEY_PREFIX + "902",
            },
        )

    async def test_invalid_patch_id_does_not_touch_cache(self) -> None:
        with patch.object(patch_detail_security, "redis_client") as redis_factory:
            result = await patch_detail_security._cached_patch_source("not-a-patch")

        self.assertIsNone(result)
        redis_factory.assert_not_called()

    async def test_existing_structured_patch_does_not_force_asset_refresh(self) -> None:
        detail = {
            "id": "123",
            "title": "Patch",
            "content": "Map changed",
            "sections": [{"kind": "general", "title": "General", "changes": []}],
        }
        cache = _Cache(
            {
                home_content.PATCH_DETAIL_KEY_PREFIX + "123": json.dumps(detail),
            }
        )
        translated = {**detail, "translated": True}
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "apply_cached_patch_translation",
                AsyncMock(return_value=translated),
            ) as translate,
        ):
            result = await patch_detail_security.get_patch_detail("123")

        self.assertEqual(result, translated)
        translate.assert_awaited_once_with(detail)

    async def test_legacy_compact_patch_with_warm_catalog_schedules_full_refresh(self) -> None:
        prefixes = (
            "platform:home-content:patch:v6:",
            "platform:home-content:patch:v7:",
            "platform:home-content:patch:v8:",
            "platform:home-content:patch:v9:",
            "platform:home-content:patch:v10:",
            "platform:home-content:patch:v11:",
            "platform:home-content:patch:v12:",
            "platform:home-content:patch:v13:",
        )
        catalog = {"heroes": {}, "items": {}, "ranks": {}, "objectives": {}}
        for index, prefix in enumerate(prefixes):
            with self.subTest(prefix=prefix):
                patch_id = str(987654330 + index)
                legacy_key = prefix + patch_id
                legacy_detail = {"id": patch_id, "content": "General: compact old content"}
                cache = _Cache(
                    {
                        legacy_key: json.dumps(legacy_detail),
                        home_content.PATCH_ASSET_CATALOG_KEY: json.dumps(catalog),
                    }
                )
                tasks, registered, refresh_patch = _capture_refresh_tasks()
                with (
                    patch.object(patch_detail_security, "redis_client", return_value=cache),
                    refresh_patch,
                    patch.object(
                        patch_detail_security,
                        "_refresh_patch_cache_after_miss",
                        AsyncMock(),
                    ) as refresh_full,
                ):
                    results = await asyncio.gather(
                        patch_detail_security._cached_patch_source(patch_id),
                        patch_detail_security._cached_patch_source(patch_id),
                    )
                    await asyncio.wait_for(
                        registered.wait(), timeout=_REFRESH_OBSERVER_TIMEOUT_SECONDS
                    )
                    await _await_refresh_tasks(tasks)

                self.assertEqual(results, [None, None])
                self.assertEqual(len(tasks), 1)
                refresh_full.assert_awaited_once()
                self.assertEqual(cache.values[legacy_key], json.dumps(legacy_detail))
                self.assertNotIn(legacy_key, cache.get_calls)

    async def test_legacy_compact_patch_with_cold_catalog_schedules_full_refresh(self) -> None:
        patch_id = "987654322"
        legacy_key = "platform:home-content:patch:v6:" + patch_id
        legacy_detail = {"id": patch_id, "content": "General: compact old content"}
        cache = _Cache({legacy_key: json.dumps(legacy_detail)})
        tasks, registered, refresh_patch = _capture_refresh_tasks()
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            refresh_patch,
            patch.object(
                patch_detail_security,
                "_refresh_patch_cache_after_miss",
                AsyncMock(),
            ) as refresh_full,
        ):
            results = await asyncio.gather(
                patch_detail_security._cached_patch_source(patch_id),
                patch_detail_security._cached_patch_source(patch_id),
            )
            await asyncio.wait_for(registered.wait(), timeout=_REFRESH_OBSERVER_TIMEOUT_SECONDS)
            await _await_refresh_tasks(tasks)

        self.assertEqual(results, [None, None])
        self.assertEqual(len(tasks), 1)
        refresh_full.assert_awaited_once()
        self.assertEqual(cache.values[legacy_key], json.dumps(legacy_detail))
        self.assertNotIn(legacy_key, cache.get_calls)

    async def test_patch_miss_refresh_registers_translations_after_cache_publish(self) -> None:
        raw_detail = {
            "id": "123",
            "title": "Patch",
            "content": "- Map speed reduced",
        }
        cache = _Cache()
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "ensure_patch_translation_records",
                AsyncMock(return_value={"registered": 1, "enqueued": 1, "enqueue_failures": 0}),
            ) as register,
        ):
            await patch_detail_security._publish_patch_refresh(
                {"123": raw_detail},
                {"heroes": {}, "items": {}, "ranks": {}, "objectives": {}},
                publish_catalog=False,
            )

        register.assert_awaited_once()
        registered_details = register.await_args.args[0]
        self.assertIn("123", registered_details)
        self.assertEqual(registered_details["123"]["id"], "123")
        self.assertTrue(registered_details["123"]["sections"])

    async def test_bounded_client_does_not_follow_redirects(self) -> None:
        requests: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            return httpx.Response(
                302,
                headers={"Location": "https://redirected.example/patch"},
                request=request,
            )

        async with patch_detail_security._BoundedPatchRefreshClient(
            transport=httpx.MockTransport(handler),
            max_response_bytes=64,
        ) as client:
            response = await client.get("https://api.steampowered.com/example")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(requests, ["https://api.steampowered.com/example"])

    async def test_bounded_client_rejects_oversized_response(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 65, request=request)

        async with patch_detail_security._BoundedPatchRefreshClient(
            transport=httpx.MockTransport(handler),
            max_response_bytes=64,
        ) as client:
            with self.assertRaisesRegex(ValueError, "byte limit"):
                await client.get("https://api.steampowered.com/example")


if __name__ == "__main__":
    unittest.main()
