from __future__ import annotations

import inspect
import unittest

from apps.platform_api.app.services import patch_translation_runtime as runtime


class PatchTranslationPromptInputTests(unittest.TestCase):
    def test_full_glossary_payload_keeps_every_term_and_variant(self) -> None:
        glossary = {
            "Move Speed": ["Скорость передвижения"],
            "Bullet Resist": ["Сопр. пулям", "Сопротивляемость пулям"],
            "Bullet Lifesteal": ["Кража здоровья пулями"],
        }

        payload = runtime._glossary_payload(glossary)

        self.assertEqual(set(payload), set(glossary))
        self.assertEqual(payload["Move Speed"], ["Скорость передвижения"])
        self.assertEqual(
            payload["Bullet Resist"],
            ["Сопр. пулям", "Сопротивляемость пулям"],
        )
        self.assertEqual(payload["Bullet Lifesteal"], ["Кража здоровья пулями"])

    def test_segment_context_is_read_only_metadata_for_change_text(self) -> None:
        patch = {
            "sections": [{
                "kind": "hero",
                "title": "Haze",
                "hero_name": "Haze",
                "changes": ["Base health increased from 500 to 525."],
                "abilities": [{
                    "name": "Sleep Dagger",
                    "changes": ["Cooldown reduced from 12s to 10s."],
                }],
            }],
        }

        contexts = runtime._segment_contexts(patch)

        self.assertEqual(
            contexts["s000-c000"],
            {
                "section_kind": "hero",
                "section_title": "Haze",
                "hero_name": "Haze",
            },
        )
        self.assertEqual(
            contexts["s000-a000-c000"],
            {
                "section_kind": "hero",
                "section_title": "Haze",
                "hero_name": "Haze",
                "ability_name": "Sleep Dagger",
            },
        )

    def test_ambiguous_item_mechanics_stay_visible_to_model(self) -> None:
        patch = {
            "sections": [{
                "kind": "item",
                "title": "Test Item",
                "item_name": "Test Item",
                "changes": [
                    "Now has +10% Bullet Lifesteal as an innate",
                    "Fleetfoot cooldown reduced from 20s to 18s",
                ],
            }],
        }
        catalog = {
            "items": {
                "bullet_lifesteal": {"name": "Bullet Lifesteal"},
                "fleetfoot": {"name": "Fleetfoot"},
                "test_item": {"name": "Test Item"},
            }
        }

        prepared, entity_maps, _fact_maps = runtime._prepare_segments(patch, catalog)

        self.assertIn("Bullet Lifesteal", prepared[0]["text"])
        self.assertNotIn("Bullet Lifesteal", entity_maps["s000-c000"].values())
        self.assertNotIn("Fleetfoot", prepared[1]["text"])
        self.assertIn("Fleetfoot", entity_maps["s000-c001"].values())

    def test_openai_usage_exposes_cached_input_tokens(self) -> None:
        self.assertEqual(
            runtime._openai_usage({
                "usage": {
                    "input_tokens": 1200,
                    "input_tokens_details": {"cached_tokens": 900},
                    "output_tokens": 300,
                }
            }),
            (1200, 900, 300),
        )

    def test_prompt_cache_key_is_stable_per_translation_version(self) -> None:
        self.assertEqual(runtime._PROMPT_CACHE_KEY, "oldsparky-patch-ru-ru-v9")

    def test_ru_v9_prompt_locks_valve_scaling_and_speed_unit_regressions(self) -> None:
        source = inspect.getsource(runtime._request_openai)

        self.assertIn("коэффициент масштабирования от спиритической", source)
        self.assertIn("Move speed bonus reduced from +3.5m to +2m", source)
        self.assertIn("MUST be rendered as m/s", source)
        self.assertIn("Distance, range and radius values remain m", source)


if __name__ == "__main__":
    unittest.main()
