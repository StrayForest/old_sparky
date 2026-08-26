from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import sha256
import logging
import os
from time import perf_counter

from sqlalchemy import MetaData, event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import DeclarativeBase, Session
from starlette.exceptions import HTTPException

from python_packages.platform_infra.config import (
    PLATFORM_SCHEMA,
    PLATFORM_SSE_DB_POOL_SIZE,
    get_settings,
    validate_platform_settings,
)
from python_packages.platform_infra.performance import (
    install_sqlalchemy_query_metrics,
    record_pool_checkout_wait,
)

logger = logging.getLogger(__name__)

# Tournament.automation_last_error is part of the public TournamentResponse contract.
# Never persist arbitrary worker/domain exception text into that field.
PUBLIC_AUTOMATION_FAILURE_MESSAGE = "Tournament automation failed. A retry is scheduled."
# A stream admission request needs a short DB transaction, but a burst of
# thousands of EventSource opens must not occupy the ordinary API pool. Keep a
# separate explicit two-connection budget for stream admission/revalidation.
SSE_STREAM_DB_CONCURRENCY = 2
SSE_STREAM_DB_POOL_SIZE = PLATFORM_SSE_DB_POOL_SIZE
# This is admission work, not a queue.  Once the two short-lived stream DB
# slots are busy, the client must switch to revision polling instead of
# waiting behind a burst of EventSource handshakes.
SSE_STREAM_DB_ACQUIRE_TIMEOUT_SECONDS = 0.5
SSE_STREAM_DB_ADMISSION_RETRY_AFTER_SECONDS = 1


class SseStreamDbAdmissionUnavailable(RuntimeError):
    """The short SSE admission DB budget is temporarily saturated."""

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
_stream_engine: AsyncEngine | None = None
_stream_session_factory: async_sessionmaker[AsyncSession] | None = None
_stream_db_concurrency = asyncio.Semaphore(SSE_STREAM_DB_CONCURRENCY)


def engine() -> AsyncEngine:
    global _engine, _session_factory

    if _engine is None:
        settings = get_settings()
        validate_platform_settings(settings)
        is_worker = os.environ.get("PLATFORM_RUNTIME_SERVICE", "").strip().lower() == "worker"
        pool_size = (
            settings.platform_worker_db_pool_size
            if is_worker
            else settings.platform_db_pool_size
        )
        max_overflow = (
            settings.platform_worker_db_max_overflow
            if is_worker
            else settings.platform_db_max_overflow
        )
        pool_timeout = (
            settings.platform_worker_db_pool_timeout_seconds
            if is_worker
            else settings.platform_db_pool_timeout_seconds
        )
        pool_recycle = (
            settings.platform_worker_db_pool_recycle_seconds
            if is_worker
            else settings.platform_db_pool_recycle_seconds
        )
        _engine = create_async_engine(
            settings.platform_database_url,
            future=True,
            pool_pre_ping=True,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
        )
        install_sqlalchemy_query_metrics(_engine.sync_engine)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        engine()
    assert _session_factory is not None
    return _session_factory


def stream_engine() -> AsyncEngine:
    global _stream_engine, _stream_session_factory

    if _stream_engine is None:
        settings = get_settings()
        validate_platform_settings(settings)
        _stream_engine = create_async_engine(
            settings.platform_database_url,
            future=True,
            pool_pre_ping=True,
            pool_size=SSE_STREAM_DB_POOL_SIZE,
            max_overflow=0,
            pool_timeout=SSE_STREAM_DB_ACQUIRE_TIMEOUT_SECONDS,
            pool_recycle=settings.platform_db_pool_recycle_seconds,
        )
        install_sqlalchemy_query_metrics(_stream_engine.sync_engine)
        _stream_session_factory = async_sessionmaker(
            _stream_engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _stream_engine


def stream_session_factory() -> async_sessionmaker[AsyncSession]:
    if _stream_session_factory is None:
        stream_engine()
    assert _stream_session_factory is not None
    return _stream_session_factory


async def warm_up_engine() -> None:
    async with engine().connect() as connection:
        await connection.execute(text("SELECT 1"))


async def dispose_engine() -> None:
    global _engine, _session_factory, _stream_engine, _stream_session_factory

    current_engine = _engine
    current_stream_engine = _stream_engine
    _engine = None
    _session_factory = None
    _stream_engine = None
    _stream_session_factory = None
    if current_engine is not None:
        await current_engine.dispose()
    if current_stream_engine is not None:
        await current_stream_engine.dispose()


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with session_factory()() as db_session:
        checkout_started = perf_counter()
        await db_session.connection()
        record_pool_checkout_wait(perf_counter() - checkout_started)
        yield db_session


async def get_stream_db_session() -> AsyncIterator[AsyncSession]:
    """Open one bounded short-lived DB session for SSE admission."""

    try:
        async with stream_db_session() as db_session:
            yield db_session
    except SseStreamDbAdmissionUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Live-update admission is temporarily full. Use polling.",
            headers={
                "Retry-After": str(SSE_STREAM_DB_ADMISSION_RETRY_AFTER_SECONDS),
                "Cache-Control": "no-store",
            },
        ) from exc


@asynccontextmanager
async def stream_db_session() -> AsyncIterator[AsyncSession]:
    """Use the bounded SSE-only pool for admission and revalidation."""

    try:
        await asyncio.wait_for(
            _stream_db_concurrency.acquire(),
            timeout=SSE_STREAM_DB_ACQUIRE_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise SseStreamDbAdmissionUnavailable(
            "SSE stream admission DB concurrency is temporarily saturated."
        ) from exc

    try:
        async with stream_session_factory()() as db_session:
            checkout_started = perf_counter()
            try:
                async with asyncio.timeout(SSE_STREAM_DB_ACQUIRE_TIMEOUT_SECONDS):
                    await db_session.connection()
            except (SQLAlchemyTimeoutError, TimeoutError) as exc:
                raise SseStreamDbAdmissionUnavailable(
                    "SSE stream admission DB checkout is temporarily saturated."
                ) from exc
            record_pool_checkout_wait(perf_counter() - checkout_started)
            yield db_session
    finally:
        _stream_db_concurrency.release()
