from __future__ import annotations

import unittest

from apps.platform_api.app.services import patch_translation as translation
from apps.platform_api.app.services import patch_translation_runtime as runtime


class PatchTranslationRealRegressionTests(unittest.TestCase):
    """Lock in the production QA failures found in the 2026-07-28 patch."""

    @staticmethod
    def _catalog() -> dict[str, object]:
        return {
            "items": {
                "bullet_lifesteal": {"name": "Bullet Lifesteal"},
                "spirit_lifesteal": {"name": "Spirit Lifesteal"},
                "melee_lifesteal": {"name": "Melee Lifesteal"},
                "frenzy": {"name": "Frenzy"},
                "spiritual_overflow": {"name": "Spiritual Overflow"},
                "lifestrike": {"name": "Lifestrike"},
            }
        }

    def test_lifesteal_stat_collisions_remain_visible_to_model(self) -> None:
        patch = {
            "sections": [{
                "kind": "item",
                "title": "Regression fixture",
                "item_name": "Regression fixture",
                "changes": [
                    "Gain +10% Bullet Lifesteal.",
                    "Gain +10% Spirit Lifesteal.",
                    "Gain +60% Bullet, Spirit and Melee Lifesteal for 7s.",
                ],
            }],
        }

        prepared, entity_maps, _fact_maps = runtime._prepare_segments(
            patch,
            self._catalog(),
        )

        self.assertIn("Bullet Lifesteal", prepared[0]["text"])
        self.assertIn("Spirit Lifesteal", prepared[1]["text"])
        self.assertIn("Bullet, Spirit and Melee Lifesteal", prepared[2]["text"])
        for replacements in entity_maps.values():
            self.assertNotIn("Bullet Lifesteal", replacements.values())
            self.assertNotIn("Spirit Lifesteal", replacements.values())
            self.assertNotIn("Melee Lifesteal", replacements.values())

    def test_item_reference_collision_keeps_context_for_disambiguation(self) -> None:
        patch = {
            "sections": [{
                "kind": "item",
                "title": "Lifestrike",
                "item_name": "Lifestrike",
                "changes": ["Same change for Melee Lifesteal."],
            }],
        }

        prepared, entity_maps, _fact_maps = runtime._prepare_segments(
            patch,
            self._catalog(),
        )

        self.assertIn("Melee Lifesteal", prepared[0]["text"])
        self.assertEqual(prepared[0]["context"]["item_name"], "Lifestrike")
        self.assertNotIn("Melee Lifesteal", entity_maps["s000-c000"].values())

    def test_latest_patch_regression_concepts_are_covered_by_glossary(self) -> None:
        expected = {
            "Move Speed": "Скорость передвижения",
            "Sprint Speed": "Скорость бега",
            "Fire Rate": "Скорострельность",
            "Spirit Power": "Спиритическая мощь",
            "Bullet Resist": "Сопротивляемость пулям",
            "Spirit Resist": "Сопротивляемость спиритизму",
            "Melee Resist": "Сопротивляемость в ближнем бою",
            "Bullet Lifesteal": "Кража здоровья пулями",
            "Spirit Lifesteal": "Кража здоровья спиритизмом",
            "Melee Lifesteal": "Кража здоровья в ближнем бою",
            "Cooldown": "Перезарядка",
            "Ability Duration": "Длительность умений",
            "Falloff Range": "Эффективная дальность",
        }

        for source, target in expected.items():
            self.assertEqual(translation.CANONICAL_GLOSSARY[source], (target,))

    def test_known_collision_set_stays_explicit_and_minimal(self) -> None:
        self.assertEqual(
            translation.ENTITY_MECHANIC_COLLISIONS,
            ("Bullet Lifesteal", "Spirit Lifesteal", "Melee Lifesteal"),
        )


if __name__ == "__main__":
    unittest.main()
