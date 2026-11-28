from __future__ import annotations

import json
from pathlib import Path
import unittest

from apps.platform_api.app.api.schemas import PatchDetailResponse
from apps.platform_api.app.services import home_content
from apps.platform_api.app.services.patch_detail import (
    _change_reference_entities,
    structure_patch_detail,
)
from apps.platform_api.app.services.patch_translation_runtime import (
    extract_translation_segments,
    merge_translation,
)


class PatchDetailParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = {
            "heroes": {
                "abrams": {
                    "name": "Abrams",
                    "slug": "abrams",
                    "icon_url": None,
                    "abilities": {
                        "siphon life": {
                            "name": "Siphon Life",
                            "icon_url": None,
                        }
                    },
                },
                "bebop": {
                    "name": "Bebop",
                    "slug": "bebop",
                    "icon_url": None,
                    "abilities": {
                        "sticky bomb": {
                            "name": "Sticky Bomb",
                            "icon_url": None,
                        }
                    },
                },
                "baba": {
                    "id": 88,
                    "name": "Baba",
                    "slug": "hero_baba",
                    "icon_url": None,
                    "abilities": {
                        "threadsap": {"id": 8801, "name": "Threadsap", "icon_url": None},
                        "granny long legs": {
                            "id": 8802,
                            "name": "Granny Long Legs",
                            "icon_url": None,
                        },
                        "baba's brew": {"id": 8803, "name": "Baba's Brew", "icon_url": None},
                        "feed the birds": {
                            "id": 8804,
                            "name": "Feed The Birds",
                            "icon_url": None,
                        },
                    },
                },
                "deadman danny": {
                    "id": 78,
                    "name": "Deadman Danny",
                    "slug": "deadman-danny",
                    "icon_url": None,
                    "abilities": {
                        "chimera mk1": {
                            "id": "ability_deadpack_rocketeer",
                            "name": "Chimera Mk1",
                            "icon_url": None,
                        },
                        "strix salvo": {"name": "Strix Salvo", "icon_url": None},
                    },
                },
            },
            "items": {
                "basic magazine": {
                    "name": "Basic Magazine",
                    "slug": "basic-magazine",
                    "category": "weapon",
                    "cost": 500,
                    "icon_url": None,
                },
                "extra health": {
                    "name": "Extra Health",
                    "slug": "extra-health",
                    "category": "vitality",
                    "cost": 500,
                    "icon_url": None,
                },
            },
            "objectives": {},
        }

    def _assert_patch_dto_projects_only_source_referenced_canonical_entity_names(self) -> None:
        catalog = {
            "heroes": {
                "grey_talon": {
                    "name": "Grey Talon",
                    "class_name": "hero_grey_talon",
                    "abilities": {
                        "charged shot": {"name": "Charged Shot"},
                        "rain of arrows": {"name": "Rain of Arrows"},
                    },
                }
            },
            "items": {
                "split shot": {"name": "Split Shot"},
                "superior duration": {"name": "Superior Duration"},
                "unused item": {"name": "Unused Item"},
            },
            "ranks": {},
            "objectives": {},
        }
        source = {
            "id": "entity-references",
            "title": "Entity references",
            "published_at": "2026-11-27T00:00:00+00:00",
            "url": "https://store.steampowered.com/news/app/1422450/view/123",
            "content": (
                "[General]\n"
                "Charged-Shot and Rain_of_Arrows remain unchanged.\n"
                "Split Shot and Superior-Duration changed.\n"
                "lowercase duration reduced."
            ),
        }

        detail = structure_patch_detail(source, catalog)
        self.assertEqual(
            set(detail["canonical_entity_names"]),
            {"Charged Shot", "Rain of Arrows", "Split Shot", "Superior Duration"},
        )
        self.assertNotIn("Unused Item", detail["canonical_entity_names"])

        old_cached_detail = {
            key: value
            for key, value in detail.items()
            if key != "canonical_entity_names"
        }
        old_wire = PatchDetailResponse.model_validate(old_cached_detail)
        self.assertEqual(old_wire.canonical_entity_names, [])
        reparsed = structure_patch_detail(old_cached_detail, catalog)
        self.assertEqual(
            set(reparsed["canonical_entity_names"]),
            {"Charged Shot", "Rain of Arrows", "Split Shot", "Superior Duration"},
        )
        self.assertEqual(
            set(PatchDetailResponse.model_validate(reparsed).canonical_entity_names),
            {"Charged Shot", "Rain of Arrows", "Split Shot", "Superior Duration"},
        )

        oversized_names = [f"Entity {index:04d}" for index in range(2001)]
        oversized_catalog = {
            "heroes": {},
            "items": {name.casefold(): {"name": name} for name in oversized_names},
            "ranks": {},
            "objectives": {},
        }
        with self.assertRaisesRegex(ValueError, "canonical entity-name limit"):
            structure_patch_detail(
                {**source, "content": " ".join(oversized_names)},
                oversized_catalog,
            )

    def test_multiline_hero_headings_keep_changes_with_each_hero(self) -> None:
        raw = {
            "id": "1",
            "title": "Patch",
            "content": """[Heroes]
Abrams:
Base health increased
Siphon Life:
Damage increased
Bebop
Base health reduced
Sticky Bomb - Damage reduced
""",
        }

        result = structure_patch_detail(raw, self.catalog)
        heroes = [section for section in result["sections"] if section["kind"] == "hero"]

        self.assertEqual([section["hero_name"] for section in heroes], ["Abrams", "Bebop"])
        self.assertEqual(heroes[0]["changes"], ["Base health increased"])
        self.assertEqual(heroes[0]["abilities"][0]["name"], "Siphon Life")
        self.assertEqual(heroes[0]["abilities"][0]["changes"], ["Damage increased"])
        self.assertEqual(heroes[1]["changes"], ["Base health reduced"])
        self.assertEqual(heroes[1]["abilities"][0]["name"], "Sticky Bomb")
        self.assertEqual(heroes[1]["abilities"][0]["changes"], ["Damage reduced"])

    def test_multiline_item_headings_keep_changes_with_each_item(self) -> None:
        raw = {
            "id": "2",
            "title": "Patch",
            "content": """[Items]
Basic Magazine:
Ammo increased
Extra Health
Bonus health reduced
""",
        }

        result = structure_patch_detail(raw, self.catalog)
        items = [section for section in result["sections"] if section["kind"] == "item"]

        self.assertEqual([section["item_name"] for section in items], ["Basic Magazine", "Extra Health"])
        self.assertEqual(items[0]["changes"], ["Ammo increased"])
        self.assertEqual(items[1]["changes"], ["Bonus health reduced"])

    def test_live_restorative_locket_source_keeps_nested_changes_and_references(self) -> None:
        abilities_by_hero = {
            "Danny": ["Jettison Fuel", "Strix Salvo"],
            "Doorman": ["Door Recall"],
            "Mina": ["Sanguine Retreat"],
            "Nurse Harrow": ["Healing Needles"],
            "Solomon": ["Move King", "Move Piece"],
            "Venator": ["Ira Domini"],
            "Violet": ["Living Ink"],
        }
        heroes = {
            hero.casefold(): {
                "name": hero,
                "class_name": "hero_" + hero.casefold().replace(" ", "_"),
                "hero_image_url": f"https://assets.test/{hero.casefold()}.png",
                "abilities": {
                    ability.casefold(): {
                        "name": ability,
                        "icon_url": f"https://assets.test/{ability.casefold().replace(' ', '-')}.png",
                        "description": f"Description of {ability}",
                        "russian_metadata_available": True,
                    }
                    for ability in abilities
                },
            }
            for hero, abilities in abilities_by_hero.items()
        }
        heroes["abrams"] = {"name": "Abrams", "abilities": {}}
        items = {
            name.casefold(): {
                "name": name,
                "category": "vitality",
                "icon_url": f"https://assets.test/{name.casefold().replace(' ', '-')}.png",
            }
            for name in ("Restorative Locket", "Spellslinger", "Radiant Regeneration")
        }
        fixture = Path(__file__).parent / (
            "fixtures/patch-acceptance/restorative-locket-1850460852986472.txt"
        )
        source = fixture.read_text(encoding="utf-8")
        detail = structure_patch_detail(
            {
                "id": "1850460852986472",
                "title": "November 26 Patch",
                "published_at": "2026-11-26T00:00:00+00:00",
                "url": "https://store.steampowered.com/news/app/1422450/view/1850460852986472",
                "content": source,
            },
            {"heroes": heroes, "items": items, "objectives": {}},
        )

        self.assertEqual(
            [section["kind"] for section in detail["sections"]],
            ["general", "item", "hero"],
        )
        general, item, _hero = detail["sections"]
        self.assertEqual(
            general["changes"],
            [
                "Hero base health reduced by 8%. The health is redistributed into boons, so total health at max boons is roughly the same.",
                "Bullet damage growth per boon increased for all heroes by 5% (except for Haze, Holliday, Venator, Vindicta, Warden, Wraith)",
                "First kill bonus for each player increased from 125 to 175 souls",
            ],
        )
        self.assertFalse(any(
            change.startswith("\\[")
            for section in detail["sections"]
            for change in section["changes"]
        ))
        self.assertEqual(item["item_name"], "Restorative Locket")
        self.assertEqual(len(item["changes"]), 11)
        self.assertEqual(item["change_groups"], [
            {"heading_index": 0, "description_start": 1, "description_end": 10}
        ])
        self.assertEqual(
            item["changes"][1:10],
            [
                "Danny Jettison Fuel now grants a charge",
                "Danny Strix Salvo now grants a charge",
                "Doorman Door Recall no longer grants a charge",
                "Mina Sanguine Retreat recast now grants charges",
                "Nurse Harrow Healing Needles no longer grants a charge",
                "Solomon Move King now grants a charge",
                "Solomon Move Piece (Bishop, Knight and Queen moves) now grants a charge",
                "Venator Ira Domini now grants a charge",
                "Violet Living Ink (draw) now grants a charge",
            ],
        )
        self.assertEqual(item["changes"][10], "Applied similar fixes to Spellslinger and Radiant Regeneration for the above list")
        self.assertEqual(
            [reference["change_index"] for reference in item["change_references"]],
            list(range(1, 11)),
        )
        self.assertTrue(all(
            entity["icon_url"]
            for reference in item["change_references"]
            for entity in reference["entities"]
        ))
        self.assertFalse(any(
            entity["name"] == "Restorative Locket"
            for reference in item["change_references"]
            for entity in reference["entities"]
        ))
        wire = PatchDetailResponse.model_validate(detail)
        segments = extract_translation_segments(detail)
        translated = merge_translation(
            detail,
            {segment["id"]: f"Перевод {index}" for index, segment in enumerate(segments)},
        )
        translated_item = next(section for section in translated["sections"] if section["kind"] == "item")
        self.assertEqual(translated_item["change_references"], item["change_references"])
        self.assertEqual(translated_item["change_groups"], item["change_groups"])
        self.assertEqual(len(wire.sections[1].change_references), 10)

    def test_unknown_nested_entity_remains_text_and_logs_without_metadata(self) -> None:
        with self.assertLogs(
            "apps.platform_api.app.services.patch_detail", level="WARNING"
        ) as captured:
            detail = structure_patch_detail(
                {
                    "id": "1850460852986472",
                    "title": "November 26 Patch",
                    "content": (
                        "[Items]\nRestorative Locket:\n"
                        "-- Unknown Catalog Entity received a change"
                    ),
                },
                {"heroes": {}, "items": {"restorative locket": {"name": "Restorative Locket"}}, "objectives": {}},
            )
        item = next(section for section in detail["sections"] if section["kind"] == "item")
        self.assertEqual(item["changes"], ["Unknown Catalog Entity received a change"])
        self.assertEqual(item.get("change_references", []), [])
        self.assertIn("patch_nested_entity_reference_unresolved", captured.output[0])

    def test_ambiguous_exact_catalog_alias_has_no_reference_metadata(self) -> None:
        entities = _change_reference_entities(
            "Shared Name received a change",
            {"shared": {"name": "Shared Name", "class_name": "hero_shared"}},
            {"shared": {"name": "Shared Name", "icon_url": "https://assets.test/shared.png"}},
        )
        self.assertEqual(entities, [])

    def test_change_references_respect_the_wire_bound(self) -> None:
        catalog = {
            f"item {index}": {
                "name": f"Item {index}",
                "icon_url": f"https://assets.test/item-{index}.png",
            }
            for index in range(5)
        }
        entities = _change_reference_entities(
            "Item 0, Item 1, Item 2, Item 3 and Item 4 changed.",
            {},
            catalog,
        )
        self.assertEqual([entity["name"] for entity in entities], [
            "Item 0", "Item 1", "Item 2", "Item 3"
        ])

    def test_inline_legacy_format_remains_supported(self) -> None:
        raw = {
            "id": "3",
            "title": "Patch",
            "content": "Abrams: Siphon Life: Damage increased\nBebop: Base health reduced",
        }

        result = structure_patch_detail(raw, self.catalog)
        heroes = [section for section in result["sections"] if section["kind"] == "hero"]

        self.assertEqual(len(heroes), 2)
        self.assertEqual(heroes[0]["abilities"][0]["changes"], ["Damage increased"])
        self.assertEqual(heroes[1]["changes"], ["Base health reduced"])

    def test_flattened_steam_bullets_split_between_heroes(self) -> None:
        raw = {
            "id": "4",
            "title": "Patch",
            "content": (
                "Abrams: Siphon Life: Damage increased - "
                "Abrams: Base health increased - "
                "Bebop: Sticky Bomb: Damage reduced - "
                "Bebop: Base health reduced"
            ),
        }

        result = structure_patch_detail(raw, self.catalog)
        heroes = [section for section in result["sections"] if section["kind"] == "hero"]

        self.assertEqual([section["hero_name"] for section in heroes], ["Abrams", "Bebop"])
        self.assertEqual(heroes[0]["abilities"][0]["changes"], ["Damage increased"])
        self.assertEqual(heroes[0]["changes"], ["Base health increased"])
        self.assertEqual(heroes[1]["abilities"][0]["changes"], ["Damage reduced"])
        self.assertEqual(heroes[1]["changes"], ["Base health reduced"])

    def test_explicit_bold_heading_and_list_markup_projects_change_group_indexes(self) -> None:
        groups = home_content._extract_patch_change_group_sources(
            "[p][b]Seasonal Rank Reset[/b][/p]"
            "[p]Ranked rules update[/p]"
            "[p][b]Ranked Leaderboards:[/b][/p][list]"
            "[*][p]We've reworked leaderboards.[/p][/*]"
            "[*][p]Each season now has its own leaderboard.[/p][/*]"
            "[*][p]A player must complete 50 matches.[/p][/*]"
            "[*][p]A hero leaderboard requires 15 matches.[/p]"
            "[/list]------<p>Beta Season 2 begins today!</p>"
        )
        self.assertEqual(
            groups,
            [{
                "heading": "Ranked Leaderboards:",
                "items": [
                    "We've reworked leaderboards.",
                    "Each season now has its own leaderboard.",
                    "A player must complete 50 matches.",
                    "A hero leaderboard requires 15 matches.",
                ],
            }],
        )
        self.assertEqual(
            home_content._extract_patch_change_group_sources(
                "<p><strong>Season Reset:</strong></p>"
                "<ul><li><p>Rank reset is smaller.</p></li>"
                "<li><p>Players keep calibration.</p></li></ul>"
            ),
            [{
                "heading": "Season Reset:",
                "items": ["Rank reset is smaller.", "Players keep calibration."],
            }],
        )

        detail = structure_patch_detail(
            {
                "id": "5",
                "title": "Patch",
                "content": (
                    "Ranked Leaderboards:\n"
                    "We've reworked leaderboards.\n"
                    "Each season now has its own leaderboard.\n"
                    "A player must complete 50 matches.\n"
                    "A hero leaderboard requires 15 matches.\n"
                    "Beta Season 2 begins today!"
                ),
                "change_group_sources": groups,
            },
            {"heroes": {}, "items": {}, "objectives": {}},
        )
        section = detail["sections"][0]
        self.assertEqual(
            section["changes"],
            [
                "Ranked Leaderboards:",
                "We've reworked leaderboards.",
                "Each season now has its own leaderboard.",
                "A player must complete 50 matches.",
                "A hero leaderboard requires 15 matches.",
                "Beta Season 2 begins today!",
            ],
        )
        self.assertEqual(
            section["change_groups"],
            [{"heading_index": 0, "description_start": 1, "description_end": 5}],
        )
        self.assertNotIn("change_group_sources", detail)
        self.assertEqual(
            extract_translation_segments({"sections": [{"changes": section["changes"]}]}),
            extract_translation_segments({"sections": [section]}),
        )
        home_detail = home_content._structure_patch_detail(
            {
                "id": "5",
                "title": "Patch",
                "content": (
                    "Ranked Leaderboards:\n"
                    "We've reworked leaderboards.\n"
                    "Each season now has its own leaderboard.\n"
                    "A player must complete 50 matches.\n"
                    "A hero leaderboard requires 15 matches.\n"
                    "Beta Season 2 begins today!"
                ),
                "change_group_sources": groups,
            },
            {"heroes": {}, "items": {}, "objectives": {}},
        )
        self.assertEqual(home_detail["sections"][0]["changes"], section["changes"])
        self.assertEqual(home_detail["sections"][0]["change_groups"], section["change_groups"])
        self.assertFalse(
            {"change_group_sources", "change_heading_sources", "change_list_sources"}
            & home_detail.keys()
        )

    def test_steam_blocks_preserve_headings_paragraphs_and_unheaded_list_kinds(self) -> None:
        source = (
            "[General]\n[b]Operational Changes:[/b]\n"
            "[p]First paragraph.[/p]\n"
            "[p]A paragraph with [b]inline emphasis[/b].[/p]\n"
            "<h5>Secondary heading</h5><p>Second paragraph.</p>\n"
            "[list][*]First list item.[/*][*]Second list item.[/*][/list]\n"
            "[olist][*]First ordered item.[/*][*]Second ordered item.[/*][/olist]"
        )
        content = home_content._plain_text(source, max_length=30000)
        detail = structure_patch_detail(
            {
                "id": "blocks",
                "title": "Block format",
                "content": content,
                "change_heading_sources": home_content._extract_patch_heading_sources(source),
                "change_list_sources": home_content._extract_patch_list_sources(source),
            },
            {"heroes": {}, "items": {}, "objectives": {}},
        )
        general = detail["sections"][0]
        self.assertEqual(
            general["changes"],
            [
                "Operational Changes:",
                "First paragraph.",
                "A paragraph with inline emphasis.",
                "Secondary heading",
                "Second paragraph.",
                "First list item.",
                "Second list item.",
                "First ordered item.",
                "Second ordered item.",
            ],
        )
        self.assertEqual(general["change_headings"], [0, 3])
        self.assertEqual(
            general["change_lists"],
            [
                {"start": 5, "end": 7, "ordered": False},
                {"start": 7, "end": 9, "ordered": True},
            ],
        )

    def test_cached_heading_and_list_ranges_survive_catalog_reparse(self) -> None:
        cached = {
            "id": "blocks-cached",
            "title": "Block format",
            "content": "[General]\nOperational Changes:\nFirst item\nSecond item",
            "sections": [{
                "kind": "general",
                "title": "Общие изменения",
                "hero_name": None,
                "changes": ["Operational Changes:", "First item", "Second item"],
                "change_headings": [0],
                "change_lists": [{"start": 1, "end": 3, "ordered": True}],
                "abilities": [],
            }],
        }
        reparsed = structure_patch_detail(
            cached, {"heroes": {}, "items": {}, "objectives": {}}
        )["sections"][0]
        self.assertEqual(reparsed["change_headings"], [0])
        self.assertEqual(
            reparsed["change_lists"],
            [{"start": 1, "end": 3, "ordered": True}],
        )
        self._assert_patch_dto_projects_only_source_referenced_canonical_entity_names()

    def test_source_fixture_preserves_all_ranked_patch_headings_and_final_paragraph(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures/patch-acceptance/steam-news-four-patches.json"
        feed = json.loads(fixture_path.read_text(encoding="utf-8"))
        item = next(
            entry
            for entry in feed["items"]
            if "Ranked Leaderboards:" in entry["contents"]
        )
        source = item["contents"]
        detail = structure_patch_detail(
            {
                "id": item["gid"],
                "title": item["title"],
                "content": home_content._plain_text(source, max_length=30000),
                "change_group_sources": home_content._extract_patch_change_group_sources(source),
                "change_heading_sources": home_content._extract_patch_heading_sources(source),
                "change_list_sources": home_content._extract_patch_list_sources(source),
            },
            {"heroes": {}, "items": {}, "objectives": {}},
        )
        general = next(section for section in detail["sections"] if section["kind"] == "general")
        self.assertEqual(general["change_headings"], [2, 4, 7, 9, 12, 15, 17, 19])
        self.assertEqual(
            general["change_groups"],
            [{"heading_index": 21, "description_start": 22, "description_end": 26}],
        )
        self.assertNotIn("change_lists", general)
        self.assertEqual(general["changes"][26], "Beta Season 2 begins today, we'll see you there!")
        segments = extract_translation_segments(detail)
        translated = merge_translation(detail, {
            segment["id"]: f"Перевод {index}"
            for index, segment in enumerate(segments)
        })
        translated_general = next(
            section for section in translated["sections"] if section["kind"] == "general"
        )
        self.assertEqual(translated_general["change_headings"], [2, 4, 7, 9, 12, 15, 17, 19])
        self.assertEqual(translated_general["change_groups"], general["change_groups"])
        self.assertEqual(translated_general["changes"][26], "Перевод 26")

    def test_duplicate_group_sequences_remain_flat(self) -> None:
        group = home_content._extract_patch_change_group_sources(
            "[p][b]Ranked Leaderboards:[/b][/p][list]"
            "[*][p]We've reworked leaderboards.[/p][/list]"
        )[0]
        repeated_group = [group["heading"], *group["items"]]
        detail = structure_patch_detail(
            {
                "id": "duplicate-groups",
                "title": "Patch",
                "content": "\n".join(
                    [*repeated_group, "Separate update.", *repeated_group]
                ),
                "change_group_sources": [group],
            },
            {"heroes": {}, "items": {}, "objectives": {}},
        )

        general = next(
            section for section in detail["sections"] if section["kind"] == "general"
        )
        self.assertEqual(general["changes"], [*repeated_group, "Separate update.", *repeated_group])
        self.assertNotIn("change_groups", general)

    def test_general_scope_routes_catalogued_hero_and_ability_headings(self) -> None:
        raw = {
            "id": "5",
            "title": "Minor Update",
            "content": """[General]
Global cooldown reduced from 10s to 9s
Baba: Spirit Resistance scaling reduced from 0.12 to 0.1
Baba: Threadsap
Spirit reduction on enemies now ends when Baba dies
Baba: Threadsap - Cooldown increased from 26s to 28s
Baba: Granny Long Legs
Cooldown increased from 30s to 32s
Baba: Baba's Brew - Barrier reduced from 100 to 90
Deadman Danny: Health regen increased from 2.5 to 3.25
Deadman Danny: Rocket Jumping - Self damage from 6.5% max HP to 5% and is now spirit damage
""",
        }

        result = structure_patch_detail(raw, self.catalog)
        sections = result["sections"]
        general = next(section for section in sections if section["kind"] == "general")
        heroes = {
            section["hero_name"]: section
            for section in sections
            if section["kind"] == "hero"
        }

        self.assertEqual(general["changes"], ["Global cooldown reduced from 10s to 9s"])
        self.assertEqual(
            heroes["Baba"]["changes"],
            ["Spirit Resistance scaling reduced from 0.12 to 0.1"],
        )
        baba_abilities = {
            ability["name"]: ability["changes"]
            for ability in heroes["Baba"]["abilities"]
        }
        self.assertEqual(
            baba_abilities["Threadsap"],
            [
                "Spirit reduction on enemies now ends when Baba dies",
                "Cooldown increased from 26s to 28s",
            ],
        )
        self.assertEqual(
            baba_abilities["Granny Long Legs"],
            ["Cooldown increased from 30s to 32s"],
        )
        self.assertEqual(baba_abilities["Baba's Brew"], ["Barrier reduced from 100 to 90"])

        danny_abilities = {
            ability["name"]: ability["changes"]
            for ability in heroes["Deadman Danny"]["abilities"]
        }
        self.assertEqual(
            heroes["Deadman Danny"]["changes"],
            ["Health regen increased from 2.5 to 3.25"],
        )
        self.assertEqual(
            danny_abilities["Chimera Mk1"],
            ["Self damage from 6.5% max HP to 5% and is now spirit damage"],
        )

    def test_flattened_steam_1014_raw_groups_baba_and_danny_without_scope_leak(self) -> None:
        # Faithful excerpt from Steam News 1846651706676198 (10-14-2026).
        # Steam serves this as a single dash-separated text field, not [Heroes].
        raw = {
            "id": "1846651706676198",
            "title": "Minor Update - 10-14-2026",
            "content": (
                "- Baba: Spirit Resistance scaling reduced from 0.12 to 0.1 "
                "- Baba: Now has -7% Base Bullet Resistance "
                "- Baba: Threadsap - Spirit reduction on enemies now ends when Baba dies "
                "- Baba: Threadsap - Cooldown increased from 26s to 28s "
                "- Baba: Granny Long Legs - Cooldown increased from 30s to 32s "
                "- Baba: Granny Long Legs - Repeat light melees while airborne now have diminishing forward momentum "
                "- Baba: Baba's Brew - Barrier reduced from 100 to 90 "
                "- Street Brawl: Baba Threadsap duration lasts 35% less "
                "- Deadman Danny: Health regen increased from 2.5 to 3.25 "
                "- Deadman Danny: Chimera Mk1 - Cooldown boon scaling increased from -0.25 to -0.45 "
                "- Deadman Danny: Rocket Jumping - Self damage from 6.5% max HP to 5% and is now spirit damage "
                "- Deadman Danny: Strix Salvo - Cooldown from 30s to 25s"
            ),
        }

        result = structure_patch_detail(raw, self.catalog)
        general = next(section for section in result["sections"] if section["kind"] == "general")
        heroes = {
            section["hero_name"]: section
            for section in result["sections"]
            if section["kind"] == "hero"
        }
        baba = heroes["Baba"]
        baba_abilities = {
            ability["name"]: ability["changes"] for ability in baba["abilities"]
        }
        danny_abilities = {
            ability["name"]: ability["changes"]
            for ability in heroes["Deadman Danny"]["abilities"]
        }

        self.assertEqual(
            baba["changes"],
            [
                "Spirit Resistance scaling reduced from 0.12 to 0.1",
                "Now has -7% Base Bullet Resistance",
            ],
        )
        self.assertEqual(
            baba_abilities["Threadsap"],
            [
                "Spirit reduction on enemies now ends when Baba dies",
                "Cooldown increased from 26s to 28s",
            ],
        )
        self.assertEqual(
            baba_abilities["Granny Long Legs"],
            [
                "Cooldown increased from 30s to 32s",
                "Repeat light melees while airborne now have diminishing forward momentum",
            ],
        )
        self.assertEqual(baba_abilities["Baba's Brew"], ["Barrier reduced from 100 to 90"])
        self.assertEqual(
            general["changes"],
            ["Street Brawl: Baba Threadsap duration lasts 35% less"],
        )
        self.assertEqual(heroes["Deadman Danny"]["changes"], ["Health regen increased from 2.5 to 3.25"])
        self.assertEqual(
            danny_abilities["Chimera Mk1"],
            [
                "Cooldown boon scaling increased from -0.25 to -0.45",
                "Self damage from 6.5% max HP to 5% and is now spirit damage",
            ],
        )
        self.assertEqual(danny_abilities["Strix Salvo"], ["Cooldown from 30s to 25s"])

    def test_mode_heading_scopes_source_rows_and_solomon_move_piece_is_catalogued(self) -> None:
        move_piece_icon = (
            "https://assets-bucket.deadlock-api.com/assets-api-res/images/abilities/"
            "chess/chessmaster_movepiece.png"
        )
        heroes = {
            "solomon": {
                "name": "Solomon",
                "slug": "chessmaster",
                "abilities": {
                    "move piece": {
                        "name": "Move Piece",
                        "icon_url": move_piece_icon,
                        "patch_only": True,
                    },
                },
            },
            "yamato": {
                "name": "Yamato",
                "slug": "yamato",
                "abilities": {
                    "shadow transformation": {
                        "name": "Shadow Transformation",
                        "icon_url": None,
                    },
                },
            },
            "deadman danny": {
                "name": "Deadman Danny",
                "slug": "deadman-danny",
                "abilities": {
                    "chimera mk1": {"name": "Chimera Mk1", "icon_url": None},
                },
            },
            "nurse harrow": {
                "name": "Nurse Harrow",
                "slug": "nurse-harrow",
                "abilities": {
                    "leech therapy": {"name": "Leech Therapy", "icon_url": None},
                    "lurking terrors": {"name": "Lurking Terrors", "icon_url": None},
                },
            },
            "paige": {
                "name": "Paige",
                "slug": "paige",
                "abilities": {"bookwyrm": {"name": "Bookwyrm", "icon_url": None}},
            },
            "rat king": {
                "name": "Rat King",
                "slug": "rat-king",
                "abilities": {
                    "scrap grenade": {"name": "Scrap Grenade", "icon_url": None},
                    "rule, ratannia!": {"name": "Rule, Ratannia!", "icon_url": None},
                },
            },
            "violet": {
                "name": "Violet",
                "slug": "violet",
                "abilities": {
                    "abstract landscape": {"name": "Abstract Landscape", "icon_url": None},
                },
            },
        }
        heroes["nurse"] = heroes["nurse harrow"]
        raw = {
            "id": "1847920302240420",
            "title": "Halloween 2026 + Gameplay Update",
            "content": home_content._plain_text(
                "[Heroes]\n"
                "Solomon: Move Piece - Range to grab pieces increased from 50m to 65m\n"
                "Solomon: Move Piece - Improved placement: pieces no longer fall through geometry or end up under the map\n"
                "Nurse Harrow: Leech Therapy - Leeches on the ground are now removed on round reset\n"
                "Yamato: Shadow Transformation - Cooldown increased from 150s to 160s (moved to T2)\n"
                "[b]Street Brawl:[/b]\n"
                "Deadman Danny: Chimera Mk1 - Cooldown reduction per boon reduced from -0.4s to -0.25s\n"
                "Deadman Danny: Chimera Mk1 - Cooldown refund on hit from T3 reduced from 50% to 30%\n"
                "Nurse Harrow: Leech Therapy - Mode-specific leech duration reduced\n"
                "Nurse: Lurking Terrors - Duration reduced from 7s to 5s\n"
                "Nurse: Lurking Terrors - T3 reduced from +3s Duration to +2s\n"
                "Paige: Bookwyrm - T2 Trail Duration reduced from +2s to +1s\n"
                "Rat King: Scrap Grenade - Radius reduced from 8m to 6m\n"
                "Rat King: Scrap Grenade - Large explosion radius reduced from 14m to 10m\n"
                "Rat King: Rule, Ratannia! - Radius reduced from 20m to 16m\n"
                "Rat King: Rule, Ratannia! - T2 Radius reduced from +8m to +6m\n"
                "Rat King: Rule, Ratannia! - Duration reduced from 12s to 9s\n"
                "Violet: Abstract Landscape - Duration reduced from 6s to 5s\n"
                "Violet: Abstract Landscape - T3 reduced from +4s to +3s",
                max_length=30000,
            ),
        }

        result = structure_patch_detail(raw, {"heroes": heroes, "items": {}, "objectives": {}})
        sections = [section for section in result["sections"] if section["kind"] == "hero"]
        ordinary_nurse = next(
            section for section in sections
            if section["hero_name"] == "Nurse Harrow" and section.get("mode_name") is None
        )
        mode_nurse = next(
            section for section in sections
            if section["hero_name"] == "Nurse Harrow" and section.get("mode_name") == "Street Brawl"
        )
        solomon = next(section for section in sections if section["hero_name"] == "Solomon")
        move_piece = next(ability for ability in solomon["abilities"] if ability["name"] == "Move Piece")
        mode_rows = [
            change
            for section in sections
            if section.get("mode_name") == "Street Brawl"
            for change in section["changes"] + [
                change for ability in section["abilities"] for change in ability["changes"]
            ]
        ]

        self.assertEqual(
            move_piece["changes"],
            [
                "Range to grab pieces increased from 50m to 65m",
                "Improved placement: pieces no longer fall through geometry or end up under the map",
            ],
        )
        self.assertEqual(move_piece["icon_url"], move_piece_icon)
        self.assertEqual(solomon["changes"], [])
        self.assertIsNone(ordinary_nurse.get("mode_name"))
        self.assertEqual(
            [change for ability in ordinary_nurse["abilities"] for change in ability["changes"]],
            ["Leeches on the ground are now removed on round reset"],
        )
        self.assertEqual(mode_nurse["changes"], [])
        self.assertEqual(mode_nurse["abilities"][0]["changes"], ["Mode-specific leech duration reduced"])
        self.assertEqual(len(mode_rows), 13)
        self.assertFalse(any(change == "Street Brawl:" for section in result["sections"] for change in section["changes"]))

        intro = structure_patch_detail(
            {
                "id": "solomon-intro",
                "title": "Solomon is available in-game now",
                "content": "Solomon is available in-game now.",
            },
            {"heroes": heroes, "items": {}, "objectives": {}},
        )
        solomon_intro = next(
            section for section in intro["sections"] if section.get("hero_name") == "Solomon"
        )
        self.assertNotIn("Move Piece", [ability["name"] for ability in solomon_intro["abilities"]])

    def test_legacy_ability_heading_does_not_capture_following_item_or_general_line(self) -> None:
        raw = {
            "id": "7",
            "title": "Patch",
            "content": "Baba: Threadsap\nBasic Magazine: Ammo increased\nGlobal cooldown reduced",
        }

        result = structure_patch_detail(raw, self.catalog)
        baba = next(
            section
            for section in result["sections"]
            if section["kind"] == "hero" and section["hero_name"] == "Baba"
        )
        magazine = next(section for section in result["sections"] if section["kind"] == "item")
        general = next(section for section in result["sections"] if section["kind"] == "general")

        self.assertEqual(baba["abilities"][0]["changes"], [])
        self.assertEqual(magazine["changes"], ["Ammo increased"])
        self.assertEqual(general["changes"], ["Global cooldown reduced"])

    def test_new_catalogued_hero_and_ability_require_no_parser_roster_entry(self) -> None:
        self.catalog["heroes"]["future hero"] = {
            "id": 999,
            "name": "Future Hero",
            "slug": "hero_future_hero",
            "abilities": {
                "new ability": {"id": 99901, "name": "New Ability", "icon_url": None}
            },
        }
        raw = {
            "id": "6",
            "title": "Future Update",
            "content": """[Heroes]
Future Hero: New Ability - Damage increased
""",
        }

        result = structure_patch_detail(raw, self.catalog)
        future = next(
            section
            for section in result["sections"]
            if section["kind"] == "hero" and section["hero_name"] == "Future Hero"
        )
        self.assertEqual(future["abilities"][0]["name"], "New Ability")
        self.assertEqual(future["abilities"][0]["changes"], ["Damage increased"])

    def test_objective_aliases_route_sections_without_stripping_subjects(self) -> None:
        raw = {
            "id": "1836506165584438",
            "title": "Minor Update - 07-09-2026",
            "content": """Unstable Rift warning time reduced from 25s to 20s
Rift Troopers now have Spirit Resist (30/35/40/45%)
Rift Troopers now have Melee Resistance (25%)
Rift Troopers spawn interval increased from every 0.3s to 0.5s (spawns slightly more staggered)
Unstable Rift comeback resist aura radius increased from 20m to 35m
Rift Troopers max comeback count increased from 12 to 14
Urn sprint speed increased from +2m/s to +3m/s
""",
        }

        result = structure_patch_detail(raw, self.catalog)
        objectives = {
            section["objective_key"]: section
            for section in result["sections"]
            if section["kind"] == "objective"
        }

        self.assertEqual(
            objectives["unstable_rift"]["changes"],
            [
                "Unstable Rift warning time reduced from 25s to 20s",
                "Rift Troopers now have Spirit Resist (30/35/40/45%)",
                "Rift Troopers now have Melee Resistance (25%)",
                "Rift Troopers spawn interval increased from every 0.3s to 0.5s (spawns slightly more staggered)",
                "Unstable Rift comeback resist aura radius increased from 20m to 35m",
                "Rift Troopers max comeback count increased from 12 to 14",
            ],
        )
        self.assertEqual(
            objectives["urn"]["changes"],
            ["Urn sprint speed increased from +2m/s to +3m/s"],
        )


if __name__ == "__main__":
    unittest.main()
