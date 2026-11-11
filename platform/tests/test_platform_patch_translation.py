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
        source = "Resistance reductions are only 50% as effective against the Mid-Boss."
        self.assertTrue(
            translation._preserves_effectiveness_comparison(
                source,
                "Снижение сопротивляемости действует против Mid-Boss только на 50% от прежней эффективности.",
            )
        )
        self.assertFalse(
            translation._preserves_effectiveness_comparison(
                source,
                "Снижение сопротивляемости действует против Mid-Boss на 50% эффективнее.",
            )
        )
        self.assertTrue(
            translation._preserves_effectiveness_comparison(
                "Damage increased by 50%.",
                "Урон увеличен на 50%.",
            )
        )
        quarter_source = "The effect is only 25% as effective against the Mid-Boss."
        self.assertTrue(
            translation._preserves_effectiveness_comparison(
                quarter_source,
                "Эффект действует против Mid-Boss только с эффективностью 25% от базовой.",
            )
        )
        self.assertTrue(
            translation._preserves_effectiveness_comparison(
                quarter_source,
                "Эффект действует против Mid-Boss эффективно лишь на 25%.",
            )
        )
        self.assertTrue(
            translation._preserves_effectiveness_comparison(
                quarter_source,
                "Эффект эффективен против Mid-Boss только на 25%.",
            )
        )
        self.assertFalse(
            translation._preserves_effectiveness_comparison(
                quarter_source,
                "Эффект действует против Mid-Boss на 25% лучше.",
            )
        )
        self.assertFalse(
            translation._preserves_effectiveness_comparison(
                quarter_source,
                "Эффект действует против Mid-Boss на 25% менее эффективно.",
            )
        )
        for reversed_russian in (
            "Эффект действует против Mid-Boss только на 25% менее эффективно.",
            "Эффект действует против Mid-Boss только на 25% лучше.",
        ):
            with self.subTest(reversed_russian=reversed_russian):
                self.assertFalse(
                    translation._preserves_effectiveness_comparison(
                        quarter_source,
                        reversed_russian,
                    )
                )

    def test_russian_notation_matches_valve_style(self) -> None:
        self.assertEqual(
            translation.localize_russian_notation(
                "Range 28m, cooldown 12s, dash 2m/s, delay 250ms, scaling 0.75."
            ),
            "Range 28 м, cooldown 12 с, dash 2 м/с, delay 250 мс, scaling 0,75.",
        )
        for value in ("10%", "-32%", "50%"):
            source = f'Quoted source value "{value}".'
            protected, replacements = translation.protect_facts(source)
            token = next(key for key, replacement in replacements.items() if replacement == value)
            model_text = f'Перевод с источником "{token}%".'
            restored = translation.restore_placeholders(model_text, replacements)
            self.assertEqual(restored, f'Перевод с источником "{value}".')
            self.assertNotIn(f"{value}%", restored)
        quoted_source = (
            'T3 changed from "Immune to damage while channeling" to '
            '"Takes 50% Reduced Damage while channeling".'
        )
        quoted_prepared, quoted_facts = translation.protect_facts(quoted_source)
        quoted_token = next(token for token, value in quoted_facts.items() if value == "50%")
        quoted_translation = (
            'T3 changed from "Неуязвим к урону во время поддержания" to '
            f'"Получает на {quoted_token}% меньше урона во время поддержания".'
        )
        self.assertIn(quoted_token, quoted_prepared)
        self.assertEqual(
            translation.restore_placeholders(quoted_translation, quoted_facts),
            'T3 changed from "Неуязвим к урону во время поддержания" to '
            '"Получает на 50% меньше урона во время поддержания".',
        )
        literal = translation.restore_placeholders(
            "Literal %% stays; value [[FACT_0000]]%.",
            {"[[FACT_0000]]": "50%"},
        )
        self.assertEqual(literal, "Literal %% stays; value 50%.")

        speed_source = (
            "Move speed reduced from +70% to +50%; dash distance changed from 5m/s to 4m/s."
        )
        speed_prepared, speed_facts = translation.protect_facts(speed_source)
        tokens_by_source = {value: token for token, value in speed_facts.items()}
        self.assertEqual(
            list(speed_facts.values()),
            ["+70%", "+50%", "5", "4"],
        )
        self.assertNotIn("+70%", speed_prepared)
        self.assertIn("m/s", speed_prepared)
        model_text = (
            f"Скорость передвижения снижена с {tokens_by_source['+70%']}% м/с "
            f"до {tokens_by_source['+50%']}% м/с; дальность рывка изменена "
            f"с {tokens_by_source['5']} м/с до {tokens_by_source['4']} м/с."
        )
        restored = translation.restore_placeholders(model_text, speed_facts)
        self.assertEqual(
            restored,
            "Скорость передвижения снижена с +70% до +50%; дальность рывка "
            "изменена с 5 м/с до 4 м/с.",
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
            {"Bullet Lifesteal", "Spirit Lifesteal", "Melee Lifesteal", "Gun Damage"},
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
