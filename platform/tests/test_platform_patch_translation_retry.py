from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from apps.platform_api.app.services import patch_translation as translation
from apps.platform_api.app.services import patch_translation_runtime as runtime
from python_packages.platform_infra.config import PlatformSettings
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class PatchTranslationRetryTests(PlatformIsolatedAsyncioTestCase):
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

        with patch.object(runtime, "_request_openai", request):
            translated = await runtime._request_openai_with_fact_correction(
                patch_id="1846651706683085",
                source_segments=source_segments,
                prepared_segments=prepared_segments,
                glossary={},
                settings=PlatformSettings(),
                entity_replacements={},
                fact_replacements={"s000-c001": fact_map},
            )

        self.assertEqual(request.await_count, 2)
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
            )

        self.assertEqual(request.await_count, 2)

    async def test_transient_failure_retries_once_with_75_second_budget(self) -> None:
        settings = PlatformSettings(platform_openai_timeout_seconds=30.0)
        first = {
            "ok": False,
            "status": "failed",
            "patch_id": "123",
            "error": "ReadTimeout",
        }
        second = {
            "ok": True,
            "status": "translated",
            "patch_id": "123",
        }
        translator = AsyncMock(side_effect=[first, second])

        with (
            patch.object(translation, "_translate_patch_to_russian", translator),
            patch.object(translation.asyncio, "sleep", AsyncMock()) as sleeper,
        ):
            result = await translation.translate_patch_to_russian(
                {"id": "123"},
                {},
                settings=settings,
            )

        self.assertEqual(result, second)
        self.assertEqual(translator.await_count, 2)
        for call in translator.await_args_list:
            effective_settings = call.kwargs["settings"]
            self.assertEqual(effective_settings.platform_openai_timeout_seconds, 75.0)
        sleeper.assert_awaited_once_with(1.0)

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

    async def test_existing_larger_timeout_is_preserved(self) -> None:
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
        self.assertEqual(effective_settings.platform_openai_timeout_seconds, 90.0)


if __name__ == "__main__":
    unittest.main()
