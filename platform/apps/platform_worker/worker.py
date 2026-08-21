from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Coroutine
from datetime import UTC, datetime
from secrets import token_urlsafe
from typing import Any, TypeVar

from celery import Celery
from celery.signals import worker_process_shutdown
from fastapi import HTTPException
from sqlalchemy import select

from apps.platform_api.app.services.deadlock_automation import (
    DeadlockAutomationResult,
    run_deadlock_automation_once,
)
from apps.platform_api.app.services.home_content import get_deadlock_asset_catalog
from apps.platform_api.app.services.home_content_runtime import refresh_home_content
from apps.platform_api.app.services.patch_detail import get_patch_detail_source
from apps.platform_api.app.services.patch_translation import (
    PATCH_TRANSLATION_TASK_NAME,
    translate_patch_to_russian,
)
from apps.platform_api.app.services.player_commitments import reconcile_player_commitments
from python_packages.platform_infra.auth_lifecycle import cleanup_auth_lifecycle_records
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.media.tasks import (
    media_runtime_enabled,
    process_media_asset_once,
    reconcile_media_once,
)
from python_packages.platform_infra.models import Tournament
from python_packages.platform_infra.redis import redis_client

T = TypeVar("T")

settings = get_settings()
logger = logging.getLogger(__name__)
_worker_loop: asyncio.AbstractEventLoop | None = None
_worker_loop_pid: int | None = None
AUTOMATION_LOCK_KEY = "platform:deadlock-automation:tick-lock"
AUTOMATION_LOCK_TTL_SECONDS = 300
AUTOMATION_TICK_INTERVAL_SECONDS = 60.0
AUTOMATION_TICK_EXPIRES_SECONDS = AUTOMATION_TICK_INTERVAL_SECONDS
COMMITMENT_RECONCILIATION_LOCK_KEY = "platform:player-commitments:reconcile-lock"
COMMITMENT_RECONCILIATION_INTERVAL_SECONDS = 900.0
COMMITMENT_RECONCILIATION_LOCK_TTL_SECONDS = 600
HOME_CONTENT_REFRESH_INTERVAL_SECONDS = 1800.0
AUTH_LIFECYCLE_CLEANUP_INTERVAL_SECONDS = 3600.0
MEDIA_RECONCILIATION_LOCK_KEY = "platform:media:reconciliation-lock"
MEDIA_RECONCILIATION_INTERVAL_SECONDS = 60.0
MEDIA_RECONCILIATION_LOCK_TTL_SECONDS = 300
MEDIA_PROCESSING_LOCK_KEY = "platform:media:processing-lock"
MEDIA_PROCESSING_LOCK_TTL_SECONDS = 180
AUTOMATION_LOCK_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""
AUTO_ASSIGNMENT_LOCK_PREFIX = "platform:deadlock-auto-assignment:run-lock:"
AUTO_ASSIGNMENT_LOCK_TTL_SECONDS = 900

celery_app = Celery(
    "deadlock_platform",
    broker=settings.platform_celery_broker_url,
    backend=settings.platform_celery_result_backend,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    task_default_queue="deadlock-platform",
    task_track_started=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "deadlock-automation-tick": {
            "task": "platform.deadlock_automation_tick",
            "schedule": AUTOMATION_TICK_INTERVAL_SECONDS,
            "options": {
                "expires": AUTOMATION_TICK_EXPIRES_SECONDS,
            },
        },
        "player-commitment-reconciliation": {
            "task": "platform.player_commitment_reconciliation",
            "schedule": COMMITMENT_RECONCILIATION_INTERVAL_SECONDS,
            "options": {
                "expires": COMMITMENT_RECONCILIATION_INTERVAL_SECONDS,
            },
        },
        "home-content-refresh": {
            "task": "platform.home_content_refresh",
            "schedule": HOME_CONTENT_REFRESH_INTERVAL_SECONDS,
            "options": {
                "expires": HOME_CONTENT_REFRESH_INTERVAL_SECONDS,
            },
        },
        "auth-lifecycle-cleanup": {
            "task": "platform.auth_lifecycle_cleanup",
            "schedule": AUTH_LIFECYCLE_CLEANUP_INTERVAL_SECONDS,
            "options": {
                "expires": AUTH_LIFECYCLE_CLEANUP_INTERVAL_SECONDS,
            },
        },
        "media-reconciliation": {
            "task": "platform.media_reconciliation",
            "schedule": MEDIA_RECONCILIATION_INTERVAL_SECONDS,
            "options": {
                "expires": MEDIA_RECONCILIATION_INTERVAL_SECONDS,
            },
        },
    },
)


@celery_app.task(name="platform.ping")
def ping() -> str:
    return "pong"


def _run_on_worker_loop(coroutine: Coroutine[Any, Any, T]) -> T:
    global _worker_loop, _worker_loop_pid

    current_pid = os.getpid()
    if (
        _worker_loop is None
        or _worker_loop.is_closed()
        or _worker_loop_pid != current_pid
    ):
        _worker_loop = asyncio.new_event_loop()
        _worker_loop_pid = current_pid
    return _worker_loop.run_until_complete(coroutine)


@worker_process_shutdown.connect
def _close_worker_loop(**_: object) -> None:
    global _worker_loop, _worker_loop_pid

    loop = _worker_loop
    _worker_loop = None
    _worker_loop_pid = None
    if loop is None or loop.is_closed():
        return
    try:
        loop.run_until_complete(dispose_engine())
    finally:
        loop.close()


async def _run_locked_deadlock_automation_once() -> dict[str, int]:
    client = redis_client()
    lock_token = token_urlsafe(24)
    acquired = False
    try:
        acquired = bool(
            await client.set(
                AUTOMATION_LOCK_KEY,
                lock_token,
                nx=True,
                ex=AUTOMATION_LOCK_TTL_SECONDS,
            )
        )
        if not acquired:
            logger.warning(
                "Skipping Deadlock automation tick because another tick holds the lock."
            )
            return DeadlockAutomationResult().as_dict()
        return await run_deadlock_automation_once()
    finally:
        if acquired:
            try:
                await client.eval(
                    AUTOMATION_LOCK_RELEASE_SCRIPT,
                    1,
                    AUTOMATION_LOCK_KEY,
                    lock_token,
                )
            except Exception:
                logger.exception(
                    "Failed to release Deadlock automation lock; TTL will expire it."
                )
        await client.aclose()


@celery_app.task(name="platform.deadlock_automation_tick", ignore_result=True)
def deadlock_automation_tick() -> dict[str, int]:
    return _run_on_worker_loop(_run_locked_deadlock_automation_once())


async def _run_locked_player_commitment_reconciliation() -> dict[str, int | bool]:
    client = redis_client()
    lock_token = token_urlsafe(24)
    acquired = False
    try:
        acquired = bool(
            await client.set(
                COMMITMENT_RECONCILIATION_LOCK_KEY,
                lock_token,
                nx=True,
                ex=COMMITMENT_RECONCILIATION_LOCK_TTL_SECONDS,
            )
        )
        if not acquired:
            return {"ok": True, "skipped": True, "released_total": 0}
        async with session_factory()() as db_session:
            result = await reconcile_player_commitments(
                db_session,
                now=datetime.now(UTC),
            )
            await db_session.commit()
        if result.released_total:
            logger.warning(
                "Player commitment reconciliation released %s stale commitments.",
                result.released_total,
            )
        return {
            "ok": True,
            "skipped": False,
            "released_total": result.released_total,
            "terminal_released": result.terminal_released,
            "eliminated_released": result.eliminated_released,
            "mismatched_released": result.mismatched_released,
        }
    finally:
        if acquired:
            try:
                await client.eval(
                    AUTOMATION_LOCK_RELEASE_SCRIPT,
                    1,
                    COMMITMENT_RECONCILIATION_LOCK_KEY,
                    lock_token,
                )
            except Exception:
                logger.exception(
                    "Failed to release player commitment reconciliation lock; TTL will expire it."
                )
        await client.aclose()


@celery_app.task(name="platform.player_commitment_reconciliation", ignore_result=True)
def player_commitment_reconciliation() -> dict[str, int | bool]:
    return _run_on_worker_loop(_run_locked_player_commitment_reconciliation())


def _enqueue_patch_translation_jobs(payload: dict[str, Any]) -> int:
    enqueued = 0
    seen: set[str] = set()
    for patch in payload.get("patches") or []:
        if not isinstance(patch, dict):
            continue
        patch_id = str(patch.get("id") or "").strip()
        if not patch_id.isdigit() or patch_id in seen:
            continue
        seen.add(patch_id)
        try:
            patch_translation.delay(patch_id)
            enqueued += 1
        except Exception:
            logger.exception(
                "Failed to enqueue patch translation patch_id=%s.",
                patch_id,
            )
    return enqueued


@celery_app.task(name="platform.home_content_refresh", ignore_result=True)
def home_content_refresh() -> dict[str, Any]:
    payload = _run_on_worker_loop(refresh_home_content(force=True))
    payload["patch_translations_enqueued"] = _enqueue_patch_translation_jobs(payload)
    return payload


async def _run_patch_translation(patch_id: str) -> dict[str, Any]:
    source = await get_patch_detail_source(patch_id)
    if source is None:
        return {
            "ok": False,
            "status": "not_found",
            "patch_id": patch_id,
        }
    catalog = await get_deadlock_asset_catalog()
    return await translate_patch_to_russian(source, catalog, settings=settings)


@celery_app.task(
    name=PATCH_TRANSLATION_TASK_NAME,
    ignore_result=True,
    soft_time_limit=180,
    time_limit=210,
)
def patch_translation(patch_id: str) -> dict[str, Any]:
    return _run_on_worker_loop(_run_patch_translation(patch_id))


async def _run_auth_lifecycle_cleanup() -> dict[str, int]:
    async with session_factory()() as db_session:
        result = await cleanup_auth_lifecycle_records(db_session)
    return result.as_dict()


@celery_app.task(name="platform.auth_lifecycle_cleanup", ignore_result=True)
def auth_lifecycle_cleanup() -> dict[str, int]:
    return _run_on_worker_loop(_run_auth_lifecycle_cleanup())


async def _release_redis_lock(client: object, lock_key: str, lock_token: str) -> None:
    try:
        await client.eval(
            AUTOMATION_LOCK_RELEASE_SCRIPT,
            1,
            lock_key,
            lock_token,
        )
    except Exception:
        logger.exception("Failed to release %s; TTL will expire it.", lock_key)


async def _run_locked_media_reconciliation() -> dict[str, Any]:
    if not media_runtime_enabled(settings):
        return {"ok": True, "skipped": True, "reason": "media_backend_not_r2"}
    client = redis_client()
    lock_token = token_urlsafe(24)
    acquired = False
    try:
        acquired = bool(
            await client.set(
                MEDIA_RECONCILIATION_LOCK_KEY,
                lock_token,
                nx=True,
                ex=MEDIA_RECONCILIATION_LOCK_TTL_SECONDS,
            )
        )
        if not acquired:
            return {"ok": True, "skipped": True, "reason": "locked"}
        async with session_factory()() as db_session:
            result = await reconcile_media_once(db_session, settings=settings)
        enqueued = 0
        for asset_id in result.process_asset_ids:
            try:
                media_process_asset.delay(asset_id)
                enqueued += 1
            except Exception:
                logger.exception(
                    "Failed to enqueue reconciled media asset %s; it remains durable.",
                    asset_id,
                )
        payload: dict[str, Any] = {"ok": True, "skipped": False, **result.as_dict()}
        payload["enqueued"] = enqueued
        return payload
    finally:
        if acquired:
            await _release_redis_lock(client, MEDIA_RECONCILIATION_LOCK_KEY, lock_token)
        await client.aclose()


@celery_app.task(name="platform.media_reconciliation", ignore_result=True)
def media_reconciliation() -> dict[str, Any]:
    return _run_on_worker_loop(_run_locked_media_reconciliation())


async def _run_locked_media_process(asset_id: str) -> dict[str, Any]:
    if not media_runtime_enabled(settings):
        return {
            "asset_id": asset_id,
            "status": "skipped",
            "error_code": "media_backend_not_r2",
        }
    client = redis_client()
    lock_token = token_urlsafe(24)
    acquired = False
    try:
        acquired = bool(
            await client.set(
                MEDIA_PROCESSING_LOCK_KEY,
                lock_token,
                nx=True,
                ex=MEDIA_PROCESSING_LOCK_TTL_SECONDS,
            )
        )
        if not acquired:
            return {"asset_id": asset_id, "status": "pending", "error_code": "media_locked"}
        async with session_factory()() as db_session:
            result = await process_media_asset_once(
                db_session,
                asset_id,
                settings=settings,
            )
        return result.as_dict()
    finally:
        if acquired:
            await _release_redis_lock(client, MEDIA_PROCESSING_LOCK_KEY, lock_token)
        await client.aclose()


@celery_app.task(
    name="platform.media_process_asset",
    ignore_result=True,
    soft_time_limit=90,
    time_limit=120,
)
def media_process_asset(asset_id: str) -> dict[str, Any]:
    return _run_on_worker_loop(_run_locked_media_process(asset_id))


async def _run_locked_deadlock_auto_assignment(
    tournament_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    from apps.platform_api.app.api.routes import tournaments as tournament_routes

    client = redis_client()
    lock_key = f"{AUTO_ASSIGNMENT_LOCK_PREFIX}{tournament_id}"
    lock_token = token_urlsafe(24)
    acquired = False
    try:
        acquired = bool(
            await client.set(
                lock_key,
                lock_token,
                nx=True,
                ex=AUTO_ASSIGNMENT_LOCK_TTL_SECONDS,
            )
        )
        if not acquired:
            return {
                "ok": False,
                "status": "locked",
                "tournament_id": tournament_id,
                "error": "An auto-assignment run is already queued or running for this tournament.",
            }
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(
                select(Tournament).where(Tournament.id == tournament_id)
            )
            if tournament is None:
                return {
                    "ok": False,
                    "status": "not_found",
                    "tournament_id": tournament_id,
                    "error": "Tournament not found.",
                }
            try:
                run_row = await tournament_routes.generate_deadlock_auto_assignment_run_for_tournament(
                    db_session,
                    tournament=tournament,
                    actor_user_id=actor_user_id,
                )
            except HTTPException as exc:
                return {
                    "ok": False,
                    "status": "failed",
                    "tournament_id": tournament_id,
                    "error": str(exc.detail),
                    "http_status": exc.status_code,
                }
            return {
                "ok": True,
                "status": "generated",
                "tournament_id": tournament_id,
                "run_id": run_row.id,
            }
    finally:
        if acquired:
            try:
                await client.eval(
                    AUTOMATION_LOCK_RELEASE_SCRIPT,
                    1,
                    lock_key,
                    lock_token,
                )
            except Exception:
                logger.exception(
                    "Failed to release Deadlock auto-assignment lock; TTL will expire it."
                )
        await client.aclose()


@celery_app.task(name="platform.deadlock_auto_assignment_run")
def deadlock_auto_assignment_run(
    tournament_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    return _run_on_worker_loop(
        _run_locked_deadlock_auto_assignment(tournament_id, actor_user_id)
    )
