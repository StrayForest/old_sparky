from __future__ import annotations

import unittest

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

    def test_rift_troopers_remain_the_subject_inside_unstable_rift_section(self) -> None:
        raw = {
            "id": "1836506165584438",
            "title": "Minor Update - 07-09-2026",
            "content": """Unstable Rift warning time reduced from 25s to 20s
Rift Troopers now have Spirit Resist (30/35/40/45%)
Rift Troopers now have Melee Resistance (25%)
Rift Troopers spawn interval increased from every 0.3s to 0.5s (spawns slightly more staggered)
Unstable Rift comeback resist aura radius increased from 20m to 35m
Rift Troopers max comeback count increased from 12 to 14
""",
        }

        result = structure_patch_detail(raw, self.catalog)
        objectives = [
            section
            for section in result["sections"]
            if section["kind"] == "objective" and section["objective_key"] == "unstable_rift"
        ]

        self.assertEqual(len(objectives), 1)
        self.assertEqual(
            objectives[0]["changes"],
            [
                "Warning time reduced from 25s to 20s",
                "Rift Troopers now have Spirit Resist (30/35/40/45%)",
                "Rift Troopers now have Melee Resistance (25%)",
                "Rift Troopers spawn interval increased from every 0.3s to 0.5s (spawns slightly more staggered)",
                "Comeback resist aura radius increased from 20m to 35m",
                "Rift Troopers max comeback count increased from 12 to 14",
            ],
        )


if __name__ == "__main__":
    unittest.main()
