from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch, sentinel

from python_packages.platform_infra import db


class PlatformDatabaseConfigurationTests(unittest.TestCase):
    def test_engine_pre_pings_connections_before_checkout(self) -> None:
        previous_engine = db._engine
        previous_session_factory = db._session_factory
        db._engine = None
        db._session_factory = None
        settings = Mock(
            platform_database_url="postgresql+asyncpg://platform_user@127.0.0.1/platformdb",
            platform_environment="development",
            platform_db_schema="platform",
            platform_load_test_source_ips="",
            platform_api_workers=2,
            platform_db_pool_size=3,
            platform_db_max_overflow=1,
            platform_db_pool_timeout_seconds=5,
            platform_db_pool_recycle_seconds=1800,
            platform_worker_concurrency=2,
            platform_worker_db_pool_size=2,
            platform_worker_db_max_overflow=0,
            platform_db_connection_budget=20,
        )
        engine = Mock()
        engine.sync_engine = sentinel.sync_engine

        try:
            with (
                patch.object(db, "get_settings", return_value=settings),
                patch.object(db, "create_async_engine", return_value=engine) as create_engine,
                patch.object(db, "async_sessionmaker") as create_session_factory,
                patch.object(db, "install_sqlalchemy_query_metrics") as install_query_metrics,
            ):
                self.assertIs(db.engine(), engine)

            create_engine.assert_called_once_with(
                settings.platform_database_url,
                future=True,
                pool_pre_ping=True,
                pool_size=3,
                max_overflow=1,
                pool_timeout=5,
                pool_recycle=1800,
            )
            install_query_metrics.assert_called_once_with(sentinel.sync_engine)
            create_session_factory.assert_called_once_with(
                engine,
                expire_on_commit=False,
                class_=db.AsyncSession,
            )
        finally:
            db._engine = previous_engine
            db._session_factory = previous_session_factory


class PlatformDatabaseLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispose_detaches_engine_created_on_another_event_loop(self) -> None:
        previous_engine = db._engine
        previous_engine_loop = db._engine_loop
        previous_session_factory = db._session_factory
        old_loop = asyncio.new_event_loop()
        fake_engine = Mock()
        fake_engine.dispose = AsyncMock()
        db._engine = fake_engine
        db._engine_loop = old_loop
        db._session_factory = None

        try:
            await db.dispose_engine()
        finally:
            old_loop.close()
            db._engine = previous_engine
            db._engine_loop = previous_engine_loop
            db._session_factory = previous_session_factory

        fake_engine.dispose.assert_awaited_once_with(close=False)
