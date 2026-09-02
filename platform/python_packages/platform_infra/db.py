from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, AsyncGenerator
from contextlib import asynccontextmanager
from hashlib import sha256
import logging
import os
from time import perf_counter

from sqlalchemy import MetaData, event, text
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session
from starlette.requests import Request

from python_packages.platform_infra.config import (
    PLATFORM_SCHEMA,
    get_settings,
    validate_platform_settings,
)
from python_packages.platform_infra.performance import (
    install_sqlalchemy_query_metrics,
    record_ready_vote_checkout,
    record_pool_checkout_wait,
)

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
_engine_loop: asyncio.AbstractEventLoop | None = None


def engine() -> AsyncEngine:
    global _engine, _engine_loop, _session_factory

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
            pool_pre_ping=settings.platform_db_pool_pre_ping,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
        )
        try:
            _engine_loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous configuration tests can construct the engine without
            # a running loop. Runtime callers create it from their event loop.
            _engine_loop = None
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
    global _engine, _engine_loop, _session_factory

    current_engine = _engine
    created_on_loop = _engine_loop
    _engine = None
    _engine_loop = None
    _session_factory = None
    if current_engine is not None:
        current_loop = asyncio.get_running_loop()
        # IsolatedAsyncioTestCase creates a new loop for each test. An
        # asyncpg connection belonging to a closed/other loop cannot be
        # awaited during normal pool disposal; detach that pool instead of
        # emitting a cross-loop close error. Same-loop application shutdown
        # retains the normal close semantics.
        await current_engine.dispose(
            close=created_on_loop is None or created_on_loop is current_loop
        )


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield the request session without pinning a connection on Ready Vote.

    Tournament router policy dependencies are shared by every tournament
    route.  They are intentionally no-ops for the Ready Vote endpoint, but
    FastAPI still resolves their session dependency.  Avoid the eager
    checkout for that endpoint so the policy session remains connection-free;
    the route-owned Ready Vote scope owns the only required database
    connection. Other routes retain the eager checkout so pool
    exhaustion remains a bounded, observable 503.
    """

    async with session_factory()() as db_session:
        route = request.scope.get("route")
        route_path = str(getattr(route, "path", "") or "")
        if request.method.upper() == "POST" and route_path.endswith(
            "/deadlock/ready-check/vote"
        ):
            yield db_session
            return

        await checkout_db_connection(db_session)
        yield db_session


async def get_lazy_db_session() -> AsyncIterator[AsyncSession]:
    """Yield a session without a connection checkout before it is needed."""

    async with session_factory()() as db_session:
        yield db_session


async def checkout_db_connection(db_session: AsyncSession) -> None:
    """Acquire the bounded pool connection and record its wait time."""

    checkout_started = perf_counter()
    try:
        await db_session.connection()
    except SQLAlchemyTimeoutError:
        # Keep the performance record useful even when checkout itself fails:
        # the API exception handler turns this into a controlled retryable
        # response, while the middleware records the wait.
        record_pool_checkout_wait(perf_counter() - checkout_started)
        raise
    record_pool_checkout_wait(perf_counter() - checkout_started)


async def release_db_connection(db_session: AsyncSession) -> None:
    """Release an eagerly checked-out connection before response work.

    ``AsyncSession.close`` rolls back an open transaction and returns the
    connection to the pool.  The session object remains safe for FastAPI's
    dependency teardown, which may close it a second time.
    """

    await db_session.close()


@asynccontextmanager
async def ready_vote_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Own the short transaction session used by the Ready Vote route.

    Unlike the request dependency this scope is entered by the route around
    authentication, preflight, the conditional upsert and commit. It forces
    exactly one checkout for the normal path and leaves no session alive while
    the detached response is being built or serialized.
    """

    async with session_factory()() as db_session:
        checkout_started = perf_counter()
        try:
            await db_session.connection()
        except SQLAlchemyTimeoutError:
            record_pool_checkout_wait(perf_counter() - checkout_started)
            raise
        checkout_elapsed = perf_counter() - checkout_started
        record_pool_checkout_wait(checkout_elapsed)
        record_ready_vote_checkout(checkout_elapsed)
        try:
            yield db_session
        except BaseException:
            # Auth/preflight failures and commit errors must release any
            # autobegin transaction before the context closes. This is also
            # safe after the route has already rolled back an integrity error.
            try:
                in_transaction = getattr(db_session, "in_transaction", None)
                if in_transaction is None or in_transaction():
                    await db_session.rollback()
            except Exception:
                logger.exception("ready_vote_transaction_rollback_failed")
            raise
