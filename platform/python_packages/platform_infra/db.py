from __future__ import annotations

from collections.abc import AsyncIterator
from hashlib import sha256
import logging

from sqlalchemy import MetaData, event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session

from python_packages.platform_infra.config import (
    PLATFORM_SCHEMA,
    get_settings,
    validate_platform_settings,
)
from python_packages.platform_infra.performance import install_sqlalchemy_query_metrics

logger = logging.getLogger(__name__)

# Tournament.automation_last_error is part of the public TournamentResponse contract.
# Never persist arbitrary worker/domain exception text into that field.
PUBLIC_AUTOMATION_FAILURE_MESSAGE = "Tournament automation failed. A retry is scheduled."

naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(schema=PLATFORM_SCHEMA, naming_convention=naming_convention)


@event.listens_for(Session, "before_flush")
def _sanitize_public_error_fields(
    session: Session,
    _flush_context: object,
    _instances: object,
) -> None:
    """Fail closed if public ORM fields receive arbitrary internal error text."""

    for instance in session.new.union(session.dirty):
        table = getattr(instance, "__table__", None)
        if getattr(table, "name", None) != "tournaments":
            continue
        raw_error = getattr(instance, "automation_last_error", None)
        if not raw_error or raw_error == PUBLIC_AUTOMATION_FAILURE_MESSAGE:
            continue

        fingerprint = sha256(str(raw_error).encode("utf-8", errors="replace")).hexdigest()[:16]
        logger.warning(
            "Sanitized tournament automation error before persistence "
            "tournament_id=%s failure_count=%s error_fingerprint=%s",
            getattr(instance, "id", None),
            getattr(instance, "automation_failure_count", None),
            fingerprint,
        )
        setattr(instance, "automation_last_error", PUBLIC_AUTOMATION_FAILURE_MESSAGE)


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
