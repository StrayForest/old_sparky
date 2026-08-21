from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from python_packages.platform_infra.config import (
    PLATFORM_SCHEMA,
    get_settings,
    validate_platform_settings,
)
from python_packages.platform_infra.performance import install_sqlalchemy_query_metrics

naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(schema=PLATFORM_SCHEMA, naming_convention=naming_convention)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def engine() -> AsyncEngine:
    global _engine, _session_factory

    if _engine is None:
        settings = get_settings()
        validate_platform_settings(settings)
        _engine = create_async_engine(
            settings.platform_database_url,
            future=True,
            pool_pre_ping=True,
        )
        install_sqlalchemy_query_metrics(_engine.sync_engine)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        engine()
    assert _session_factory is not None
    return _session_factory


async def warm_up_engine() -> None:
    async with engine().connect() as connection:
        await connection.execute(text("SELECT 1"))


async def dispose_engine() -> None:
    global _engine, _session_factory

    current_engine = _engine
    _engine = None
    _session_factory = None
    if current_engine is not None:
        await current_engine.dispose()


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with session_factory()() as db_session:
        yield db_session
