from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from apps.platform_api.app.services import patch_translation as translation
from apps.platform_api.app.services import patch_translation_runtime as runtime
from apps.platform_api.app.services.patch_translation_terms import (
    ProtectedEntityPlaceholderError,
    ProtectedFactPlaceholderError,
)
from python_packages.platform_infra.config import PlatformSettings
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


def _segments(count: int, total_chars: int) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    base_chars, remainder = divmod(total_chars, count)
    source: list[dict[str, str]] = []
    prepared: list[dict[str, object]] = []
    for index in range(count):
        text = "x" * (base_chars + (1 if index < remainder else 0))
        segment_id = f"s000-c{index:03d}"
        source.append({"id": segment_id, "text": text})
        prepared.append({"id": segment_id, "text": text, "context": {"section_kind": "general"}})
    return source, prepared


def _patch_with_segments(source: list[dict[str, str]]) -> dict[str, object]:
    return {
        "id": "1847920302240420",
        "title": "Halloween 2026 + Gameplay Update",
        "sections": [{
            "kind": "general",
            "title": "Общие изменения",
            "changes": [segment["text"] for segment in source],
            "abilities": [],
        }],
    }


class PatchTranslationRetryTests(PlatformIsolatedAsyncioTestCase):
    def test_batch_planner_preserves_large_and_near_limit_inputs(self) -> None:
        for segment_count, total_chars in ((269, 15_659), (269, 30_000), (301, 301)):
            with self.subTest(segment_count=segment_count, total_chars=total_chars):
                source, prepared = _segments(segment_count, total_chars)
                batches = runtime._plan_translation_batches(source, prepared)
                batch_ids = [
                    segment["id"]
                    for source_batch, _ in batches
                    for segment in source_batch
                ]
                self.assertLessEqual(len(batches), 3)
                self.assertEqual(batch_ids, [segment["id"] for segment in source])
                self.assertEqual(len(batch_ids), len(set(batch_ids)))
                self.assertEqual(
                    sum(len(segment["text"]) for batch, _ in batches for segment in batch),
                    total_chars,
                )

        source, prepared = _segments(301, 301)
        self.assertEqual(
            sorted(len(batch) for batch, _ in runtime._plan_translation_batches(source, prepared)),
            [100, 100, 101],
        )

    def test_batch_planner_preserves_article_plus_catalog_description_segments(self) -> None:
        article_source, article_prepared = _segments(269, 30_000)
        description_source, description_prepared = _segments(4, 9_600)
        for index, segment in enumerate(description_source):
            segment["id"] = f"s{index:03d}-hd"
            description_prepared[index]["id"] = segment["id"]
        source = article_source + description_source
        prepared = article_prepared + description_prepared

        batches = runtime._plan_translation_batches(source, prepared)
        batch_ids = [
            segment["id"]
            for source_batch, _ in batches
            for segment in source_batch
        ]
        self.assertLessEqual(len(batches), 3)
        self.assertGreater(sum(len(segment["text"]) for segment in source), 30_000)
        self.assertEqual(batch_ids, [segment["id"] for segment in source])
        self.assertEqual(len(batch_ids), len(set(batch_ids)))

    async def test_large_translation_batches_merge_out_of_order_with_bounded_concurrency(
        self,
    ) -> None:
        source, prepared = _segments(269, 15_659)
        budget = runtime._TranslationProviderCallBudget()
        active = 0
        peak_active = 0

        async def request(**kwargs):
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            ids = [segment["id"] for segment in kwargs["segments"]]
            try:
                # The final source chunk finishes first, exercising ID-based merging.
                await asyncio.sleep(0.03 if ids[0].endswith("200") else 0.001)
                return runtime._OpenAITranslationBatch(
                    {segment_id: f"Перевод {segment_id}" for segment_id in ids},
                    returned_ids=ids,
                    invalid_placeholder_ids=[],
                )
            finally:
                active -= 1

        with patch.object(runtime, "_request_openai", side_effect=request) as provider:
            translated = await runtime._request_openai_batches(
                patch_id="1847920302240420",
                source_segments=source,
                prepared_segments=prepared,
                glossary={},
                settings=PlatformSettings(),
                entity_replacements={},
                fact_replacements={},
                call_budget=budget,
            )

        expected_ids = [segment["id"] for segment in source]
        self.assertEqual(list(translated), expected_ids)
        self.assertEqual(set(translated), set(expected_ids))
        self.assertEqual(provider.await_count, 3)
        self.assertEqual(budget.provider_calls, 3)
        self.assertLessEqual(peak_active, 2)
        self.assertGreaterEqual(peak_active, 2)

    async def test_fact_corrections_across_batches_share_one_follow_up_slot(self) -> None:
        source_text = "Next Tuesday, October 20th at 2pm PDT."
        protected_text, fact_map = translation.protect_facts(source_text)
        source = [
            {"id": f"s000-c{index:03d}", "text": source_text}
            for index in range(150)
        ]
        prepared = [
            {"id": segment["id"], "text": protected_text}
            for segment in source
        ]
        invalid = "Следующий вторник, [[FACT_0000]] октября в [[FACT_0001]]; 99."
        valid = "Следующий вторник, [[FACT_0000]] октября в [[FACT_0001]]."

        async def request(**kwargs):
            ids = [segment["id"] for segment in kwargs["segments"]]
            if kwargs["fact_correction_ids"]:
                return {segment_id: valid for segment_id in ids}
            return runtime._OpenAITranslationBatch(
                {segment_id: invalid for segment_id in ids},
                returned_ids=ids,
                invalid_placeholder_ids=[],
            )

        budget = runtime._TranslationProviderCallBudget()
        with patch.object(runtime, "_request_openai", side_effect=request) as provider:
            translated = await runtime._request_openai_batches(
                patch_id="1847920302240420",
                source_segments=source,
                prepared_segments=prepared,
                glossary={},
                settings=PlatformSettings(),
                entity_replacements={},
                fact_replacements={segment["id"]: fact_map for segment in source},
                call_budget=budget,
            )

        self.assertEqual(set(translated), {segment["id"] for segment in source})
        self.assertEqual(len(translated), len(source))
        self.assertEqual(provider.await_count, 3)
        self.assertEqual(budget.provider_calls, 3)
        self.assertEqual(budget.follow_up_kind, "fact_correction")
        self.assertEqual(
            [segment["id"] for segment in provider.await_args.kwargs["segments"]],
            [segment["id"] for segment in source],
        )

    async def test_placeholder_failures_across_batches_correct_only_invalid_ids(self) -> None:
        source = [
            {"id": f"s000-c{index:03d}", "text": "Nurse Harrow cooldown 10 seconds."}
            for index in range(301)
        ]
        fact_protected, fact_map = translation.protect_facts(source[0]["text"])
        protected_text, entity_map = translation.protect_entities(
            fact_protected, ["Nurse Harrow"]
        )
        prepared = [
            {"id": segment["id"], "text": protected_text}
            for segment in source
        ]
        entity_replacements = {segment["id"]: entity_map for segment in source}
        fact_replacements = {segment["id"]: fact_map for segment in source}
        invalid_ids = ["s000-c000", "s000-c101", "s000-c300"]
        expected_prepared_text = "[[ENTITY_0000]] снизила время перезарядки до [[FACT_0000]] секунд."

        source_entity = "[[ENTITY_0000]] [[FACT_0000]] секунд."
        provider_rows = [
            {"id": "valid", "text": "[[ENTITY_0000]] готово за [[FACT_0000]] секунд."},
            {"id": "bad-entity", "text": "[[ENTITY_0001]] готово за [[FACT_0000]] секунд."},
            {"id": "bad-fact", "text": "[[ENTITY_0000]] готово за [[FACT_0001]] секунд."},
        ]
        provider_payload = {
            "output": [{
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": json.dumps({"segments": provider_rows}),
                }],
            }],
        }

        class FakeResponse:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict[str, object]:
                return provider_payload

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                return FakeResponse()

        provider_segments = [
            {"id": "valid", "text": source_entity},
            {"id": "bad-entity", "text": source_entity},
            {"id": "bad-fact", "text": source_entity},
        ]
        settings = PlatformSettings(
            platform_openai_api_key="test-key",
            platform_openai_model="test-model",
        )
        with patch.object(runtime.httpx, "AsyncClient", return_value=FakeClient()):
            collected = await runtime._request_openai(
                patch_id="1847920302240420",
                segments=provider_segments,
                glossary={},
                settings=settings,
                collect_placeholder_failures=True,
            )
            self.assertIsInstance(collected, runtime._OpenAITranslationBatch)
            self.assertEqual(list(collected), ["valid"])
            self.assertEqual(
                list(collected.invalid_placeholder_ids),
                ["bad-entity", "bad-fact"],
            )

            malformed_rows = [provider_rows[0], provider_rows[0], provider_rows[2]]
            provider_payload["output"][0]["content"][0]["text"] = json.dumps(
                {"segments": malformed_rows}
            )
            with self.assertRaisesRegex(ValueError, "duplicate or unexpected"):
                await runtime._request_openai(
                    patch_id="1847920302240420",
                    segments=provider_segments,
                    glossary={},
                    settings=settings,
                    collect_placeholder_failures=True,
                )

        async def request(**kwargs):
            ids = [segment["id"] for segment in kwargs["segments"]]
            if kwargs["fact_correction_ids"]:
                self.assertEqual(kwargs["fact_correction_ids"], invalid_ids)
                self.assertEqual(ids, invalid_ids)
                return {segment_id: expected_prepared_text for segment_id in ids}
            batch_invalid = [segment_id for segment_id in ids if segment_id in invalid_ids]
            valid_translations = {
                segment_id: expected_prepared_text
                for segment_id in ids
                if segment_id not in batch_invalid
            }
            return runtime._OpenAITranslationBatch(
                valid_translations,
                returned_ids=ids,
                invalid_placeholder_ids=batch_invalid,
            )

        budget = runtime._TranslationProviderCallBudget()
        with patch.object(runtime, "_request_openai", side_effect=request) as provider:
            translated = await runtime._request_openai_batches(
                patch_id="1847920302240420",
                source_segments=source,
                prepared_segments=prepared,
                glossary={},
                settings=PlatformSettings(),
                entity_replacements=entity_replacements,
                fact_replacements=fact_replacements,
                call_budget=budget,
            )
        self.assertEqual(provider.await_count, 4)
        self.assertEqual(budget.provider_calls, 4)
        self.assertEqual(budget.follow_up_kind, "fact_correction")
        self.assertEqual(len(translated), len(source))
        self.assertEqual(set(translated), {segment["id"] for segment in source})
        self.assertEqual(translated["s000-c001"], "Nurse Harrow снизила время перезарядки до 10 секунд.")
        self.assertEqual(
            [segment["id"] for segment in provider.await_args.kwargs["segments"]],
            invalid_ids,
        )

    async def test_protected_numeric_placeholder_failure_gets_one_corrective_retry(self) -> None:
        source = [{"id": "s000-c000", "text": "Nurse Harrow cooldown 10 seconds."}]
        fact_protected, fact_map = translation.protect_facts(source[0]["text"])
        protected_text, entity_map = translation.protect_entities(
            fact_protected, ["Nurse Harrow"]
        )
        prepared = [{"id": "s000-c000", "text": protected_text}]
        with self.assertRaises(ProtectedFactPlaceholderError):
            runtime.validate_prepared_translation(
                prepared[0]["text"], "[[ENTITY_0000]] Cooldown is [[FACT_0001]] seconds."
            )
        with self.assertRaises(ProtectedEntityPlaceholderError):
            runtime.validate_prepared_translation(
                prepared[0]["text"], "[[ENTITY_0001]] Cooldown is [[FACT_0000]] seconds."
            )
        corrected_prepared = "[[ENTITY_0000]] снизила время перезарядки до [[FACT_0000]] секунд."
        initial_result = runtime._OpenAITranslationBatch(
            {},
            returned_ids=["s000-c000"],
            invalid_placeholder_ids=["s000-c000"],
        )
        request = AsyncMock(side_effect=[initial_result, {"s000-c000": corrected_prepared}])
        budget = runtime._TranslationProviderCallBudget()

        with patch.object(runtime, "_request_openai", request):
            translated = await runtime._request_openai_batches(
                patch_id="1847920302240420",
                source_segments=source,
                prepared_segments=prepared,
                glossary={},
                settings=PlatformSettings(),
                entity_replacements={"s000-c000": entity_map},
                fact_replacements={"s000-c000": fact_map},
                call_budget=budget,
            )

        self.assertEqual(
            translated,
            {"s000-c000": "Nurse Harrow снизила время перезарядки до 10 секунд."},
        )
        self.assertEqual(request.await_count, 2)
        self.assertEqual(request.await_args.kwargs["fact_correction_ids"], ["s000-c000"])
        self.assertEqual(
            [segment["id"] for segment in request.await_args.kwargs["segments"]],
            ["s000-c000"],
        )
        self.assertEqual(budget.provider_calls, 2)
        self.assertEqual(budget.follow_up_kind, "fact_correction")

        shared_budget = runtime._TranslationProviderCallBudget()
        shared_budget.begin_call()
        shared_budget.begin_call(follow_up_kind="transient_retry")
        invalid_result = runtime._OpenAITranslationBatch(
            {},
            returned_ids=["s000-c000"],
            invalid_placeholder_ids=["s000-c000"],
        )
        blocked_request = AsyncMock(return_value=invalid_result)
        with (
            patch.object(runtime, "_request_openai", blocked_request),
            self.assertRaises(runtime._ProviderCallBudgetExceeded),
        ):
            await runtime._request_openai_batches(
                patch_id="1847920302240420",
                source_segments=source,
                prepared_segments=prepared,
                glossary={},
                settings=PlatformSettings(),
                entity_replacements={"s000-c000": entity_map},
                fact_replacements={"s000-c000": fact_map},
                call_budget=shared_budget,
            )
        self.assertEqual(blocked_request.await_count, 1)
        self.assertEqual(shared_budget.follow_up_kind, "transient_retry")
        self.assertEqual(shared_budget.provider_calls, 3)


    async def test_transient_retry_and_fact_correction_compete_for_same_slot(self) -> None:
        source_text = "Next Tuesday, October 20th at 2pm PDT."
        protected_text, fact_map = translation.protect_facts(source_text)
        source = [{"id": "s000-c000", "text": source_text}]
        prepared = [{"id": "s000-c000", "text": protected_text}]
        invalid = "Следующий вторник, [[FACT_0000]] октября в [[FACT_0001]]; 99."
        initial_result = runtime._OpenAITranslationBatch(
            {"s000-c000": invalid},
            returned_ids=["s000-c000"],
            invalid_placeholder_ids=[],
        )
        request = AsyncMock(side_effect=[httpx.ReadTimeout("timeout"), initial_result])
        budget = runtime._TranslationProviderCallBudget()

        with (
            patch.object(runtime, "_request_openai", request),
            self.assertRaises(runtime._ProviderCallBudgetExceeded),
        ):
            await runtime._request_openai_batches(
                patch_id="1847920302240420",
                source_segments=source,
                prepared_segments=prepared,
                glossary={},
                settings=PlatformSettings(),
                entity_replacements={},
                fact_replacements={"s000-c000": fact_map},
                call_budget=budget,
            )

        self.assertEqual(request.await_count, 2)
        self.assertEqual(budget.provider_calls, 2)
        self.assertEqual(budget.follow_up_kind, "transient_retry")

    async def test_provider_deadline_caps_call_timeout_and_total_calls(self) -> None:
        budget = runtime._TranslationProviderCallBudget(
            deadline_seconds=0.05,
            max_calls=4,
            call_timeout_seconds=75,
        )
        first_timeout = budget.begin_call()
        self.assertLessEqual(first_timeout, 0.05)
        await asyncio.sleep(0.06)
        with self.assertRaises(TimeoutError):
            budget.begin_call()

        budget = runtime._TranslationProviderCallBudget(max_calls=4)
        for _ in range(3):
            budget.begin_call()
        budget.begin_call(follow_up_kind="transient_retry")
        with self.assertRaises(runtime._ProviderCallBudgetExceeded):
            budget.begin_call()
        self.assertEqual(budget.provider_calls, 4)

    async def test_failed_batch_never_persists_partial_translation(self) -> None:
        source = [
            {"id": f"s000-c{index:03d}", "text": "Cooldown changed from 10 to 12 seconds."}
            for index in range(150)
        ]
        patch_detail = _patch_with_segments(source)
        settings = PlatformSettings(
            platform_openai_api_key="test-key",
            platform_openai_model="test-model",
        )

        async def assert_atomic_failure(error: Exception) -> None:
            cache = SimpleNamespace(
                set=AsyncMock(return_value=True),
                eval=AsyncMock(return_value=1),
                aclose=AsyncMock(),
            )
            first_batch_calls = 0
            sibling_started = asyncio.Event()
            sibling_cancelled = False
            initial_batches = 0

            async def request(**kwargs):
                nonlocal first_batch_calls, sibling_cancelled, initial_batches
                ids = [segment["id"] for segment in kwargs["segments"]]
                if kwargs["fact_correction_ids"]:
                    if isinstance(error, httpx.ReadTimeout):
                        self.fail("A transport retry should have exhausted the follow-up slot.")
                    raise error
                initial_batches += 1
                if not isinstance(error, httpx.ReadTimeout):
                    invalid_id = ids[0] if initial_batches == 1 else ids[-1]
                    valid = {
                        segment["id"]: segment["text"]
                        for segment in kwargs["segments"]
                        if segment["id"] != invalid_id
                    }
                    return runtime._OpenAITranslationBatch(
                        valid,
                        returned_ids=ids,
                        invalid_placeholder_ids=[invalid_id],
                    )
                if ids[0] == "s000-c000":
                    first_batch_calls += 1
                    if first_batch_calls == 1:
                        await asyncio.wait_for(sibling_started.wait(), timeout=1)
                    raise error
                sibling_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    sibling_cancelled = True
                    raise

            with (
                patch.object(
                    runtime,
                    "get_cached_patch_translation",
                    new=AsyncMock(return_value=None),
                ),
                patch.object(runtime, "redis_client", return_value=cache),
                patch.object(
                    runtime,
                    "_claim_translation",
                    new=AsyncMock(
                        return_value=(runtime.TRANSLATION_STATUS_PROCESSING, None)
                    ),
                ) as claim,
                patch.object(
                    runtime, "get_translation_glossary", new=AsyncMock(return_value={})
                ),
                patch.object(runtime, "_request_openai", side_effect=request) as provider,
                patch.object(
                    runtime, "_persist_translation_completion", new=AsyncMock()
                ) as persist,
                patch.object(runtime, "_write_translation_cache", new=AsyncMock()) as cache_write,
                patch.object(runtime, "_set_translation_failure", new=AsyncMock()) as mark_failed,
            ):
                result = await runtime.translate_patch_to_russian(
                    patch_detail,
                    {},
                    settings=settings,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], runtime.TRANSLATION_STATUS_FAILED)
            self.assertEqual(result["error"], type(error).__name__)
            self.assertEqual(provider.await_count, 3)
            if isinstance(error, httpx.ReadTimeout):
                self.assertEqual(first_batch_calls, 2)
                self.assertTrue(sibling_cancelled)
            else:
                self.assertEqual(initial_batches, 2)
                self.assertFalse(sibling_cancelled)
            claim.assert_awaited_once()
            persist.assert_not_awaited()
            cache_write.assert_not_awaited()
            mark_failed.assert_awaited_once()
            cache.eval.assert_awaited_once()
            cache.aclose.assert_awaited_once()

        await assert_atomic_failure(
            ProtectedFactPlaceholderError(
                "OpenAI changed a protected numeric/tier placeholder."
            )
        )
        await assert_atomic_failure(httpx.ReadTimeout("bounded test timeout"))

    async def test_fact_mismatch_gets_one_correction_and_accepts_preserved_facts(
        self,
    ) -> None:
        source_text = "Next Tuesday, October 20th at 2pm PDT."
        protected_text, fact_map = translation.protect_facts(source_text)
        comparison_source = (
            "Resistance reductions are now only 50% as effective against the Mid-Boss."
        )
        comparison_prepared, comparison_fact_map = translation.protect_facts(
            comparison_source
        )
        comparison_token = next(
            token
            for token, value in comparison_fact_map.items()
            if value == "50%"
        )
        source_segments = [
            {"id": "s000-c000", "text": "Violet is available in-game now."},
            {"id": "s000-c001", "text": source_text},
            {"id": "s000-c002", "text": comparison_source},
        ]
        prepared_segments = [
            {
                "id": "s000-c000",
                "text": "Violet is available in-game now.",
            },
            {"id": "s000-c001", "text": protected_text},
            {"id": "s000-c002", "text": comparison_prepared},
        ]
        initial = (
            "Следующий вторник, [[FACT_0000]] октября в [[FACT_0001]] "
            "по тихоокеанскому времени, затем 99."
        )
        corrected = (
            "Следующий вторник, [[FACT_0000]] октября в [[FACT_0001]] "
            "по тихоокеанскому времени."
        )
        reversed_comparison = (
            "Снижение сопротивляемости теперь действует против Mid-Boss "
            f"на {comparison_token} эффективнее."
        )
        corrected_comparison = (
            "Снижение сопротивляемости теперь действует против Mid-Boss "
            f"только на {comparison_token} от прежней эффективности."
        )
        initial_result = runtime._OpenAITranslationBatch(
            {
                "s000-c000": "Виолетта уже доступна в игре.",
                "s000-c001": initial,
                "s000-c002": reversed_comparison,
            },
            returned_ids=["s000-c000", "s000-c001", "s000-c002"],
            invalid_placeholder_ids=[],
        )
        request = AsyncMock(side_effect=[
            initial_result,
            {
                "s000-c001": corrected,
                "s000-c002": corrected_comparison,
            },
        ])
        budget = runtime._TranslationProviderCallBudget()

        with patch.object(runtime, "_request_openai", request):
            translated = await runtime._request_openai_batches(
                patch_id="1846651706683085",
                source_segments=source_segments,
                prepared_segments=prepared_segments,
                glossary={},
                settings=PlatformSettings(),
                entity_replacements={},
                fact_replacements={
                    "s000-c001": fact_map,
                    "s000-c002": comparison_fact_map,
                },
                call_budget=budget,
            )

        self.assertEqual(request.await_count, 2)
        self.assertEqual(budget.provider_calls, 2)
        self.assertEqual(budget.follow_up_kind, "fact_correction")
        self.assertEqual(
            request.await_args_list[1].kwargs["fact_correction_ids"],
            ["s000-c001", "s000-c002"],
        )
        self.assertEqual(
            [segment["id"] for segment in request.await_args_list[1].kwargs["segments"]],
            ["s000-c001", "s000-c002"],
        )
        self.assertEqual(
            translated["s000-c000"], "Виолетта уже доступна в игре."
        )
        self.assertEqual(
            translation._fact_fingerprint(source_text),
            translation._fact_fingerprint(translated["s000-c001"]),
        )
        self.assertTrue(
            translation._preserves_effectiveness_comparison(
                comparison_source,
                translated["s000-c002"],
            )
        )

    async def test_repeated_fact_mismatch_fails_after_one_correction(self) -> None:
        source_text = "Next Tuesday, October 20th at 2pm PDT."
        protected_text, fact_map = translation.protect_facts(source_text)
        source_segments = [{"id": "s000-c001", "text": source_text}]
        prepared_segments = [{"id": "s000-c001", "text": protected_text}]
        invalid = "Следующий вторник, [[FACT_0000]] октября в [[FACT_0001]]; 99."
        request = AsyncMock(
            side_effect=[
                runtime._OpenAITranslationBatch(
                    {"s000-c001": invalid},
                    returned_ids=["s000-c001"],
                    invalid_placeholder_ids=[],
                ),
                {"s000-c001": invalid},
            ]
        )
        budget = runtime._TranslationProviderCallBudget()

        with (
            patch.object(runtime, "_request_openai", request),
            self.assertRaisesRegex(ValueError, "bounded correction"),
        ):
            await runtime._request_openai_batches(
                patch_id="1846651706683085",
                source_segments=source_segments,
                prepared_segments=prepared_segments,
                glossary={},
                settings=PlatformSettings(),
                entity_replacements={},
                fact_replacements={"s000-c001": fact_map},
                call_budget=budget,
            )

        self.assertEqual(request.await_count, 2)
        self.assertEqual(budget.provider_calls, 2)

    async def test_facade_does_not_repeat_runtime_attempt_after_transient_failure(self) -> None:
        settings = PlatformSettings(platform_openai_timeout_seconds=30.0)
        failure = {
            "ok": False,
            "status": "failed",
            "patch_id": "123",
            "error": "ReadTimeout",
        }
        translator = AsyncMock(return_value=failure)

        with patch.object(translation, "_translate_patch_to_russian", translator):
            result = await translation.translate_patch_to_russian(
                {"id": "123"},
                {},
                settings=settings,
            )

        self.assertEqual(result, failure)
        self.assertEqual(translator.await_count, 1)
        self.assertEqual(
            translator.await_args.kwargs["settings"].platform_openai_timeout_seconds,
            75.0,
        )

    async def test_validation_failure_is_not_retried(self) -> None:
        settings = PlatformSettings(platform_openai_timeout_seconds=30.0)
        failure = {
            "ok": False,
            "status": "failed",
            "patch_id": "123",
            "error": "ValueError",
        }
        translator = AsyncMock(return_value=failure)

        with patch.object(translation, "_translate_patch_to_russian", translator):
            result = await translation.translate_patch_to_russian(
                {"id": "123"},
                {},
                settings=settings,
            )

        self.assertEqual(result, failure)
        self.assertEqual(translator.await_count, 1)

    async def test_configured_larger_timeout_is_capped_at_provider_budget(self) -> None:
        settings = PlatformSettings(platform_openai_timeout_seconds=90.0)
        success = {"ok": True, "status": "cached", "patch_id": "123"}
        translator = AsyncMock(return_value=success)

        with patch.object(translation, "_translate_patch_to_russian", translator):
            await translation.translate_patch_to_russian(
                {"id": "123"},
                {},
                settings=settings,
            )

        effective_settings = translator.await_args.kwargs["settings"]
        self.assertEqual(effective_settings.platform_openai_timeout_seconds, 75.0)


if __name__ == "__main__":
    unittest.main()
