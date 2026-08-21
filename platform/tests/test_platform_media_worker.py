from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from apps.platform_worker import worker
from python_packages.platform_infra.media.service import (
    MediaProcessResult,
    MediaReconciliationResult,
)


class AsyncSessionContext:
    def __init__(self, session: object) -> None:
        self.session = session

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, *_: object) -> None:
        return None


class PlatformMediaWorkerConfigurationTests(unittest.TestCase):
    def test_media_tasks_are_bounded_periodic_and_ignore_results(self) -> None:
        self.assertEqual(
            worker.celery_app.conf.beat_schedule["media-reconciliation"],
            {
                "task": "platform.media_reconciliation",
                "schedule": 60.0,
                "options": {"expires": 60.0},
            },
        )
        self.assertEqual(worker.media_reconciliation.name, "platform.media_reconciliation")
        self.assertTrue(worker.media_reconciliation.ignore_result)
        self.assertEqual(worker.media_process_asset.name, "platform.media_process_asset")
        self.assertTrue(worker.media_process_asset.ignore_result)
        self.assertEqual(worker.media_process_asset.soft_time_limit, 90)
        self.assertEqual(worker.media_process_asset.time_limit, 120)


class PlatformMediaWorkerLockTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _redis_client(*, acquired: bool) -> Mock:
        client = Mock()
        client.set = AsyncMock(return_value=acquired)
        client.eval = AsyncMock(return_value=1)
        client.aclose = AsyncMock()
        return client

    async def test_reconciliation_enqueues_only_asset_ids_and_releases_lock(self) -> None:
        client = self._redis_client(acquired=True)
        session = object()
        result = MediaReconciliationResult(
            process_asset_ids=("asset-one", "asset-two"),
            failed_exhausted=1,
            cleaned_assets=2,
            cleaned_sources=3,
        )
        delay = Mock()
        with (
            patch.object(worker, "media_runtime_enabled", return_value=True),
            patch.object(worker, "redis_client", return_value=client),
            patch.object(worker, "token_urlsafe", return_value="media-owner"),
            patch.object(
                worker,
                "session_factory",
                return_value=lambda: AsyncSessionContext(session),
            ),
            patch.object(worker, "reconcile_media_once", AsyncMock(return_value=result)) as reconcile,
            patch.object(worker.media_process_asset, "delay", delay),
        ):
            payload = await worker._run_locked_media_reconciliation()

        self.assertEqual(payload["enqueued"], 2)
        reconcile.assert_awaited_once_with(session, settings=worker.settings)
        self.assertEqual(delay.call_args_list[0].args, ("asset-one",))
        self.assertEqual(delay.call_args_list[1].args, ("asset-two",))
        client.set.assert_awaited_once_with(
            worker.MEDIA_RECONCILIATION_LOCK_KEY,
            "media-owner",
            nx=True,
            ex=worker.MEDIA_RECONCILIATION_LOCK_TTL_SECONDS,
        )
        client.eval.assert_awaited_once_with(
            worker.AUTOMATION_LOCK_RELEASE_SCRIPT,
            1,
            worker.MEDIA_RECONCILIATION_LOCK_KEY,
            "media-owner",
        )
        client.aclose.assert_awaited_once()

    async def test_processing_lock_prevents_parallel_image_work(self) -> None:
        client = self._redis_client(acquired=False)
        with (
            patch.object(worker, "media_runtime_enabled", return_value=True),
            patch.object(worker, "redis_client", return_value=client),
            patch.object(worker, "process_media_asset_once", AsyncMock()) as process,
        ):
            payload = await worker._run_locked_media_process("asset-id")

        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["error_code"], "media_locked")
        process.assert_not_awaited()
        client.eval.assert_not_awaited()
        client.aclose.assert_awaited_once()

    async def test_processing_uses_db_asset_id_and_releases_owned_lock(self) -> None:
        client = self._redis_client(acquired=True)
        session = object()
        result = MediaProcessResult(asset_id="asset-id", status="ready", variants=3)
        with (
            patch.object(worker, "media_runtime_enabled", return_value=True),
            patch.object(worker, "redis_client", return_value=client),
            patch.object(worker, "token_urlsafe", return_value="process-owner"),
            patch.object(
                worker,
                "session_factory",
                return_value=lambda: AsyncSessionContext(session),
            ),
            patch.object(
                worker,
                "process_media_asset_once",
                AsyncMock(return_value=result),
            ) as process,
        ):
            payload = await worker._run_locked_media_process("asset-id")

        self.assertEqual(payload["status"], "ready")
        process.assert_awaited_once_with(session, "asset-id", settings=worker.settings)
        client.eval.assert_awaited_once_with(
            worker.AUTOMATION_LOCK_RELEASE_SCRIPT,
            1,
            worker.MEDIA_PROCESSING_LOCK_KEY,
            "process-owner",
        )


if __name__ == "__main__":
    unittest.main()
