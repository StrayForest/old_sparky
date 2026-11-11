from __future__ import annotations

import asyncio
import gzip
import json
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch as mock_patch

from apps.platform_api.app.services import home_content
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

        # Exercise the actual current Steam article with the full retained asset
        # catalog. Rem's cataloged ability is itself named "Gun Damage", so this
        # catches an ENTITY placeholder taking precedence over the mechanic alias.
        fixture_root = Path(__file__).parent / "fixtures" / "patch-acceptance"
        steam_fixture = json.loads(
            (fixture_root / "steam-news-four-patches.json").read_text(encoding="utf-8")
        )
        catalog_fixture = json.loads(
            gzip.decompress(
                (fixture_root / "deadlock-catalog-inputs.json.gz").read_bytes()
            )
        )
        catalog_inputs = catalog_fixture["inputs"]
        full_catalog = home_content._build_asset_catalog(
            {},
            {},
            supplemental_hero_payload=catalog_inputs["heroes_english"],
            supplemental_item_payload=catalog_inputs["items_english"],
            localized_supplemental_hero_payload=catalog_inputs["heroes_russian"],
            localized_supplemental_item_payload=catalog_inputs["items_russian"],
            icon_payload=catalog_inputs["icons"],
        )
        rem_gun_damage = full_catalog["heroes"]["rem"]["abilities"]["gun damage"]
        self.assertEqual(rem_gun_damage["name"], "Gun Damage")
        self.assertEqual(rem_gun_damage["slug"], "ability_familiar_primary_weapon_bubblegun")

        raw_patch = next(
            item for item in steam_fixture["items"]
            if item["gid"] == "1847920302240420"
        )
        source_patch = home_content._structure_patch_detail(
            {**raw_patch, "content": raw_patch["contents"]},
            full_catalog,
        )
        source_patch["id"] = raw_patch["gid"]
        full_source_segments = runtime.extract_translation_segments(source_patch)
        mechanic_ids = [
            segment["id"]
            for segment in full_source_segments
            if runtime._GUN_DAMAGE_MECHANIC_SOURCE_RE.search(segment["text"])
        ]
        self.assertEqual(len(mechanic_ids), 3)
        prepared_full, entities_full, facts_full = runtime._prepare_segments(
            source_patch,
            full_catalog,
        )
        prepared_by_id = {segment["id"]: segment for segment in prepared_full}
        source_by_id = {segment["id"]: segment for segment in full_source_segments}
        mechanic_source = [source_by_id[segment_id] for segment_id in mechanic_ids]
        mechanic_prepared = [prepared_by_id[segment_id] for segment_id in mechanic_ids]
        self.assertTrue(all("Base gun damage" in segment["text"] for segment in mechanic_prepared))
        self.assertTrue(all("[[ENTITY_" not in segment["text"] for segment in mechanic_prepared))

        # The same full catalog still protects a sentence that names the Rem
        # ability, because it is not a source-backed stat use.
        named_ability_patch = {
            "sections": [{
                "kind": "general",
                "title": "Ability reference",
                "changes": ["The Gun Damage ability remains unchanged."],
                "abilities": [],
            }],
        }
        named_prepared, named_entities, _ = runtime._prepare_segments(
            named_ability_patch,
            full_catalog,
        )
        self.assertIn("[[ENTITY_0000]]", named_prepared[0]["text"])
        self.assertEqual(
            named_entities["s000-c000"]["[[ENTITY_0000]]"],
            "Gun Damage",
        )

        class FixtureResponse:
            def __init__(self, rows: list[dict[str, str]]) -> None:
                self.rows = rows

            @staticmethod
            def raise_for_status() -> None:
                return None

            def json(self) -> dict[str, object]:
                return {
                    "output": [{
                        "type": "message",
                        "content": [{
                            "type": "output_text",
                            "text": json.dumps({"segments": self.rows}, ensure_ascii=False),
                        }],
                    }],
                }

        class FixtureClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **kwargs):
                request = kwargs["json"]
                self.calls.append(request)
                request_segments = json.loads(request["input"])["segments"]
                if "CORRECTION ATTEMPT" in request["instructions"]:
                    rows = [
                        {
                            "id": segment["id"],
                            "text": (
                                "Базовый урон от оружия уменьшен на [[FACT_0000]]."
                                if "reduced" in source_by_id[segment["id"]]["text"].lower()
                                else "Базовый урон от оружия увеличен на [[FACT_0000]]."
                            ),
                        }
                        for segment in request_segments
                    ]
                else:
                    rows = [
                        {
                            "id": segment["id"],
                            "text": "Базовый gun damage изменен на [[FACT_0000]].",
                        }
                        for segment in request_segments
                    ]
                return FixtureResponse(rows)

        async def translate_full_source() -> tuple[dict[str, str], FixtureClient, runtime._TranslationProviderCallBudget]:
            client = FixtureClient()
            budget = runtime._TranslationProviderCallBudget()
            with mock_patch.object(runtime.httpx, "AsyncClient", return_value=client):
                translated = await runtime._request_openai_batches(
                    patch_id=source_patch["id"],
                    source_segments=mechanic_source,
                    prepared_segments=mechanic_prepared,
                    glossary=runtime._glossary_payload(translation.CANONICAL_GLOSSARY),
                    settings=PlatformSettings(platform_openai_api_key="fixture-key"),
                    entity_replacements={key: entities_full[key] for key in mechanic_ids},
                    fact_replacements={key: facts_full[key] for key in mechanic_ids},
                    call_budget=budget,
                )
            return translated, client, budget

        full_translated, fixture_client, fixture_budget = asyncio.run(translate_full_source())
        self.assertEqual(len(fixture_client.calls), 2)
        self.assertEqual(fixture_budget.provider_calls, 2)
        self.assertIn("gun damage", fixture_client.calls[0]["instructions"].lower())
        self.assertEqual(
            [segment["id"] for segment in json.loads(fixture_client.calls[1]["input"])["segments"]],
            mechanic_ids,
        )
        self.assertEqual(
            [full_translated[segment_id] for segment_id in mechanic_ids],
            [
                "Базовый урон от оружия увеличен на 5%.",
                "Базовый урон от оружия увеличен на 3%.",
                "Базовый урон от оружия уменьшен на 10%.",
            ],
        )

    def test_known_collision_set_stays_explicit_and_minimal(self) -> None:
        self.assertEqual(
            translation.ENTITY_MECHANIC_COLLISIONS,
            ("Bullet Lifesteal", "Spirit Lifesteal", "Melee Lifesteal", "Gun Damage"),
        )


if __name__ == "__main__":
    unittest.main()
