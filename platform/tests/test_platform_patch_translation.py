from __future__ import annotations

import asyncio
import unittest

from apps.platform_api.app.services import patch_translation as translation


class PatchTranslationProtectionTests(unittest.TestCase):
    def test_entities_are_placeholdered_and_restored_exactly(self) -> None:
        source = "Haze: Sleep Dagger now applies Fixation stacks."
        protected, replacements = translation.protect_entities(
            source,
            ["Sleep Dagger", "Haze"],
        )
        self.assertNotIn("Haze", protected)
        self.assertNotIn("Sleep Dagger", protected)
        self.assertEqual(translation.restore_placeholders(protected, replacements), source)

    def test_only_numbers_and_tiers_are_fact_placeholders(self) -> None:
        source = "T3 changed from -30% at 20m->58m to -25% at 18m→54m for 0.4s."
        protected, replacements = translation.protect_facts(source)
        self.assertEqual(
            list(replacements.values()),
            ["T3", "-30%", "20", "58", "-25%", "18", "54", "0.4"],
        )
        self.assertIn("m->", protected)
        self.assertIn("m→", protected)
        self.assertIn("s.", protected)
        self.assertEqual(translation.restore_placeholders(protected, replacements), source)

    def test_validation_rejects_missing_numeric_or_tier_placeholder(self) -> None:
        source, _ = translation.protect_facts("T2 Weapon Damage increased from 10% to 12%.")
        translated = source.replace("[[FACT_0001]]", "13%")
        with self.assertRaisesRegex(ValueError, "numeric/tier placeholder"):
            translation._validate_translation(source, translated)

    def test_validation_rejects_missing_entity_placeholder(self) -> None:
        with self.assertRaisesRegex(ValueError, "protected Deadlock entity"):
            translation._validate_translation(
                "[[ENTITY_0000]] cooldown reduced.",
                "Перезарядка уменьшена.",
            )

    def test_fact_fingerprint_accepts_russian_decimal_separator(self) -> None:
        self.assertEqual(
            translation._fact_fingerprint("Damage scaling 0.73 -> 0.63"),
            translation._fact_fingerprint("Масштабирование 0,73 → 0,63"),
        )

    def test_russian_notation_matches_valve_style(self) -> None:
        self.assertEqual(
            translation.localize_russian_notation(
                "Range 28m, cooldown 12s, dash 2m/s, delay 250ms, scaling 0.75."
            ),
            "Range 28 м, cooldown 12 с, dash 2 м/с, delay 250 мс, scaling 0,75.",
        )


class PatchTranslationCanonicalGlossaryTests(unittest.TestCase):
    def test_glossary_is_small_and_contains_core_mechanics(self) -> None:
        glossary = translation.CANONICAL_GLOSSARY

        self.assertGreaterEqual(len(glossary), 40)
        self.assertLessEqual(len(glossary), 80)
        self.assertEqual(glossary["Move Speed"], ("Скорость передвижения",))
        self.assertEqual(glossary["Fire Rate"], ("Скорострельность",))
        self.assertEqual(glossary["Spirit Power"], ("Спиритическая мощь",))
        self.assertEqual(glossary["Bullet Lifesteal"], ("Кража здоровья пулями",))
        self.assertEqual(
            glossary["Spirit Lifesteal"],
            ("Кража здоровья спиритизмом",),
        )
        self.assertEqual(
            glossary["Melee Lifesteal"],
            ("Кража здоровья в ближнем бою",),
        )

    def test_glossary_excludes_entities_and_narrow_property_combinations(self) -> None:
        glossary = translation.CANONICAL_GLOSSARY

        for forbidden in (
            "Haze",
            "Sleep Dagger",
            "Fleetfoot",
            "Cooldown Per Headshot NPC",
            "Ambush Spirit Power",
            "Max Spirit Resist Stolen",
            "Move Speed per Stack",
        ):
            self.assertNotIn(forbidden, glossary)

    def test_entity_mechanic_collisions_are_small_explicit_subset(self) -> None:
        self.assertEqual(
            set(translation.ENTITY_MECHANIC_COLLISIONS),
            {"Bullet Lifesteal", "Spirit Lifesteal", "Melee Lifesteal"},
        )
        self.assertTrue(
            set(translation.ENTITY_MECHANIC_COLLISIONS).issubset(
                translation.CANONICAL_GLOSSARY
            )
        )

    def test_runtime_glossary_is_static_isolated_copy(self) -> None:
        first = asyncio.run(translation.get_translation_glossary(force_refresh=True))
        first["Move Speed"][0] = "сломано"

        second = asyncio.run(translation.get_translation_glossary(force_refresh=True))

        self.assertEqual(second["Move Speed"], ["Скорость передвижения"])
        self.assertEqual(len(second), len(translation.CANONICAL_GLOSSARY))


class PatchTranslationStructureTests(unittest.TestCase):
    def test_only_change_strings_are_replaced(self) -> None:
        patch = {
            "id": "123",
            "sections": [{
                "kind": "hero",
                "title": "Haze",
                "hero_name": "Haze",
                "changes": ["Base health increased from 500 to 525."],
                "abilities": [{
                    "name": "Sleep Dagger",
                    "icon_url": "https://example.invalid/dagger.png",
                    "changes": ["Cooldown reduced from 12s to 10s."],
                }],
            }],
        }
        segments = translation.extract_translation_segments(patch)
        translated = {
            segments[0]["id"]: "Базовое здоровье увеличено с 500 до 525.",
            segments[1]["id"]: "Время перезарядки уменьшено с 12 с до 10 с.",
        }
        result = translation.merge_translation(patch, translated)
        self.assertEqual(result["sections"][0]["title"], "Haze")
        self.assertEqual(result["sections"][0]["hero_name"], "Haze")
        self.assertEqual(result["sections"][0]["abilities"][0]["name"], "Sleep Dagger")
        self.assertEqual(result["sections"][0]["changes"][0], translated[segments[0]["id"]])
        self.assertEqual(
            result["sections"][0]["abilities"][0]["changes"][0],
            translated[segments[1]["id"]],
        )
        self.assertEqual(
            patch["sections"][0]["changes"][0],
            "Base health increased from 500 to 525.",
        )


if __name__ == "__main__":
    unittest.main()
