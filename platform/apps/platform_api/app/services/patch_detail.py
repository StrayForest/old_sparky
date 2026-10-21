from __future__ import annotations

import re
from typing import Any

from apps.platform_api.app.services import home_content as base
from apps.platform_api.app.services.patch_translation import apply_cached_patch_translation


_ENTITY_DELIMITERS = " \t:-–—"
_FLATTENED_ENTITY_BULLET_RE = re.compile(
    r"\s+-\s+(?=[A-Z][A-Za-z0-9 '&()./–—-]{0,80}:)"
)
_PATCH_ABILITY_ALIASES = {
    # Rocket Jumping is a named patch-note mechanic of Deadman Danny's
    # Chimera Mk1 jetpack, not a separate selectable ability in the game data.
    "deadman danny": {"rocket jumping": "chimera mk1"},
}


def _patch_lines(content: str) -> list[str]:
    """Restore Steam bullets that were flattened to `` ... - Hero: ...``.

    Some Steam News posts contain their bullet list as plain text rather than
    individual HTML list elements. ``_plain_text`` correctly normalizes the
    whitespace but those bullets can consequently arrive as one long line.
    Split only separators followed by a short title-like ``Name:`` prefix so
    numeric minus signs and prose hyphens remain untouched.
    """

    normalized = _FLATTENED_ENTITY_BULLET_RE.sub("\n- ", content)
    return base._patch_lines(normalized)


def _match_named_line(
    line: str,
    catalog: dict[str, Any],
) -> tuple[str, str] | None:
    lowered = line.casefold()
    for alias in sorted(catalog, key=len, reverse=True):
        if lowered == alias:
            return alias, ""
        if not lowered.startswith(alias):
            continue
        suffix = line[len(alias):]
        if suffix and suffix[0] in _ENTITY_DELIMITERS:
            return alias, suffix.lstrip(_ENTITY_DELIMITERS).strip()
    return None


def _hero_section(
    hero_sections: dict[str, dict[str, Any]],
    hero: dict[str, Any],
    alias: str,
) -> tuple[str, dict[str, Any]]:
    hero_key = str(hero.get("slug") or hero.get("name") or alias)
    section = hero_sections.setdefault(
        hero_key,
        {
            "kind": "hero",
            "title": str(hero.get("name") or alias.title()),
            "hero_name": str(hero.get("name") or alias.title()),
            "changes": [],
            "abilities": [],
            "_ability_map": {},
        },
    )
    return hero_key, section


def _item_section(
    item_sections: dict[str, dict[str, Any]],
    item: dict[str, Any],
    alias: str,
) -> tuple[str, dict[str, Any]]:
    item_key = str(item.get("slug") or item.get("name") or alias)
    section = item_sections.setdefault(
        item_key,
        {
            "kind": "item",
            "title": str(item.get("name") or alias.title()),
            "hero_name": None,
            "item_name": str(item.get("name") or alias.title()),
            "item_category": str(item.get("category") or ""),
            "_item_cost": item.get("cost"),
            "item_icon_url": item.get("icon_url"),
            "changes": [],
            "abilities": [],
        },
    )
    return item_key, section


def _match_ability(
    line: str,
    hero: dict[str, Any],
) -> tuple[str, str] | None:
    abilities = hero.get("abilities") or {}
    lowered = line.casefold()
    hero_name = str(hero.get("name") or "").casefold()
    aliases = {
        str(alias).casefold(): str(alias)
        for alias in abilities
    }
    for alias, ability in abilities.items():
        if not isinstance(ability, dict):
            continue
        display_name = str(ability.get("name") or "").strip()
        if display_name:
            aliases.setdefault(display_name.casefold(), str(alias))
        extra_aliases = ability.get("aliases") or []
        if isinstance(extra_aliases, str):
            extra_aliases = [extra_aliases]
        for extra_alias in extra_aliases:
            if isinstance(extra_alias, str) and extra_alias.strip():
                aliases.setdefault(extra_alias.strip().casefold(), str(alias))
    patch_aliases = _PATCH_ABILITY_ALIASES.get(hero_name, {})
    for patch_alias, canonical_alias in patch_aliases.items():
        if canonical_alias in aliases:
            aliases[patch_alias] = aliases[canonical_alias]

    for alias in sorted(aliases, key=len, reverse=True):
        if lowered == alias:
            return aliases[alias], ""
        if not lowered.startswith(alias):
            continue
        suffix = line[len(alias):]
        if suffix and suffix[0] in _ENTITY_DELIMITERS:
            return aliases[alias], suffix.lstrip(_ENTITY_DELIMITERS).strip()
    return None


def _ability_section(
    section: dict[str, Any],
    hero: dict[str, Any],
    ability_alias: str,
) -> dict[str, Any]:
    ability = (hero.get("abilities") or {})[ability_alias]
    ability_map = section["_ability_map"]
    ability_section = ability_map.get(ability_alias)
    if ability_section is None:
        ability_section = {
            "name": ability["name"],
            "icon_url": ability.get("icon_url"),
            "changes": [],
        }
        ability_map[ability_alias] = ability_section
        section["abilities"].append(ability_section)
    return ability_section


def structure_patch_detail(raw: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    """Parse Steam patch text as a stateful General/Items/Heroes document.

    Steam frequently emits an entity heading on one line and its bullet changes on
    following lines. The legacy parser only associated lines written as
    ``Hero: change`` and therefore lost section boundaries when Valve changed
    formatting. This parser remembers the active item/hero/ability until the next
    heading.
    """

    hero_catalog = catalog.get("heroes") if isinstance(catalog.get("heroes"), dict) else {}
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

    current_scope: str | None = None
    current_hero_key: str | None = None
    current_hero_alias: str | None = None
    current_item_key: str | None = None
    current_ability_alias: str | None = None
    current_general_ability_pending = False

    for line in _patch_lines(str(raw.get("content") or "")):
        scope_heading = base._patch_scope_heading(line)
        if scope_heading is not None:
            current_scope = scope_heading
            current_hero_key = None
            current_hero_alias = None
            current_item_key = None
            current_ability_alias = None
            current_general_ability_pending = False
            continue

        if current_scope == "heroes":
            hero_match = _match_named_line(line, hero_catalog)
            if hero_match is not None:
                hero_alias, remainder = hero_match
                hero = hero_catalog[hero_alias]
                current_hero_key, section = _hero_section(hero_sections, hero, hero_alias)
                current_hero_alias = hero_alias
                current_item_key = None
                current_ability_alias = None
                current_general_ability_pending = False
                if remainder:
                    ability_match = _match_ability(remainder, hero)
                    if ability_match is None:
                        section["changes"].extend(base._change_chunks(remainder))
                    else:
                        current_ability_alias, ability_change = ability_match
                        ability_section = _ability_section(
                            section,
                            hero,
                            current_ability_alias,
                        )
                        if ability_change:
                            ability_section["changes"].extend(
                                base._change_chunks(ability_change)
                            )
                        current_general_ability_pending = not ability_change
                continue

            if current_hero_key is not None:
                section = hero_sections[current_hero_key]
                hero_name = str(section.get("hero_name") or "").casefold()
                hero = next(
                    (
                        value
                        for value in hero_catalog.values()
                        if isinstance(value, dict)
                        and str(value.get("name") or "").casefold() == hero_name
                    ),
                    None,
                )
                if isinstance(hero, dict):
                    ability_match = _match_ability(line, hero)
                    if ability_match is not None:
                        current_ability_alias, ability_change = ability_match
                        ability_section = _ability_section(
                            section,
                            hero,
                            current_ability_alias,
                        )
                        if ability_change:
                            ability_section["changes"].extend(
                                base._change_chunks(ability_change)
                            )
                        continue
                    if current_ability_alias is not None:
                        ability_section = _ability_section(
                            section,
                            hero,
                            current_ability_alias,
                        )
                        ability_section["changes"].extend(base._change_chunks(line))
                        continue
                section["changes"].extend(base._change_chunks(line))
                continue

        if current_scope == "items":
            item_match = _match_named_line(line, item_catalog)
            if item_match is not None:
                item_alias, remainder = item_match
                item = item_catalog[item_alias]
                current_item_key, section = _item_section(item_sections, item, item_alias)
                current_hero_key = None
                current_hero_alias = None
                current_ability_alias = None
                current_general_ability_pending = False
                if remainder:
                    section["changes"].extend(base._change_chunks(remainder))
                continue
            if current_item_key is not None:
                item_sections[current_item_key]["changes"].extend(base._change_chunks(line))
                continue

        if current_scope == "general":
            # Valve sometimes places hero changes in the [General] block. Route
            # catalogued hero prefixes into their hero section while preserving
            # unlabelled general changes. A bare follow-up line is consumed only
            # when the immediately preceding hero line was an ability heading.
            hero_match = _match_named_line(line, hero_catalog)
            if hero_match is not None:
                hero_alias, remainder = hero_match
                hero = hero_catalog[hero_alias]
                current_hero_key, section = _hero_section(hero_sections, hero, hero_alias)
                current_hero_alias = hero_alias
                current_item_key = None
                current_ability_alias = None
                current_general_ability_pending = False
                if remainder:
                    ability_match = _match_ability(remainder, hero)
                    if ability_match is None:
                        section["changes"].extend(base._change_chunks(remainder))
                    else:
                        current_ability_alias, ability_change = ability_match
                        ability_section = _ability_section(
                            section,
                            hero,
                            current_ability_alias,
                        )
                        if ability_change:
                            ability_section["changes"].extend(
                                base._change_chunks(ability_change)
                            )
                        current_general_ability_pending = not ability_change
                continue

            if current_hero_alias is not None:
                hero = hero_catalog.get(current_hero_alias)
                if isinstance(hero, dict) and current_hero_key is not None:
                    section = hero_sections[current_hero_key]
                    if current_general_ability_pending and current_ability_alias is not None:
                        _ability_section(
                            section,
                            hero,
                            current_ability_alias,
                        )["changes"].extend(base._change_chunks(line))
                        current_general_ability_pending = False
                        continue
                    ability_match = _match_ability(line, hero)
                    if ability_match is not None:
                        current_ability_alias, ability_change = ability_match
                        ability_section = _ability_section(
                            section,
                            hero,
                            current_ability_alias,
                        )
                        if ability_change:
                            ability_section["changes"].extend(
                                base._change_chunks(ability_change)
                            )
                        current_general_ability_pending = not ability_change
                        continue

            general_changes.extend(base._change_chunks(line))
            continue

        objective_key: str | None = None
        objective_change = line
        if current_scope == "objective":
            objective_key = base._objective_scope_key(line)
        else:
            objective_match = base._objective_prefix_match(line)
            if objective_match is not None:
                objective_key, _matched_objective_alias = objective_match
                # Objective aliases are routing hints, not disposable headings.
                # Keep the full source sentence so subjects such as Rift Troopers,
                # Urn carriers, Mid Boss, and future neutral objectives survive
                # into translation instead of becoming ambiguous fragments.
                objective_change = line
        if objective_key is not None:
            current_hero_key = None
            current_hero_alias = None
            current_item_key = None
            current_ability_alias = None
            current_general_ability_pending = False
            definition = base.OBJECTIVE_DEFINITIONS[objective_key]
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
            section["changes"].extend(base._change_chunks(objective_change))
            continue

        # Backward compatibility for older Steam posts that did not include
        # explicit [Items]/[Heroes] scope headings.
        item_match = _match_named_line(line, item_catalog)
        if item_match is not None:
            item_alias, remainder = item_match
            _, section = _item_section(item_sections, item_catalog[item_alias], item_alias)
            current_hero_key = None
            current_hero_alias = None
            current_item_key = item_alias
            current_ability_alias = None
            current_general_ability_pending = False
            if remainder:
                section["changes"].extend(base._change_chunks(remainder))
            continue

        hero_match = _match_named_line(line, hero_catalog)
        if hero_match is not None:
            hero_alias, remainder = hero_match
            hero = hero_catalog[hero_alias]
            current_hero_key, section = _hero_section(hero_sections, hero, hero_alias)
            current_hero_alias = hero_alias
            current_item_key = None
            current_ability_alias = None
            current_general_ability_pending = False
            if remainder:
                ability_match = _match_ability(remainder, hero)
                if ability_match is None:
                    section["changes"].extend(base._change_chunks(remainder))
                else:
                    current_ability_alias, ability_change = ability_match
                    ability_section = _ability_section(section, hero, current_ability_alias)
                    if ability_change:
                        ability_section["changes"].extend(
                            base._change_chunks(ability_change)
                        )
                    else:
                        current_general_ability_pending = True
            continue

        # Older Steam posts often omit [Heroes] and flatten an ability heading
        # plus its following change into separate dash-delimited lines. Preserve
        # just the immediately following line for that ability; explicit entity
        # prefixes above reset this state, so it cannot leak into another hero.
        if current_general_ability_pending and current_hero_alias is not None:
            hero = hero_catalog.get(current_hero_alias)
            if isinstance(hero, dict) and current_hero_key is not None and current_ability_alias is not None:
                section = hero_sections[current_hero_key]
                _ability_section(section, hero, current_ability_alias)["changes"].extend(
                    base._change_chunks(line)
                )
                current_general_ability_pending = False
                continue

        general_changes.extend(base._change_chunks(line))

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

    for objective_key in base.OBJECTIVE_DEFINITIONS:
        section = objective_sections.get(objective_key)
        if section is not None:
            sections.append(section)

    category_order = {"weapon": 0, "vitality": 1, "spirit": 2}
    sorted_item_sections = sorted(
        item_sections.values(),
        key=lambda section: (
            category_order.get(str(section.get("item_category")), 3),
            section.get("_item_cost")
            if isinstance(section.get("_item_cost"), (int, float))
            else float("inf"),
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


async def get_patch_detail_source(patch_id: str) -> dict[str, Any] | None:
    raw = await base.get_patch_detail(patch_id)
    if raw is None:
        return None
    catalog = await base.get_deadlock_asset_catalog()
    return structure_patch_detail(raw, catalog)


async def get_patch_detail(patch_id: str) -> dict[str, Any] | None:
    source = await get_patch_detail_source(patch_id)
    if source is None:
        return None
    return await apply_cached_patch_translation(source)
