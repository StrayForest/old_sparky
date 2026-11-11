from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import patch as mock_patch

from apps.platform_api.app.services import patch_translation as translation
from apps.platform_api.app.services import patch_translation_runtime as runtime
from python_packages.platform_infra.config import PlatformSettings


class PatchTranslationRealRegressionTests(unittest.TestCase):
    """Lock in translation failures observed in recent production patches."""

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
            "Gun Damage": "Урон от оружия",
            "Move Speed": "Скорость передвижения",
            "Sprint Speed": "Скорость бега",
            "Fire Rate": "Скорострельность",
            "Spirit Power": "Спиритическая мощь",
            "Spirit Scaling": "Коэффициент масштабирования от спиритической мощи",
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

        self.assertEqual(
            translation.CANONICAL_GLOSSARY["Gun Damage"],
            translation.CANONICAL_GLOSSARY["Weapon Damage"],
        )

        patch = {
            "sections": [{
                "kind": "hero",
                "title": "Abrams",
                "hero_name": "Abrams",
                "changes": ["Base gun damage increased by 5%."],
                "abilities": [],
            }],
        }
        catalog = {
            "heroes": {
                "hero_atlas": {
                    "class_name": "hero_atlas",
                    "name": "Abrams",
                    "abilities": {},
                }
            },
            "items": {},
            "ranks": {},
        }
        prepared, entity_maps, fact_maps = runtime._prepare_segments(patch, catalog)
        source_segments = runtime.extract_translation_segments(patch)
        segment_id = source_segments[0]["id"]
        self.assertIn("Base gun damage increased by", prepared[0]["text"])
        self.assertNotIn("Gun Damage", entity_maps[segment_id].values())
        self.assertEqual(fact_maps[segment_id], {"[[FACT_0000]]": "5%"})

        async def translate_with_glossary_alias() -> dict[str, str]:
            calls = 0

            async def provider_stub(**kwargs: Any) -> runtime._OpenAITranslationBatch:
                nonlocal calls
                calls += 1
                self.assertEqual(
                    kwargs["glossary"]["Gun Damage"],
                    ["Урон от оружия"],
                )
                self.assertEqual(kwargs["segments"][0]["id"], segment_id)
                if calls == 1:
                    self.assertTrue(kwargs["collect_placeholder_failures"])
                    return runtime._OpenAITranslationBatch(
                        {},
                        returned_ids=[segment_id],
                        invalid_placeholder_ids=[],
                        invalid_mechanic_ids=[segment_id],
                    )
                self.assertEqual(kwargs["fact_correction_ids"], [segment_id])
                self.assertFalse(kwargs["collect_placeholder_failures"])
                return runtime._OpenAITranslationBatch(
                    {segment_id: "Базовый урон от оружия увеличен на [[FACT_0000]]."},
                    returned_ids=[segment_id],
                    invalid_placeholder_ids=[],
                )

            with mock_patch.object(runtime, "_request_openai", side_effect=provider_stub):
                return await runtime._request_openai_batches(
                    patch_id="1847920302240420",
                    source_segments=source_segments,
                    prepared_segments=prepared,
                    glossary=runtime._glossary_payload(translation.CANONICAL_GLOSSARY),
                    settings=PlatformSettings(),
                    entity_replacements=entity_maps,
                    fact_replacements=fact_maps,
                    call_budget=runtime._TranslationProviderCallBudget(),
                )

        restored = asyncio.run(translate_with_glossary_alias())
        self.assertEqual(
            restored[segment_id],
            "Базовый урон от оружия увеличен на 5%.",
        )

    def test_known_collision_set_stays_explicit_and_minimal(self) -> None:
        self.assertEqual(
            translation.ENTITY_MECHANIC_COLLISIONS,
            ("Bullet Lifesteal", "Spirit Lifesteal", "Melee Lifesteal"),
        )


if __name__ == "__main__":
    unittest.main()
