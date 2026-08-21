#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from python_packages.platform_infra.config import PLATFORM_SCHEMA
from python_packages.platform_infra.db import dispose_engine, engine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Print a read-only PostgreSQL performance baseline for platformdb. "
            "The report contains catalog statistics, not application row data."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum rows per detailed report section (default: 20).",
    )
    parser.add_argument(
        "--statement-timeout-ms",
        type=int,
        default=3000,
        help="Per-query PostgreSQL statement timeout in milliseconds (default: 3000).",
    )
    parser.add_argument(
        "--lock-timeout-ms",
        type=int,
        default=500,
        help="Per-query PostgreSQL lock timeout in milliseconds (default: 500).",
    )
    parser.add_argument(
        "--skip-statements",
        action="store_true",
        help="Do not read normalized pg_stat_statements entries.",
    )
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 200:
        parser.error("--limit must be between 1 and 200")
    if args.statement_timeout_ms < 100 or args.statement_timeout_ms > 60_000:
        parser.error("--statement-timeout-ms must be between 100 and 60000")
    if args.lock_timeout_ms < 0 or args.lock_timeout_ms > 10_000:
        parser.error("--lock-timeout-ms must be between 0 and 10000")
    return args


def human_bytes(value: int | float | None) -> str:
    if value is None:
        return "-"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024.0 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{amount:.1f} TiB"


def human_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "-"
    value = float(seconds)
    if value < 1:
        return f"{value * 1000:.1f} ms"
    if value < 60:
        return f"{value:.2f} s"
    return f"{value / 60:.2f} min"


def format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def print_section(title: str) -> None:
    print(f"\n## {title}")


def print_mapping(values: Mapping[str, Any]) -> None:
    for key, value in values.items():
        print(f"{key}: {format_value(value)}")


def print_table(
    rows: Iterable[Mapping[str, Any]],
    columns: tuple[tuple[str, str], ...],
) -> None:
    row_list = list(rows)
    if not row_list:
        print("(none)")
        return

    rendered = [
        [format_value(row.get(key)) for key, _ in columns]
        for row in row_list
    ]
    widths = [
        max(len(label), *(len(row[index]) for row in rendered))
        for index, (_, label) in enumerate(columns)
    ]
    print("  ".join(label.ljust(widths[index]) for index, (_, label) in enumerate(columns)))
    print("  ".join("-" * width for width in widths))
    for row in rendered:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


async def fetch_one(connection, statement: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await connection.execute(text(statement), parameters or {})
    row = result.mappings().one()
    return dict(row)


async def fetch_all(connection, statement: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = await connection.execute(text(statement), parameters or {})
    return [dict(row) for row in result.mappings().all()]


async def run_audit(args: argparse.Namespace) -> None:
    async with engine().connect() as connection:
        async with connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            await connection.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": f"{args.statement_timeout_ms}ms"},
            )
            await connection.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": f"{args.lock_timeout_ms}ms"},
            )
            await connection.execute(
                text(
                    "SELECT set_config("
                    "'idle_in_transaction_session_timeout', '10s', true"
                    ")"
                )
            )

            database = await fetch_one(
                connection,
                """
                SELECT
                    current_database() AS database_name,
                    current_user AS database_user,
                    current_setting('server_version') AS server_version,
                    pg_database_size(current_database()) AS database_bytes,
                    current_setting('max_connections')::integer AS max_connections,
                    current_setting('shared_buffers') AS shared_buffers,
                    current_setting('effective_cache_size') AS effective_cache_size,
                    current_setting('work_mem') AS work_mem,
                    current_setting('maintenance_work_mem') AS maintenance_work_mem,
                    current_setting('track_io_timing') AS track_io_timing,
                    COALESCE(
                        (
                            SELECT setting
                            FROM pg_settings
                            WHERE name = 'shared_preload_libraries'
                        ),
                        '(restricted)'
                    ) AS shared_preload_libraries
                """,
            )
            if database["database_name"] != "platformdb":
                raise RuntimeError(
                    "Refusing to audit unexpected database "
                    f"{database['database_name']!r}; expected 'platformdb'."
                )

            connections = await fetch_all(
                connection,
                """
                SELECT
                    COALESCE(state, 'background') AS state,
                    count(*)::integer AS connection_count
                FROM pg_stat_activity
                WHERE datname = current_database()
                GROUP BY COALESCE(state, 'background')
                ORDER BY connection_count DESC, state
                """,
            )
            transaction_age = await fetch_one(
                connection,
                """
                SELECT
                    count(*) FILTER (
                        WHERE xact_start IS NOT NULL
                          AND pid <> pg_backend_pid()
                    )::integer AS open_transactions,
                    COALESCE(
                        max(EXTRACT(EPOCH FROM (clock_timestamp() - xact_start))) FILTER (
                            WHERE xact_start IS NOT NULL
                              AND pid <> pg_backend_pid()
                        ),
                        0
                    )::double precision AS oldest_transaction_seconds
                FROM pg_stat_activity
                WHERE datname = current_database()
                """,
            )
            database_stats = await fetch_one(
                connection,
                """
                SELECT
                    xact_commit,
                    xact_rollback,
                    blks_read,
                    blks_hit,
                    temp_files,
                    temp_bytes,
                    deadlocks,
                    conflicts,
                    stats_reset
                FROM pg_stat_database
                WHERE datname = current_database()
                """,
            )
            block_total = int(database_stats["blks_read"]) + int(database_stats["blks_hit"])
            cache_hit_percent = (
                round(int(database_stats["blks_hit"]) / block_total * 100, 3)
                if block_total
                else 100.0
            )

            table_rows = await fetch_all(
                connection,
                """
                SELECT
                    relname AS table_name,
                    n_live_tup::bigint AS live_rows,
                    n_dead_tup::bigint AS dead_rows,
                    seq_scan::bigint,
                    idx_scan::bigint,
                    pg_total_relation_size(relid) AS total_bytes,
                    pg_relation_size(relid) AS table_bytes,
                    pg_indexes_size(relid) AS index_bytes,
                    last_autovacuum,
                    last_autoanalyze
                FROM pg_stat_user_tables
                WHERE schemaname = :schema
                ORDER BY pg_total_relation_size(relid) DESC, relname
                LIMIT :limit
                """,
                {"schema": PLATFORM_SCHEMA, "limit": args.limit},
            )
            for row in table_rows:
                row["total_size"] = human_bytes(row.pop("total_bytes"))
                row["table_size"] = human_bytes(row.pop("table_bytes"))
                row["index_size"] = human_bytes(row.pop("index_bytes"))

            index_rows = await fetch_all(
                connection,
                """
                SELECT
                    relname AS table_name,
                    indexrelname AS index_name,
                    idx_scan::bigint,
                    pg_relation_size(indexrelid) AS index_bytes
                FROM pg_stat_user_indexes
                WHERE schemaname = :schema
                ORDER BY idx_scan ASC, pg_relation_size(indexrelid) DESC, indexrelname
                LIMIT :limit
                """,
                {"schema": PLATFORM_SCHEMA, "limit": args.limit},
            )
            for row in index_rows:
                row["index_size"] = human_bytes(row.pop("index_bytes"))

            duplicate_indexes = await fetch_all(
                connection,
                """
                SELECT
                    table_class.relname AS table_name,
                    string_agg(index_class.relname, ', ' ORDER BY index_class.relname) AS indexes,
                    string_agg(
                        CASE WHEN index_data.indisunique THEN 'unique' ELSE 'non-unique' END,
                        ', '
                        ORDER BY index_class.relname
                    ) AS uniqueness,
                    pg_get_indexdef(min(index_data.indexrelid::bigint)::oid) AS representative_definition
                FROM pg_index AS index_data
                JOIN pg_class AS table_class
                  ON table_class.oid = index_data.indrelid
                JOIN pg_namespace AS table_namespace
                  ON table_namespace.oid = table_class.relnamespace
                JOIN pg_class AS index_class
                  ON index_class.oid = index_data.indexrelid
                WHERE table_namespace.nspname = :schema
                  AND index_data.indisvalid
                  AND index_data.indisready
                GROUP BY
                    table_class.relname,
                    index_data.indrelid,
                    index_data.indkey::text,
                    index_data.indclass::text,
                    index_data.indcollation::text,
                    pg_get_expr(index_data.indexprs, index_data.indrelid),
                    pg_get_expr(index_data.indpred, index_data.indrelid)
                HAVING count(*) > 1
                ORDER BY table_class.relname
                """,
                {"schema": PLATFORM_SCHEMA},
            )

            extension_rows = await fetch_all(
                connection,
                """
                SELECT extname, extversion
                FROM pg_extension
                ORDER BY extname
                """,
            )
            extension_names = {str(row["extname"]) for row in extension_rows}

            statement_rows: list[dict[str, Any]] = []
            statement_error: str | None = None
            if "pg_stat_statements" in extension_names and not args.skip_statements:
                try:
                    async with connection.begin_nested():
                        statement_rows = await fetch_all(
                            connection,
                            """
                            SELECT
                                calls::bigint,
                                round(total_exec_time::numeric, 2) AS total_ms,
                                round(mean_exec_time::numeric, 2) AS mean_ms,
                                rows::bigint,
                                shared_blks_hit::bigint,
                                shared_blks_read::bigint,
                                temp_blks_written::bigint,
                                left(
                                    regexp_replace(query, '[[:space:]]+', ' ', 'g'),
                                    240
                                ) AS query
                            FROM pg_stat_statements
                            WHERE dbid = (
                                SELECT oid
                                FROM pg_database
                                WHERE datname = current_database()
                            )
                              AND userid = (
                                  SELECT usesysid
                                  FROM pg_user
                                  WHERE usename = current_user
                              )
                              AND query NOT ILIKE '%pg_stat_statements%'
                            ORDER BY total_exec_time DESC
                            LIMIT :limit
                            """,
                            {"limit": args.limit},
                        )
                except SQLAlchemyError as exc:
                    statement_error = str(exc).splitlines()[0]

    print("# Platform PostgreSQL Performance Audit")
    print("mode: read-only")
    print(f"schema: {PLATFORM_SCHEMA}")

    print_section("Database")
    print_mapping(
        {
            "database": database["database_name"],
            "user": database["database_user"],
            "server_version": database["server_version"],
            "database_size": human_bytes(database["database_bytes"]),
            "max_connections": database["max_connections"],
            "shared_buffers": database["shared_buffers"],
            "effective_cache_size": database["effective_cache_size"],
            "work_mem": database["work_mem"],
            "maintenance_work_mem": database["maintenance_work_mem"],
            "track_io_timing": database["track_io_timing"],
            "shared_preload_libraries": database["shared_preload_libraries"] or "(none)",
        }
    )

    print_section("Connections")
    print_table(
        connections,
        (
            ("state", "state"),
            ("connection_count", "count"),
        ),
    )
    print_mapping(
        {
            "open_transactions": transaction_age["open_transactions"],
            "oldest_transaction": human_duration(
                transaction_age["oldest_transaction_seconds"]
            ),
        }
    )

    print_section("Database Statistics")
    print_mapping(
        {
            "transactions_committed": database_stats["xact_commit"],
            "transactions_rolled_back": database_stats["xact_rollback"],
            "cache_hit_percent": cache_hit_percent,
            "temp_files": database_stats["temp_files"],
            "temp_bytes": human_bytes(database_stats["temp_bytes"]),
            "deadlocks": database_stats["deadlocks"],
            "conflicts": database_stats["conflicts"],
            "stats_reset": database_stats["stats_reset"],
        }
    )

    print_section("Largest Tables")
    print_table(
        table_rows,
        (
            ("table_name", "table"),
            ("live_rows", "live"),
            ("dead_rows", "dead"),
            ("seq_scan", "seq"),
            ("idx_scan", "idx"),
            ("total_size", "total"),
            ("table_size", "heap"),
            ("index_size", "indexes"),
            ("last_autovacuum", "last autovacuum"),
            ("last_autoanalyze", "last autoanalyze"),
        ),
    )

    print_section("Least-Used Indexes")
    print_table(
        index_rows,
        (
            ("table_name", "table"),
            ("index_name", "index"),
            ("idx_scan", "scans"),
            ("index_size", "size"),
        ),
    )

    print_section("Potentially Redundant Indexes")
    print_table(
        duplicate_indexes,
        (
            ("table_name", "table"),
            ("indexes", "indexes"),
            ("uniqueness", "types"),
            ("representative_definition", "representative definition"),
        ),
    )

    print_section("Extensions")
    print_table(
        extension_rows,
        (
            ("extname", "extension"),
            ("extversion", "version"),
        ),
    )

    print_section("Top Statements")
    if args.skip_statements:
        print("skipped by --skip-statements")
    elif "pg_stat_statements" not in extension_names:
        print("pg_stat_statements is not installed")
    elif statement_error:
        print(f"unavailable: {statement_error}")
    else:
        print_table(
            statement_rows,
            (
                ("calls", "calls"),
                ("total_ms", "total ms"),
                ("mean_ms", "mean ms"),
                ("rows", "rows"),
                ("shared_blks_hit", "hit"),
                ("shared_blks_read", "read"),
                ("temp_blks_written", "temp write"),
                ("query", "normalized query"),
            ),
        )


async def async_main() -> None:
    args = parse_args()
    try:
        await run_audit(args)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(async_main())
