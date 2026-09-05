from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from apps.platform_api.app.services import patch_translation as translation
from python_packages.platform_infra.config import PlatformSettings
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class PatchTranslationRetryTests(PlatformIsolatedAsyncioTestCase):
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
