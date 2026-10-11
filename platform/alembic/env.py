from __future__ import annotations

import asyncio
from logging.config import fileConfig
import math
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from python_packages.platform_infra.config import get_settings, validate_platform_settings
from python_packages.platform_infra.db import Base
from python_packages.platform_infra import models  # noqa: F401
from tools.platform_migration_support import record_migration_progress

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
validate_platform_settings(settings)
config.set_main_option("sqlalchemy.url", settings.platform_database_url)
target_metadata = Base.metadata

# Alembic runs during the release transaction, so every asyncpg connection and
# statement must have the same bounded database contract as the release
# preflight.  The defaults are intentionally short; the environment may only
# tighten or raise them within this bounded operator contract.
ALEMBIC_DB_CONNECT_TIMEOUT_SECONDS = 30.0
ALEMBIC_DB_COMMAND_TIMEOUT_SECONDS = 30.0
ALEMBIC_DB_STATEMENT_TIMEOUT_MS = 30_000
ALEMBIC_DB_LOCK_TIMEOUT_MS = 30_000
ALEMBIC_DB_TIMEOUT_MAX_SECONDS = 30.0
ALEMBIC_DB_TIMEOUT_MAX_MS = 30_000


def _bounded_seconds(*names: str, default: float) -> float:
    raw = next((os.environ.get(name, "").strip() for name in names if os.environ.get(name, "").strip()), "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{names[0]} must be a finite positive number") from exc
    if not math.isfinite(value) or not 0 < value <= ALEMBIC_DB_TIMEOUT_MAX_SECONDS:
        raise RuntimeError(
            f"{names[0]} must be greater than zero and at most "
            f"{ALEMBIC_DB_TIMEOUT_MAX_SECONDS:g} seconds"
        )
    return value


def _bounded_milliseconds(*names: str, default: int) -> str:
    raw = next((os.environ.get(name, "").strip() for name in names if os.environ.get(name, "").strip()), "")
    if not raw:
        value = default
    elif not raw.isdigit():
        raise RuntimeError(f"{names[0]} must be an integer number of milliseconds")
    else:
        value = int(raw)
    if not 1 <= value <= ALEMBIC_DB_TIMEOUT_MAX_MS:
        raise RuntimeError(
            f"{names[0]} must be between 1 and {ALEMBIC_DB_TIMEOUT_MAX_MS} milliseconds"
        )
    return f"{value}ms"


def alembic_asyncpg_connect_args() -> dict[str, object]:
    """Return the closed, bounded asyncpg connection contract for migrations."""

    return {
        "timeout": _bounded_seconds(
            "PLATFORM_ALEMBIC_DB_CONNECT_TIMEOUT_SECONDS",
            "PLATFORM_DB_CONNECT_TIMEOUT_SECONDS",
            default=ALEMBIC_DB_CONNECT_TIMEOUT_SECONDS,
        ),
        "command_timeout": _bounded_seconds(
            "PLATFORM_ALEMBIC_DB_COMMAND_TIMEOUT_SECONDS",
            "PLATFORM_DB_COMMAND_TIMEOUT_SECONDS",
            default=ALEMBIC_DB_COMMAND_TIMEOUT_SECONDS,
        ),
        "server_settings": {
            "application_name": "oldsparky-alembic",
            "statement_timeout": _bounded_milliseconds(
                "PLATFORM_ALEMBIC_DB_STATEMENT_TIMEOUT_MS",
                "PLATFORM_DB_STATEMENT_TIMEOUT_MS",
                default=ALEMBIC_DB_STATEMENT_TIMEOUT_MS,
            ),
            "lock_timeout": _bounded_milliseconds(
                "PLATFORM_ALEMBIC_DB_LOCK_TIMEOUT_MS",
                "PLATFORM_DB_LOCK_TIMEOUT_MS",
                default=ALEMBIC_DB_LOCK_TIMEOUT_MS,
            ),
        },
    }


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=alembic_asyncpg_connect_args(),
    )

    record_migration_progress("alembic-connection-started")
    connection_opened = False
    try:
        async with connectable.connect() as connection:
            connection_opened = True
            record_migration_progress("alembic-connection-opened")
            record_migration_progress("alembic-migrations-started")
            try:
                await connection.run_sync(do_run_migrations)
            except Exception:
                record_migration_progress("alembic-migrations-error")
                raise
            record_migration_progress("alembic-migrations-completed")
    except Exception:
        if not connection_opened:
            record_migration_progress("alembic-connection-failed")
        raise
    finally:
        await connectable.dispose()
    record_migration_progress("alembic-connection-closed")


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
