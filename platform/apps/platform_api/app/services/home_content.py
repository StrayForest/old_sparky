from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
import json
import logging
import re
from secrets import token_urlsafe
from typing import Any
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

import httpx

from python_packages.platform_domain.deadlock.constants import POOL_LIST, RANKS
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.redis import redis_client


STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
DEADLOCK_APP_ID = 1422450
YOUTUBE_CHANNEL_ID = "UCNoxEIMEAbGHLpn2IGKqlCQ"
YOUTUBE_FEED_URL = (
    "https://www.youtube.com/feeds/videos.xml?channel_id=" + YOUTUBE_CHANNEL_ID
)
YOUTUBE_VIDEOS_URL = "https://www.youtube.com/@deadlockOldSparky/videos"
DEADLOCK_HERO_INDEX_URL = "https://deadlock.io/api/v1/heroes.json"
DEADLOCK_ITEM_INDEX_URL = "https://deadlock.io/api/v1/items.json"
DEADLOCK_RANK_INDEX_URL = "https://api.deadlock-api.com/v1/assets/ranks"
DEADLOCK_ICON_INDEX_URL = "https://api.deadlock-api.com/v1/assets/icons"
DEADLOCK_ASSET_BASE_URL = "https://deadlock.io"
HOME_CONTENT_KEY = "platform:home-content:v4"
HOME_CONTENT_STALE_KEY = "platform:home-content:stale:v4"
HOME_CONTENT_LOCK_KEY = "platform:home-content:refresh-lock:v4"
PATCH_DETAIL_KEY_PREFIX = "platform:home-content:patch:v6:"
PATCH_ASSET_CATALOG_KEY = "platform:home-content:patch-assets:v4"
PATCH_DETAIL_TTL_SECONDS = 30 * 24 * 60 * 60
PATCH_ASSET_CATALOG_TTL_SECONDS = 6 * 60 * 60
REFRESH_LOCK_SECONDS = 30
HOME_PATCH_LIMIT = 4
HOME_VIDEO_LIMIT = 4
SOURCE_DATE_MAX_FUTURE_SKEW = timedelta(days=1)
STEAM_NEWS_HOST_SUFFIXES = (
    "steampowered.com",
    "steamcommunity.com",
    "steamstatic.com",
    "akamaihd.net",
)
YOUTUBE_IMAGE_HOSTS = ("i2.ytimg.com", "i3.ytimg.com")
DEADLOCK_API_ASSET_HOST_SUFFIXES = ("assets-bucket.deadlock-api.com",)
OBJECTIVE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "urn": {
        "title": "Urn",
        "icon_key": "soul_jar_marker_psd.png",
        "aliases": (
            "spirit urn",
            "the urn runner",
            "urn runner",
            "urn running",
            "the urn",
            "urn",
        ),
    },
    "unstable_rift": {
        "title": "Unstable Rift",
        "icon_key": "minimap_icon_koth.png",
        "aliases": (
            "the unstable rift",
            "unstable rift zone",
            "unstable rift",
            "rift troopers",
            "rift trooper",
            "king of the hill objective",
            "king of the hill",
        ),
    },
}
OBJECTIVE_PREFIX_ALIASES = tuple(
    sorted(
        (
            (alias, objective_key)
            for objective_key, definition in OBJECTIVE_DEFINITIONS.items()
            for alias in definition["aliases"]
        ),
        key=lambda value: len(value[0]),
        reverse=True,
    )
)
PATCH_SCOPE_HEADINGS = {
    "urn / king of the hill": "objective",
    "general": "general",
    "items": "items",
    "heroes": "heroes",
}
logger = logging.getLogger(__name__)
STEAM_CLAN_IMAGE_RE = re.compile(
    r"\{STEAM_CLAN(?:_LOC)?_IMAGE\}/(?P<clan_id>\d+)/(?P<asset>[a-fA-F0-9]{32,64}\.(?:avif|gif|jpe?g|png|webp))",
    re.IGNORECASE,
)
STEAM_CLAN_IMAGE_BASE_URL = "https://clan.fastly.steamstatic.com/images"
LOCK_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img":
            source = next((value for name, value in attrs if name == "src" and value), None)
            if source and (
                STEAM_CLAN_IMAGE_RE.fullmatch(source)
                or source.startswith(f"{STEAM_CLAN_IMAGE_BASE_URL}/")
            ):
                self.parts.append(f"\n{source}\n")
        if tag in {"br", "p", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")


def _plain_text(value: str, *, max_length: int) -> str:
    extractor = _TextExtractor()
    extractor.feed(unescape(value.replace("\\-", "-")))
    lines = [" ".join(line.split()) for line in "".join(extractor.parts).splitlines()]
    return "\n".join(line for line in lines if line)[:max_length].strip()


def _excerpt(value: str, *, max_length: int = 260) -> str:
    compact = " ".join(value.split())
    if len(compact) <= max_length:
        return compact
    return compact[: max_length - 1].rstrip() + "…"


def _source_datetime(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(int(value), tz=UTC)
    else:
        normalized = str(value or "").strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            raise ValueError("External content timestamp must include a timezone.")
        parsed = parsed.astimezone(UTC)
    if parsed.year < 2010 or parsed > datetime.now(UTC) + SOURCE_DATE_MAX_FUTURE_SKEW:
        raise ValueError("External content timestamp is outside the accepted range.")
    return parsed


def _https_url(
    value: Any,
    *,
    host_suffixes: tuple[str, ...] | None = None,
    hosts: tuple[str, ...] | None = None,
) -> str:
    normalized = str(value or "").strip()
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise ValueError("External content URL must use HTTPS.")
    if host_suffixes is not None and not any(
        host == suffix or host.endswith(f".{suffix}") for suffix in host_suffixes
    ):
        raise ValueError("External content URL host is not allowed.")
    if hosts is not None and host not in hosts:
        raise ValueError("External content URL host is not allowed.")
    return normalized[:1000]


def _patch_lines(content: str) -> list[str]:
    expanded = STEAM_CLAN_IMAGE_RE.sub(
        lambda match: (
            f"\n{STEAM_CLAN_IMAGE_BASE_URL}/{match.group('clan_id')}/{match.group('asset')}\n"
        ),
        content,
    )
    expanded = re.sub(r"(?<=\S)-\s+(?=[A-Z])", "\n- ", expanded)
    return [
        line.strip().removeprefix("-").strip()
        for line in expanded.splitlines()
        if line.strip().removeprefix("-").strip()
    ]


def _change_chunks(value: str, *, max_length: int = 1000) -> list[str]:
    remaining = value.strip()
    chunks: list[str] = []
    while len(remaining) > max_length:
        split_at = max(
            remaining.rfind(marker, 0, max_length + 1) + 1
            for marker in (". ", "? ", "! ", "; ", ": ")
        )
        if split_at < max_length // 2:
            split_at = remaining.rfind(" ", 0, max_length + 1)
        if split_at <= 0:
            split_at = max_length
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _is_official_deadlock_announcement(item: dict[str, Any]) -> bool:
    feed_label = str(item.get("feedlabel") or "").strip().casefold()
    source_path = urlparse(str(item.get("url") or "")).path.casefold()
    return (
        feed_label == "community announcements"
        or "/steam_community_announcements/" in source_path
    )


def _asset_url(public_path: Any) -> str | None:
    normalized = str(public_path or "").strip()
    return f"{DEADLOCK_ASSET_BASE_URL}{normalized}" if normalized.startswith("/assets/") else None


def _strip_named_change_prefix(change: str, name: str) -> str:
    suffix = change[len(name):] if change[:len(name)].casefold() == name.casefold() else change
    if suffix == change or (suffix and suffix[0] not in " \t:-–—"):
        return change.strip()
    normalized = suffix.lstrip(" \t:-–—").strip()
    if not normalized:
        return change.strip()
    for index, character in enumerate(normalized):
        if character.isalpha():
            return normalized[:index] + character.upper() + normalized[index + 1:]
    return normalized


def _patch_scope_heading(line: str) -> str | None:
    normalized = " ".join(line.split()).casefold()
    if len(normalized) < 2 or normalized[0] != "[" or normalized[-1] != "]":
        return None
    return PATCH_SCOPE_HEADINGS.get(normalized[1:-1].strip())


def _has_whole_token_anchor(line: str, alias: str) -> bool:
    return re.search(
        rf"(?<![a-z0-9_]){re.escape(alias)}(?![a-z0-9_])",
        line,
        re.IGNORECASE,
    ) is not None


def _objective_scope_key(line: str) -> str | None:
    matches = {
        objective_key
        for objective_key, definition in OBJECTIVE_DEFINITIONS.items()
        if any(
            _has_whole_token_anchor(line, alias)
            for alias in definition["aliases"]
        )
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _objective_prefix_match(line: str) -> tuple[str, str] | None:
    lowered = line.casefold()
    for alias, objective_key in OBJECTIVE_PREFIX_ALIASES:
        if not lowered.startswith(alias):
            continue
        suffix = line[len(alias):]
        if not suffix or suffix[0] in " \t\r\n:-–—":
            return objective_key, alias
    return None


def _strip_objective_prefix(line: str, alias: str) -> str:
    return _strip_named_change_prefix(line, alias)


def _objective_catalog(
    icon_payload: dict[str, Any] | None,
    previous_catalog: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    previous_objectives = (
        previous_catalog.get("objectives")
        if isinstance(previous_catalog, dict)
        and isinstance(previous_catalog.get("objectives"), dict)
        else {}
    )
    minimap_icons = (
        icon_payload.get("minimap")
        if isinstance(icon_payload, dict) and isinstance(icon_payload.get("minimap"), dict)
        else {}
    )
    objectives: dict[str, dict[str, Any]] = {}
    for objective_key, definition in OBJECTIVE_DEFINITIONS.items():
        icon_url: str | None = None
        previous_objective = previous_objectives.get(objective_key)
        if isinstance(previous_objective, dict) and previous_objective.get("icon_url"):
            try:
                icon_url = _https_url(
                    previous_objective["icon_url"],
                    host_suffixes=DEADLOCK_API_ASSET_HOST_SUFFIXES,
                )
            except ValueError:
                pass
        candidate = minimap_icons.get(definition["icon_key"])
        if candidate:
            try:
                icon_url = _https_url(
                    candidate,
                    host_suffixes=DEADLOCK_API_ASSET_HOST_SUFFIXES,
                )
            except ValueError:
                pass
        objectives[objective_key] = {
            "name": definition["title"],
            "icon_url": icon_url,
        }
    return objectives


def _build_asset_catalog(
    hero_payload: dict[str, Any],
    item_payload: dict[str, Any],
    rank_payload: list[dict[str, Any]] | None = None,
    *,
    icon_payload: dict[str, Any] | None = None,
    previous_catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    heroes: dict[str, Any] = {}
    for hero in hero_payload.get("heroes") or []:
        display_name = str((hero.get("displayName") or {}).get("english") or "").strip()
        if not display_name:
            continue
        canonical_name = "The Doorman" if display_name == "Doorman" else display_name
        abilities: dict[str, Any] = {}
        for ability in hero.get("abilities") or []:
            ability_name = str((ability.get("displayName") or {}).get("english") or "").strip()
            if not ability_name or ability_name.startswith("ability_"):
                continue
            icon = _asset_url(((ability.get("assets") or {}).get("icon") or {}).get("publicPath"))
            abilities[ability_name.lower()] = {"name": ability_name, "icon_url": icon}
        hero_value = {
            "name": canonical_name,
            "slug": str(hero.get("slug") or "").strip(),
            "icon_url": _asset_url(
                (((hero.get("assets") or {}).get("card") or {}).get("publicPath"))
                or (((hero.get("assets") or {}).get("portrait") or {}).get("publicPath"))
                or (((hero.get("assets") or {}).get("icon") or {}).get("publicPath"))
            ),
            "abilities": abilities,
        }
        heroes[display_name.lower()] = hero_value
        if display_name.casefold() in {"doorman", "the doorman"}:
            heroes["doorman"] = hero_value
            heroes["the doorman"] = hero_value

    items: dict[str, Any] = {}
    for item in item_payload.get("items") or []:
        display_name = str((item.get("displayName") or {}).get("english") or "").strip()
        category = str((item.get("shop") or {}).get("category") or "").strip().lower()
        if not display_name or category not in {"weapon", "vitality", "spirit"}:
            continue
        assets = item.get("assets") or {}
        icon_asset = assets.get("shopIcon") or assets.get("icon") or {}
        items[display_name.lower()] = {
            "name": display_name,
            "slug": str(item.get("slug") or "").strip(),
            "category": category,
            "cost": (item.get("shop") or {}).get("cost"),
            "icon_url": _asset_url(icon_asset.get("publicPath")),
        }
    ranks: dict[str, dict[str, Any]] = {}
    rank_names_by_tier = {
        len(RANKS) - index: rank_name
        for index, rank_name in enumerate(RANKS)
    }
    for rank in rank_payload or []:
        tier = rank.get("tier")
        if not isinstance(tier, int) or tier not in rank_names_by_tier:
            continue
        rank_name = rank_names_by_tier[tier]
        image_url = ((rank.get("images") or {}).get("large_webp"))
        if not image_url:
            continue
        try:
            safe_image_url = _https_url(
                image_url,
                host_suffixes=DEADLOCK_API_ASSET_HOST_SUFFIXES,
            )
        except ValueError:
            continue
        ranks[rank_name.casefold()] = {
            "name": rank_name,
            "tier": tier,
            "icon_url": safe_image_url,
        }
    return {
        "heroes": heroes,
        "items": items,
        "ranks": ranks,
        "objectives": _objective_catalog(icon_payload, previous_catalog),
    }


async def _fetch_deadlock_asset_catalog(
    client: httpx.AsyncClient,
    *,
    previous_catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hero_response, item_response = await asyncio.gather(
        client.get(DEADLOCK_HERO_INDEX_URL),
        client.get(DEADLOCK_ITEM_INDEX_URL),
    )
    hero_response.raise_for_status()
    item_response.raise_for_status()
    rank_result, icon_result = await asyncio.gather(
        client.get(DEADLOCK_RANK_INDEX_URL),
        client.get(DEADLOCK_ICON_INDEX_URL),
        return_exceptions=True,
    )
    rank_payload: list[dict[str, Any]] = []
    if isinstance(rank_result, Exception):
        logger.warning("deadlock_asset_refresh_failed source=ranks")
    else:
        try:
            rank_result.raise_for_status()
            decoded_rank_payload = rank_result.json()
            if isinstance(decoded_rank_payload, list):
                rank_payload = decoded_rank_payload
        except Exception:  # noqa: BLE001
            logger.warning("deadlock_asset_refresh_failed source=ranks")
    icon_payload: dict[str, Any] | None = None
    if isinstance(icon_result, Exception):
        logger.warning("deadlock_asset_refresh_failed source=icons")
    else:
        try:
            icon_result.raise_for_status()
            decoded_icon_payload = icon_result.json()
            if isinstance(decoded_icon_payload, dict):
                icon_payload = decoded_icon_payload
        except Exception:  # noqa: BLE001
            logger.warning("deadlock_asset_refresh_failed source=icons")
    return _build_asset_catalog(
        hero_response.json(),
        item_response.json(),
        rank_payload,
        icon_payload=icon_payload,
        previous_catalog=previous_catalog,
    )


async def _cached_deadlock_asset_catalog() -> dict[str, Any] | None:
    cache = redis_client()
    try:
        catalog = await _read_json(cache, PATCH_ASSET_CATALOG_KEY)
    except Exception:  # noqa: BLE001
        return None
    finally:
        await cache.aclose()
    return catalog


async def get_deadlock_asset_catalog() -> dict[str, Any]:
    catalog = await _cached_deadlock_asset_catalog()
    if catalog is None:
        await refresh_home_content(force=True)
        catalog = await _cached_deadlock_asset_catalog()
    return catalog or {"heroes": {}, "items": {}, "ranks": {}, "objectives": {}}


async def get_supported_deadlock_hero_names() -> set[str]:
    catalog = await _cached_deadlock_asset_catalog()
    source_names = {
        str(hero.get("name") or "").strip()
        for hero in ((catalog or {}).get("heroes") or {}).values()
        if isinstance(hero, dict) and str(hero.get("name") or "").strip()
    }
    return source_names or set(POOL_LIST)


def public_deadlock_game_assets(catalog: dict[str, Any]) -> dict[str, Any]:
    unique_heroes: dict[str, str] = {}
    for hero in (catalog.get("heroes") or {}).values():
        if not isinstance(hero, dict):
            continue
        name = str(hero.get("name") or "").strip()
        if name:
            unique_heroes[name.casefold()] = name
    hero_names = sorted(unique_heroes.values(), key=str.casefold) or list(POOL_LIST)
    ranks = catalog.get("ranks") if isinstance(catalog.get("ranks"), dict) else {}
    return {
        "heroes": [
            {
                "name": name,
                "image_url": f"/api/v1/content/game-assets/heroes/{quote(name, safe='')}.png",
                "source_available": name.casefold() in unique_heroes,
            }
            for name in hero_names
        ],
        "ranks": [
            {
                "name": name,
                "image_url": f"/api/v1/content/game-assets/ranks/{quote(name, safe='')}.webp",
                "source_available": name.casefold() in ranks,
            }
            for name in RANKS
        ],
    }


def resolve_deadlock_hero_image(catalog: dict[str, Any], hero_name: str) -> str | None:
    hero = (catalog.get("heroes") or {}).get(hero_name.strip().casefold())
    return str((hero or {}).get("icon_url") or "").strip() or None


def resolve_deadlock_rank_image(catalog: dict[str, Any], rank_name: str) -> str | None:
    rank = (catalog.get("ranks") or {}).get(rank_name.strip().casefold())
    return str((rank or {}).get("icon_url") or "").strip() or None


def _structure_patch_detail(raw: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    hero_catalog = catalog.get("heroes") if isinstance(catalog.get("heroes"), dict) else catalog
    item_catalog = catalog.get("items") if isinstance(catalog.get("items"), dict) else {}
    objective_catalog = (
        catalog.get("objectives")
        if isinstance(catalog.get("objectives"), dict)
        else {}
    )
    hero_sections: dict[str, dict[str, Any]] = {}
    item_sections: dict[str, dict[str, Any]] = {}
    objective_sections: dict[str, dict[str, Any]] = {}
    general_changes: list[str] = []
    hero_aliases = sorted(hero_catalog, key=len, reverse=True)
    item_aliases = sorted(item_catalog, key=len, reverse=True)
    current_scope: str | None = None

    for line in _patch_lines(str(raw.get("content") or "")):
        scope_heading = _patch_scope_heading(line)
        if scope_heading is not None:
            current_scope = scope_heading
            continue
        lowered = line.lower()
        matched_item_alias = next(
            (alias for alias in item_aliases if lowered.startswith(f"{alias}:")),
            None,
        )
        if matched_item_alias is not None:
            item = item_catalog[matched_item_alias]
            item_key = str(item.get("slug") or item.get("name") or matched_item_alias)
            section = item_sections.setdefault(
                item_key,
                {
                    "kind": "item",
                    "title": str(item.get("name") or matched_item_alias.title()),
                    "hero_name": None,
                    "item_name": str(item.get("name") or matched_item_alias.title()),
                    "item_category": str(item.get("category") or ""),
                    "_item_cost": item.get("cost"),
                    "item_icon_url": item.get("icon_url"),
                    "changes": [],
                    "abilities": [],
                },
            )
            section["changes"].extend(_change_chunks(line.split(":", 1)[1]))
            continue

        matched_hero_alias = next(
            (alias for alias in hero_aliases if lowered.startswith(f"{alias}:")),
            None,
        )
        if matched_hero_alias is not None:
            hero = hero_catalog[matched_hero_alias]
            hero_key = str(hero.get("slug") or hero.get("name") or matched_hero_alias)
            section = hero_sections.setdefault(
                hero_key,
                {
                    "kind": "hero",
                    "title": str(hero.get("name") or matched_hero_alias.title()),
                    "hero_name": str(hero.get("name") or matched_hero_alias.title()),
                    "changes": [],
                    "abilities": [],
                    "_ability_map": {},
                },
            )
            change = line.split(":", 1)[1].strip()
            ability_map = section["_ability_map"]
            abilities = hero.get("abilities") or {}
            matched_ability = next(
                (
                    name
                    for name in sorted(abilities, key=len, reverse=True)
                    if change[:len(name)].casefold() == name.casefold()
                    and (len(change) == len(name) or change[len(name)] in " \t:-–—")
                ),
                None,
            )
            if matched_ability is None:
                section["changes"].extend(_change_chunks(change))
                continue
            ability = abilities[matched_ability]
            ability_section = ability_map.get(matched_ability)
            if ability_section is None:
                ability_section = {
                    "name": ability["name"],
                    "icon_url": ability.get("icon_url"),
                    "changes": [],
                }
                ability_map[matched_ability] = ability_section
                section["abilities"].append(ability_section)
            ability_section["changes"].extend(
                _change_chunks(_strip_named_change_prefix(change, str(ability["name"])))
            )
            continue

        objective_key: str | None = None
        objective_change = line
        if current_scope == "objective":
            objective_key = _objective_scope_key(line)
        else:
            objective_match = _objective_prefix_match(line)
            if objective_match is not None:
                objective_key, matched_objective_alias = objective_match
                objective_change = _strip_objective_prefix(line, matched_objective_alias)
        if objective_key is None:
            general_changes.extend(_change_chunks(line))
            continue
        definition = OBJECTIVE_DEFINITIONS[objective_key]
        objective = objective_catalog.get(objective_key)
        objective_icon_url = (
            str(objective.get("icon_url") or "").strip() or None
            if isinstance(objective, dict)
            else None
        )
        section = objective_sections.setdefault(
            objective_key,
            {
                "kind": "objective",
                "title": definition["title"],
                "objective_key": objective_key,
                "objective_icon_url": objective_icon_url,
                "changes": [],
                "abilities": [],
            },
        )
        section["changes"].extend(_change_chunks(objective_change))

    sections: list[dict[str, Any]] = []
    if general_changes:
        sections.append(
            {
                "kind": "general",
                "title": "Общие изменения",
                "hero_name": None,
                "changes": general_changes,
                "abilities": [],
            }
        )
    for objective_key in OBJECTIVE_DEFINITIONS:
        section = objective_sections.get(objective_key)
        if section is not None:
            sections.append(section)
    category_order = {"weapon": 0, "vitality": 1, "spirit": 2}
    sorted_item_sections = sorted(
        item_sections.values(),
        key=lambda section: (
            category_order.get(str(section.get("item_category")), 3),
            section.get("_item_cost") if isinstance(section.get("_item_cost"), (int, float)) else float("inf"),
            str(section.get("title") or "").casefold(),
        ),
    )
    for section in sorted_item_sections:
        section.pop("_item_cost", None)
        sections.append(section)
    for section in hero_sections.values():
        section.pop("_ability_map", None)
        sections.append(section)
    return {**raw, "sections": sections}


async def _fetch_steam_patches(client: httpx.AsyncClient) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    response = await client.get(
        STEAM_NEWS_URL,
        params={
            "appid": DEADLOCK_APP_ID,
            "count": 20,
            "maxlength": 30000,
            "format": "json",
        },
    )
    response.raise_for_status()
    items = response.json().get("appnews", {}).get("newsitems", [])
    patches_by_id: dict[str, dict[str, Any]] = {}
    details: dict[str, dict[str, Any]] = {}
    for item in items:
        if not _is_official_deadlock_announcement(item):
            continue
        patch_id = str(item.get("gid") or "").strip()
        title = str(item.get("title") or "").strip()
        if not patch_id.isdigit() or not title:
            continue
        if patch_id in patches_by_id:
            continue
        content = _plain_text(str(item.get("contents") or ""), max_length=30000)
        published_at = _source_datetime(item.get("date"))
        url = _https_url(
            item.get("url") or "https://store.steampowered.com/news/app/1422450",
            host_suffixes=STEAM_NEWS_HOST_SUFFIXES,
        )
        patches_by_id[patch_id] = {
            "id": patch_id,
            "title": title[:180],
            "excerpt": _excerpt(content),
            "published_at": published_at.isoformat(),
            "url": url,
        }
        details[patch_id] = {
            "id": patch_id,
            "title": title[:180],
            "published_at": published_at.isoformat(),
            "url": url,
            "content": content,
        }
    patches = sorted(
        patches_by_id.values(),
        key=lambda patch: _source_datetime(patch["published_at"]),
        reverse=True,
    )[:HOME_PATCH_LIMIT]
    if not patches:
        raise ValueError("Steam News did not contain a valid Deadlock patch note.")
    return patches, {patch["id"]: details[patch["id"]] for patch in patches}


async def _fetch_youtube_videos(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    feed_response, videos_response = await asyncio.gather(
        client.get(YOUTUBE_FEED_URL),
        client.get(
            YOUTUBE_VIDEOS_URL,
            headers={
                "Cookie": "SOCS=CAI",
                "User-Agent": "Mozilla/5.0 (compatible; OldSparkyArena/1.0)",
            },
        ),
    )
    feed_response.raise_for_status()
    videos_response.raise_for_status()
    regular_video_ids = set(
        re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', videos_response.text)
    )
    if not regular_video_ids:
        raise ValueError("YouTube videos tab did not expose regular video identifiers.")
    root = ElementTree.fromstring(feed_response.content)
    atom = "{http://www.w3.org/2005/Atom}"
    yt = "{http://www.youtube.com/xml/schemas/2015}"
    media = "{http://search.yahoo.com/mrss/}"
    videos_by_id: dict[str, dict[str, Any]] = {}
    for entry in root.findall(f"{atom}entry"):
        video_id = (entry.findtext(f"{yt}videoId") or "").strip()
        title = (entry.findtext(f"{atom}title") or "").strip()
        published_at = (entry.findtext(f"{atom}published") or "").strip()
        if (
            not video_id
            or re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) is None
            or video_id in videos_by_id
            or video_id not in regular_video_ids
            or not title
            or not published_at
        ):
            continue
        published = _source_datetime(published_at)
        thumbnail = entry.find(f"{media}group/{media}thumbnail")
        thumbnail_url = (
            thumbnail.attrib.get("url")
            if thumbnail is not None
            else f"https://i3.ytimg.com/vi/{video_id}/hqdefault.jpg"
        )
        safe_thumbnail = _https_url(
            str(thumbnail_url),
            hosts=YOUTUBE_IMAGE_HOSTS,
        )
        videos_by_id[video_id] = {
            "id": video_id[:32],
            "title": title[:180],
            "published_at": published.isoformat(),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail_url": safe_thumbnail,
        }
    videos = sorted(
        videos_by_id.values(),
        key=lambda video: _source_datetime(video["published_at"]),
        reverse=True,
    )[:HOME_VIDEO_LIMIT]
    if not videos:
        raise ValueError("YouTube feed did not contain a regular channel video.")
    return videos


async def _read_json(client: Any, key: str) -> dict[str, Any] | None:
    raw_value = await client.get(key)
    if not raw_value:
        return None
    try:
        value = json.loads(raw_value)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


async def refresh_home_content(*, force: bool = False) -> dict[str, Any]:
    settings = get_settings()
    cache = redis_client()
    lock_token = token_urlsafe(24)
    acquired = False
    try:
        if not force:
            cached = await _read_json(cache, HOME_CONTENT_KEY)
            if cached is not None:
                return cached
        stale = await _read_json(cache, HOME_CONTENT_STALE_KEY)
        acquired = bool(
            await cache.set(
                HOME_CONTENT_LOCK_KEY,
                lock_token,
                nx=True,
                ex=REFRESH_LOCK_SECONDS,
            )
        )
        if not acquired:
            if stale is not None:
                return stale
            await asyncio.sleep(0.2)
            return await _read_json(cache, HOME_CONTENT_KEY) or _empty_home_content()

        asset_catalog = await _read_json(cache, PATCH_ASSET_CATALOG_KEY)
        timeout = httpx.Timeout(settings.platform_external_content_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            steam_result, youtube_result = await asyncio.gather(
                _fetch_steam_patches(client),
                _fetch_youtube_videos(client),
                return_exceptions=True,
            )
            stale_patch_ids = {str(patch.get("id") or "") for patch in (stale or {}).get("patches") or []}
            has_new_patch = (
                not isinstance(steam_result, Exception)
                and any(str(patch.get("id") or "") not in stale_patch_ids for patch in steam_result[0])
            )
            asset_result = asset_catalog
            if asset_catalog is None or has_new_patch:
                try:
                    asset_result = await _fetch_deadlock_asset_catalog(
                        client,
                        previous_catalog=asset_catalog,
                    )
                except Exception as error:  # noqa: BLE001
                    asset_result = error
            if has_new_patch and isinstance(asset_result, Exception):
                logger.warning(
                    "home_content_refresh_deferred source=deadlock-assets error=%s",
                    type(asset_result).__name__,
                )
                steam_result = ValueError("New patch asset catalog is unavailable.")

        stale_patches = list((stale or {}).get("patches") or [])
        stale_videos = list((stale or {}).get("videos") or [])
        patch_details: dict[str, dict[str, Any]] = {}
        if isinstance(steam_result, Exception):
            logger.warning("home_content_refresh_failed source=steam error=%s", type(steam_result).__name__)
            patches = stale_patches
        else:
            patches, raw_patch_details = steam_result
            resolved_catalog = asset_catalog if isinstance(asset_catalog, dict) else {}
            if not isinstance(asset_result, Exception):
                resolved_catalog = asset_result
            patch_details = {
                patch_id: _structure_patch_detail(detail, resolved_catalog)
                for patch_id, detail in raw_patch_details.items()
            }
        if isinstance(youtube_result, Exception):
            logger.warning("home_content_refresh_failed source=youtube error=%s", type(youtube_result).__name__)
            videos = stale_videos
        else:
            videos = youtube_result
        payload = {
            "patches": patches,
            "videos": videos,
            "generated_at": datetime.now(UTC).isoformat(),
            "patches_available": not isinstance(steam_result, Exception),
            "videos_available": not isinstance(youtube_result, Exception),
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with cache.pipeline(transaction=True) as pipeline:
            pipeline.set(HOME_CONTENT_KEY, encoded, ex=settings.platform_home_content_cache_seconds)
            pipeline.set(HOME_CONTENT_STALE_KEY, encoded, ex=settings.platform_home_content_stale_seconds)
            if (asset_catalog is None or has_new_patch) and not isinstance(asset_result, Exception):
                pipeline.set(
                    PATCH_ASSET_CATALOG_KEY,
                    json.dumps(asset_result, ensure_ascii=False, separators=(",", ":")),
                    ex=PATCH_ASSET_CATALOG_TTL_SECONDS,
                )
            for patch_id, content in patch_details.items():
                pipeline.set(
                    PATCH_DETAIL_KEY_PREFIX + patch_id,
                    json.dumps(content, ensure_ascii=False, separators=(",", ":")),
                    ex=PATCH_DETAIL_TTL_SECONDS,
                )
            await pipeline.execute()
        return payload
    finally:
        if acquired:
            await cache.eval(
                LOCK_RELEASE_SCRIPT,
                1,
                HOME_CONTENT_LOCK_KEY,
                lock_token,
            )
        await cache.aclose()


async def get_patch_detail(patch_id: str) -> dict[str, Any] | None:
    cache = redis_client()
    try:
        content = await cache.get(PATCH_DETAIL_KEY_PREFIX + patch_id)
    finally:
        await cache.aclose()
    if content:
        try:
            decoded = json.loads(content)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, dict):
            return decoded
    await refresh_home_content(force=True)
    cache = redis_client()
    try:
        content = await cache.get(PATCH_DETAIL_KEY_PREFIX + patch_id)
        if not content:
            return None
        try:
            decoded = json.loads(content)
        except (TypeError, ValueError):
            return None
        return decoded if isinstance(decoded, dict) else None
    finally:
        await cache.aclose()


def _empty_home_content() -> dict[str, Any]:
    return {
        "patches": [],
        "videos": [],
        "generated_at": datetime.now(UTC).isoformat(),
        "patches_available": False,
        "videos_available": False,
    }
