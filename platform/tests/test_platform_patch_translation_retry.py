from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from apps.platform_api.app.services import patch_translation as translation
from apps.platform_api.app.services import patch_translation_runtime as runtime
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
                return {segment_id: f"Перевод {segment_id}" for segment_id in ids}
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
            text = valid if kwargs["fact_correction_ids"] else invalid
            return {segment_id: text for segment_id in ids}

        budget = runtime._TranslationProviderCallBudget()
        with (
            patch.object(runtime, "_request_openai", side_effect=request) as provider,
            self.assertRaises(runtime._ProviderCallBudgetExceeded),
        ):
            await runtime._request_openai_batches(
                patch_id="1847920302240420",
                source_segments=source,
                prepared_segments=prepared,
                glossary={},
                settings=PlatformSettings(),
                entity_replacements={},
                fact_replacements={segment["id"]: fact_map for segment in source},
                call_budget=budget,
            )

        self.assertEqual(provider.await_count, 3)
        self.assertEqual(budget.provider_calls, 3)
        self.assertEqual(budget.follow_up_kind, "fact_correction")

    async def test_protected_numeric_placeholder_failure_gets_one_corrective_retry(self) -> None:
        source = [{"id": "s000-c000", "text": "Cooldown is 10 seconds."}]
        protected_text, fact_map = translation.protect_facts(source[0]["text"])
        prepared = [{"id": "s000-c000", "text": protected_text}]
        with self.assertRaises(runtime.ProtectedFactPlaceholderError):
            runtime.validate_prepared_translation(
                prepared[0]["text"], "Cooldown is [[FACT_0001]] seconds."
            )
        error = runtime.ProtectedFactPlaceholderError(
            "OpenAI changed a protected numeric/tier placeholder."
        )
        request = AsyncMock(
            side_effect=[error, {"s000-c000": "Перезарядка — [[FACT_0000]] секунд."}]
        )
        budget = runtime._TranslationProviderCallBudget()

        with patch.object(runtime, "_request_openai", request):
            translated = await runtime._request_openai_with_fact_correction(
                patch_id="1847920302240420",
                source_segments=source,
                prepared_segments=prepared,
                glossary={},
                settings=PlatformSettings(),
                entity_replacements={},
                fact_replacements={"s000-c000": fact_map},
                call_budget=budget,
            )

        self.assertEqual(translated, {"s000-c000": "Перезарядка — 10 секунд."})
        self.assertEqual(request.await_count, 2)
        self.assertEqual(request.await_args.kwargs["fact_correction_ids"], ["s000-c000"])
        self.assertEqual(budget.provider_calls, 2)
        self.assertEqual(budget.follow_up_kind, "protected_fact_retry")

        shared_budget = runtime._TranslationProviderCallBudget()
        shared_budget.begin_call()
        shared_budget.begin_call(follow_up_kind="fact_correction")
        blocked_request = AsyncMock(side_effect=error)
        with (
            patch.object(runtime, "_request_openai", blocked_request),
            self.assertRaises(runtime.ProtectedFactPlaceholderError),
        ):
            await runtime._request_openai_with_budget(
                call_budget=shared_budget,
                patch_id="1847920302240420",
                segments=prepared,
                glossary={},
                settings=PlatformSettings(),
                fact_correction_ids=None,
            )
        self.assertEqual(blocked_request.await_count, 1)
        self.assertEqual(shared_budget.follow_up_kind, "fact_correction")
        self.assertEqual(shared_budget.provider_calls, 3)

    async def test_transient_retry_and_fact_correction_compete_for_same_slot(self) -> None:
        source_text = "Next Tuesday, October 20th at 2pm PDT."
        protected_text, fact_map = translation.protect_facts(source_text)
        source = [{"id": "s000-c000", "text": source_text}]
        prepared = [{"id": "s000-c000", "text": protected_text}]
        invalid = "Следующий вторник, [[FACT_0000]] октября в [[FACT_0001]]; 99."
        request = AsyncMock(side_effect=[httpx.ReadTimeout("timeout"), {"s000-c000": invalid}])
        budget = runtime._TranslationProviderCallBudget()

        with (
            patch.object(runtime, "_request_openai", request),
            self.assertRaises(runtime._ProviderCallBudgetExceeded),
        ):
            await runtime._request_openai_with_fact_correction(
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
            {"id": f"s000-c{index:03d}", "text": "A small gameplay update."}
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

            async def request(**kwargs):
                nonlocal first_batch_calls, sibling_cancelled
                ids = [segment["id"] for segment in kwargs["segments"]]
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
            self.assertEqual(first_batch_calls, 2)
            self.assertEqual(provider.await_count, 3)
            self.assertTrue(sibling_cancelled)
            claim.assert_awaited_once()
            persist.assert_not_awaited()
            cache_write.assert_not_awaited()
            mark_failed.assert_awaited_once()
            cache.eval.assert_awaited_once()
            cache.aclose.assert_awaited_once()

        await assert_atomic_failure(
            runtime.ProtectedFactPlaceholderError(
                "OpenAI changed a protected numeric/tier placeholder."
            )
        )
        await assert_atomic_failure(httpx.ReadTimeout("bounded test timeout"))

    async def test_fact_mismatch_gets_one_correction_and_accepts_preserved_facts(
        self,
    ) -> None:
        source_text = "Next Tuesday, October 20th at 2pm PDT."
        protected_text, fact_map = translation.protect_facts(source_text)
        source_segments = [
            {"id": "s000-c000", "text": "Violet is available in-game now."},
            {"id": "s000-c001", "text": source_text},
        ]
        prepared_segments = [
            {
                "id": "s000-c000",
                "text": "Violet is available in-game now.",
            },
            {"id": "s000-c001", "text": protected_text},
        ]
        initial = (
            "Следующий вторник, [[FACT_0000]] октября в [[FACT_0001]] "
            "по тихоокеанскому времени, затем 99."
        )
        corrected = (
            "Следующий вторник, [[FACT_0000]] октября в [[FACT_0001]] "
            "по тихоокеанскому времени."
        )
        request = AsyncMock(
            side_effect=[
                {
                    "s000-c000": "Виолетта уже доступна в игре.",
                    "s000-c001": initial,
                },
                {"s000-c001": corrected},
            ]
        )
        budget = runtime._TranslationProviderCallBudget()

        with patch.object(runtime, "_request_openai", request):
            translated = await runtime._request_openai_with_fact_correction(
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
        self.assertEqual(budget.follow_up_kind, "fact_correction")
        self.assertEqual(
            request.await_args_list[1].kwargs["fact_correction_ids"],
            ["s000-c001"],
        )
        self.assertEqual(
            [segment["id"] for segment in request.await_args_list[1].kwargs["segments"]],
            ["s000-c001"],
        )
        self.assertEqual(
            translated["s000-c000"], "Виолетта уже доступна в игре."
        )
        self.assertEqual(
            translation._fact_fingerprint(source_text),
            translation._fact_fingerprint(translated["s000-c001"]),
        )

    async def test_repeated_fact_mismatch_fails_after_one_correction(self) -> None:
        source_text = "Next Tuesday, October 20th at 2pm PDT."
        protected_text, fact_map = translation.protect_facts(source_text)
        source_segments = [{"id": "s000-c001", "text": source_text}]
        prepared_segments = [{"id": "s000-c001", "text": protected_text}]
        invalid = "Следующий вторник, [[FACT_0000]] октября в [[FACT_0001]]; 99."
        request = AsyncMock(
            side_effect=[
                {"s000-c001": invalid},
                {"s000-c001": invalid},
            ]
        )
        budget = runtime._TranslationProviderCallBudget()

        with (
            patch.object(runtime, "_request_openai", request),
            self.assertRaisesRegex(ValueError, "bounded correction"),
        ):
            await runtime._request_openai_with_fact_correction(
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
