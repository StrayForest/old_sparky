from __future__ import annotations

import unittest

from apps.platform_api.app.services import home_content
from apps.platform_api.app.services.patch_detail import structure_patch_detail


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
