from __future__ import annotations

import unittest
from unittest.mock import Mock, patch, sentinel

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
