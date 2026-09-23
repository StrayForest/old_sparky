"""Fail-closed recovery for an interrupted tournament catalog migration.

Revision ``20260901_0051`` predates this helper and is intentionally left
unchanged.  Its table and backfill are committed before its concurrent index
builds, so an index failure can leave a usable-looking table while Alembic
still records ``20260901_0050``.  The production Alembic wrapper invokes
``recover_partial_0051`` before the exact ``upgrade head`` command.  Revision
0053 reuses the validation and index repair primitives for databases that
already recorded 0051/0052.

The helper is deliberately conservative:

* an existing table must match the historical table definition exactly;
* a valid, named index with the wrong owner table or definition is an error;
* only an invalid/unfinished index on the expected table is dropped and
  rebuilt; and
* the old revision is stamped only after the table, backfill and every index
  have been checked successfully.

Concurrent index DDL is always run on an AUTOCOMMIT connection.  Table
validation/backfill and Alembic-version stamping each use an explicit
transaction boundary in the command-line recovery path.  The migration path
uses the same primitives around Alembic's explicit autocommit block.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Any

from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

# ``python tools/<script>.py`` puts ``tools/`` (rather than the platform root)
# on ``sys.path``.  Migrations and the release wrapper both use that form.
PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from python_packages.platform_infra.config import (
    get_settings,
    validate_platform_settings,
)


PARTIAL_REVISION = "20260901_0050"
APPLIED_REVISION = "20260901_0051"
ADVISORY_LOCK_KEY = 202609010051


class RecoveryError(RuntimeError):
    """Raised when a partially applied migration is not safely repairable."""


@dataclass(frozen=True, slots=True)
class IndexSpec:
    """Catalog-level contract for one projection index."""

    name: str
    method: str
    keys: tuple[str, ...]
    options: tuple[int, ...]
    opclasses: tuple[str, ...]
    collations: tuple[str, ...]
    predicate: str | None
    create_sql: str


EXPECTED_COLUMNS: tuple[tuple[str, str, str, int | None, str, str | None], ...] = (
    ("id", "character varying", "varchar", 36, "NO", None),
    ("slug", "character varying", "varchar", 140, "NO", None),
    ("name", "character varying", "varchar", 120, "NO", None),
    ("description", "text", "text", None, "YES", None),
    ("cover_url", "character varying", "varchar", 512, "YES", None),
    ("banner_asset_id", "character varying", "varchar", 36, "YES", None),
    ("visibility", "character varying", "varchar", 20, "NO", None),
    ("status", "character varying", "varchar", 20, "NO", None),
    ("format_slug", "character varying", "varchar", 64, "NO", None),
    ("allowed_ranks", "jsonb", "jsonb", None, "NO", None),
    ("max_participants", "integer", "int4", None, "YES", None),
    ("registration_starts_at", "timestamp with time zone", "timestamptz", None, "YES", None),
    ("registration_closes_at", "timestamp with time zone", "timestamptz", None, "YES", None),
    ("ready_check_starts_at", "timestamp with time zone", "timestamptz", None, "YES", None),
    ("ready_check_ends_at", "timestamp with time zone", "timestamptz", None, "YES", None),
    ("captain_selection_starts_at", "timestamp with time zone", "timestamptz", None, "YES", None),
    ("starts_at", "timestamp with time zone", "timestamptz", None, "YES", None),
    ("match_format", "character varying", "varchar", 20, "NO", None),
    ("final_format", "character varying", "varchar", 20, "NO", None),
    ("captain_response_deadline_minutes", "integer", "int4", None, "YES", None),
    ("teams_count", "integer", "int4", None, "YES", None),
    ("automation_ready_check_started_at", "timestamp with time zone", "timestamptz", None, "YES", None),
    ("automation_ready_check_closed_at", "timestamp with time zone", "timestamptz", None, "YES", None),
    ("automation_captain_round_started_at", "timestamp with time zone", "timestamptz", None, "YES", None),
    ("automation_captain_round_finalized_at", "timestamp with time zone", "timestamptz", None, "YES", None),
    ("automation_assignment_generated_at", "timestamp with time zone", "timestamptz", None, "YES", None),
    ("automation_last_error", "text", "text", None, "YES", None),
    ("automation_failure_count", "integer", "int4", None, "NO", "0"),
    ("automation_retry_after", "timestamp with time zone", "timestamptz", None, "YES", None),
    ("organizer_user_id", "character varying", "varchar", 36, "NO", None),
    ("organizer_display_name", "character varying", "varchar", 40, "NO", None),
    ("organizer_avatar_asset_id", "character varying", "varchar", 36, "YES", None),
    ("participant_count", "integer", "int4", None, "NO", "0"),
    ("has_locked_deadlock_roster", "boolean", "bool", None, "NO", "false"),
    ("bracket_revision", "integer", "int4", None, "NO", "0"),
    ("created_at", "timestamp with time zone", "timestamptz", None, "NO", None),
    ("updated_at", "timestamp with time zone", "timestamptz", None, "NO", None),
)


EXPECTED_CONSTRAINTS: dict[str, tuple[str, str]] = {
    "pk_tournament_list_read_models": ("p", "PRIMARY KEY (id)"),
    "uq_tournament_list_read_models_slug": ("u", "UNIQUE (slug)"),
    "fk_tournament_list_read_models_id_tournaments": (
        "f",
        "FOREIGN KEY (id) REFERENCES platform.tournaments(id) ON DELETE CASCADE",
    ),
}


_PUBLIC_PREDICATE = "visibility = 'public' AND format_slug = 'solo'"
_INDEX_SQL_PREFIX = "CREATE INDEX CONCURRENTLY"


INDEX_SPECS: tuple[IndexSpec, ...] = (
    IndexSpec(
        name="ix_tournament_list_public_created_at_id",
        method="btree",
        keys=("created_at", "id"),
        options=(3, 3),
        opclasses=("timestamptz_ops", "text_ops"),
        collations=("none", "default"),
        predicate=_PUBLIC_PREDICATE,
        create_sql=(
            f'{_INDEX_SQL_PREFIX} "ix_tournament_list_public_created_at_id" '
            "ON platform.tournament_list_read_models (created_at DESC, id DESC) "
            "WHERE visibility = 'public' AND format_slug = 'solo'"
        ),
    ),
    IndexSpec(
        name="ix_tournament_list_public_status_created_at_id",
        method="btree",
        keys=("status", "created_at", "id"),
        options=(0, 3, 3),
        opclasses=("text_ops", "timestamptz_ops", "text_ops"),
        collations=("default", "none", "default"),
        predicate=_PUBLIC_PREDICATE,
        create_sql=(
            f'{_INDEX_SQL_PREFIX} "ix_tournament_list_public_status_created_at_id" '
            "ON platform.tournament_list_read_models "
            "(status, created_at DESC, id DESC) "
            "WHERE visibility = 'public' AND format_slug = 'solo'"
        ),
    ),
    IndexSpec(
        name="ix_tournament_list_public_starts_nearest",
        method="btree",
        keys=("starts_at", "created_at", "id"),
        options=(0, 3, 3),
        opclasses=("timestamptz_ops", "timestamptz_ops", "text_ops"),
        collations=("none", "none", "default"),
        predicate=_PUBLIC_PREDICATE,
        create_sql=(
            f'{_INDEX_SQL_PREFIX} "ix_tournament_list_public_starts_nearest" '
            "ON platform.tournament_list_read_models "
            "(starts_at ASC NULLS LAST, created_at DESC, id DESC) "
            "WHERE visibility = 'public' AND format_slug = 'solo'"
        ),
    ),
    IndexSpec(
        name="ix_tournament_list_public_starts_farthest",
        method="btree",
        keys=("starts_at", "created_at", "id"),
        options=(1, 3, 3),
        opclasses=("timestamptz_ops", "timestamptz_ops", "text_ops"),
        collations=("none", "none", "default"),
        predicate=_PUBLIC_PREDICATE,
        create_sql=(
            f'{_INDEX_SQL_PREFIX} "ix_tournament_list_public_starts_farthest" '
            "ON platform.tournament_list_read_models "
            "(starts_at DESC NULLS LAST, created_at DESC, id DESC) "
            "WHERE visibility = 'public' AND format_slug = 'solo'"
        ),
    ),
    IndexSpec(
        name="ix_tournament_list_public_participants_asc",
        method="btree",
        keys=("participant_count", "created_at", "id"),
        options=(0, 3, 3),
        opclasses=("int4_ops", "timestamptz_ops", "text_ops"),
        collations=("none", "none", "default"),
        predicate=_PUBLIC_PREDICATE,
        create_sql=(
            f'{_INDEX_SQL_PREFIX} "ix_tournament_list_public_participants_asc" '
            "ON platform.tournament_list_read_models "
            "(participant_count ASC, created_at DESC, id DESC) "
            "WHERE visibility = 'public' AND format_slug = 'solo'"
        ),
    ),
    IndexSpec(
        name="ix_tournament_list_public_participants_desc",
        method="btree",
        keys=("participant_count", "created_at", "id"),
        options=(3, 3, 3),
        opclasses=("int4_ops", "timestamptz_ops", "text_ops"),
        collations=("none", "none", "default"),
        predicate=_PUBLIC_PREDICATE,
        create_sql=(
            f'{_INDEX_SQL_PREFIX} "ix_tournament_list_public_participants_desc" '
            "ON platform.tournament_list_read_models "
            "(participant_count DESC, created_at DESC, id DESC) "
            "WHERE visibility = 'public' AND format_slug = 'solo'"
        ),
    ),
    IndexSpec(
        name="ix_tournament_list_organizer_created_at_id",
        method="btree",
        keys=("organizer_user_id", "created_at", "id"),
        options=(0, 3, 3),
        opclasses=("text_ops", "timestamptz_ops", "text_ops"),
        collations=("default", "none", "default"),
        predicate=None,
        create_sql=(
            f'{_INDEX_SQL_PREFIX} "ix_tournament_list_organizer_created_at_id" '
            "ON platform.tournament_list_read_models "
            "(organizer_user_id, created_at DESC, id DESC)"
        ),
    ),
    IndexSpec(
        name="ix_tournament_list_allowed_ranks_gin",
        method="gin",
        keys=("allowed_ranks",),
        options=(0,),
        opclasses=("jsonb_ops",),
        collations=("none",),
        predicate=None,
        create_sql=(
            f'{_INDEX_SQL_PREFIX} "ix_tournament_list_allowed_ranks_gin" '
            "ON platform.tournament_list_read_models USING gin (allowed_ranks)"
        ),
    ),
    IndexSpec(
        name="ix_tournament_list_name_lower_trgm",
        method="gin",
        keys=("lower(name)",),
        options=(0,),
        opclasses=("gin_trgm_ops",),
        collations=("default",),
        predicate=None,
        create_sql=(
            f'{_INDEX_SQL_PREFIX} "ix_tournament_list_name_lower_trgm" '
            "ON platform.tournament_list_read_models "
            "USING gin (lower(name) gin_trgm_ops)"
        ),
    ),
    IndexSpec(
        name="ix_tournament_list_organizer_name_lower_trgm",
        method="gin",
        keys=("lower(organizer_display_name)",),
        options=(0,),
        opclasses=("gin_trgm_ops",),
        collations=("default",),
        predicate=None,
        create_sql=(
            f'{_INDEX_SQL_PREFIX} "ix_tournament_list_organizer_name_lower_trgm" '
            "ON platform.tournament_list_read_models "
            "USING gin (lower(organizer_display_name) gin_trgm_ops)"
        ),
    ),
)


_COLUMN_SQL = text(
    """
    SELECT ordinal_position, column_name, data_type, udt_name,
           character_maximum_length, is_nullable, column_default,
           is_generated, identity_generation
    FROM information_schema.columns
    WHERE table_schema = 'platform' AND table_name = 'tournament_list_read_models'
    ORDER BY ordinal_position
    """
)
_RELATION_SQL = text(
    """
    SELECT c.oid::bigint AS oid, c.relkind, c.relpersistence,
           c.relispartition
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'platform' AND c.relname = 'tournament_list_read_models'
    """
)
_WRONG_SCHEMA_TABLE_SQL = text(
    """
    SELECT n.nspname AS table_schema, c.relkind
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = 'tournament_list_read_models'
      AND n.nspname <> 'platform'
    """
)
_CONSTRAINT_SQL = text(
    """
    SELECT conname, contype, convalidated, conindid::bigint AS conindid,
           pg_get_constraintdef(oid, false) AS definition
    FROM pg_constraint
    WHERE conrelid = 'platform.tournament_list_read_models'::regclass
    ORDER BY conname
    """
)
_CONSTRAINT_INDEX_SQL = text(
    """
    SELECT indisvalid, indisready, indislive
    FROM pg_index
    WHERE indexrelid = :index_oid
    """
)
_INDEX_SQL = text(
    """
    SELECT c.oid::bigint AS index_oid, c.relkind, c.relpersistence,
           c.relispartition, i.indrelid::bigint AS table_oid,
           i.indisvalid, i.indisready, i.indislive,
           i.indnkeyatts, i.indnatts, i.indkey::int[] AS indkey,
           i.indoption::int[] AS indoption,
           i.indclass::oid[] AS indclass,
           i.indcollation::oid[] AS indcollation,
           i.indisunique, i.indisexclusion, am.amname,
           pg_get_expr(i.indpred, i.indrelid) AS predicate,
           n_table.nspname AS table_schema,
           table_class.relname AS table_name
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_index i ON i.indexrelid = c.oid
    LEFT JOIN pg_am am ON am.oid = c.relam
    LEFT JOIN pg_class table_class ON table_class.oid = i.indrelid
    LEFT JOIN pg_namespace n_table ON n_table.oid = table_class.relnamespace
    WHERE n.nspname = 'platform' AND c.relname = :index_name
    """
)
_WRONG_SCHEMA_INDEX_SQL = text(
    """
    SELECT n.nspname AS index_schema, c.relkind
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = :index_name AND n.nspname <> 'platform'
    """
)
_INDEX_KEYS_SQL = text(
    """
    SELECT x.ordinality::int AS position, x.attnum::int AS attnum,
           i.indoption[x.ordinality - 1]::int AS indoption,
           i.indclass[x.ordinality - 1]::oid AS opclass_oid,
           i.indcollation[x.ordinality - 1]::oid AS collation_oid,
           opc.opcname AS opclass_name, coll.collname AS collation_name,
           a.attname, pg_get_indexdef(i.indexrelid, x.ordinality::int, false)
           AS key_definition
    FROM pg_index i
    CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS x(attnum, ordinality)
    LEFT JOIN pg_attribute a
      ON a.attrelid = i.indrelid AND a.attnum = x.attnum
    LEFT JOIN pg_opclass opc
      ON opc.oid = i.indclass[x.ordinality - 1]
    LEFT JOIN pg_collation coll
      ON coll.oid = i.indcollation[x.ordinality - 1]
    WHERE i.indexrelid = :index_oid
    ORDER BY x.ordinality
    """
)
_VERSION_SQL = text(
    """
    SELECT version_num
    FROM public.alembic_version
    ORDER BY version_num
    """
)
_VERSION_TABLE_EXISTS_SQL = text(
    "SELECT to_regclass('public.alembic_version') IS NOT NULL AS present"
)
_BACKFILL_SQL = text(
    """
    INSERT INTO platform.tournament_list_read_models
    (id, slug, name, description, cover_url, banner_asset_id, visibility, status,
     format_slug, allowed_ranks, max_participants, registration_starts_at,
     registration_closes_at, ready_check_starts_at, ready_check_ends_at,
     captain_selection_starts_at, starts_at, match_format, final_format,
     captain_response_deadline_minutes, teams_count,
     automation_ready_check_started_at, automation_ready_check_closed_at,
     automation_captain_round_started_at, automation_captain_round_finalized_at,
     automation_assignment_generated_at, automation_last_error,
     automation_failure_count, automation_retry_after, organizer_user_id,
     organizer_display_name, organizer_avatar_asset_id, participant_count,
     has_locked_deadlock_roster, bracket_revision, created_at, updated_at)
    SELECT
        t.id,
        t.slug,
        t.name,
        t.description,
        t.cover_url,
        t.banner_asset_id,
        t.visibility,
        t.status,
        t.format_slug,
        t.allowed_ranks,
        t.max_participants,
        t.registration_starts_at,
        t.registration_closes_at,
        t.ready_check_starts_at,
        t.ready_check_ends_at,
        t.captain_selection_starts_at,
        t.starts_at,
        t.match_format,
        t.final_format,
        t.captain_response_deadline_minutes,
        t.teams_count,
        t.automation_ready_check_started_at,
        t.automation_ready_check_closed_at,
        t.automation_captain_round_started_at,
        t.automation_captain_round_finalized_at,
        t.automation_assignment_generated_at,
        t.automation_last_error,
        t.automation_failure_count,
        t.automation_retry_after,
        t.organizer_user_id,
        u.display_name,
        pp.avatar_asset_id,
        (SELECT count(tp.id) FROM platform.tournament_participants tp
         WHERE tp.tournament_id = t.id
           AND tp.status NOT IN ('withdrawn', 'disqualified')),
        EXISTS (SELECT 1 FROM platform.tournament_deadlock_assignment_runs ar
                WHERE ar.tournament_id = t.id AND ar.status = 'locked'),
        t.bracket_revision,
        t.created_at,
        t.updated_at
    FROM platform.tournaments t
    JOIN platform.users u ON u.id = t.organizer_user_id
    LEFT JOIN platform.player_profiles pp ON pp.user_id = t.organizer_user_id
    ON CONFLICT (id) DO UPDATE SET
        slug = EXCLUDED.slug,
        name = EXCLUDED.name,
        description = EXCLUDED.description,
        cover_url = EXCLUDED.cover_url,
        banner_asset_id = EXCLUDED.banner_asset_id,
        visibility = EXCLUDED.visibility,
        status = EXCLUDED.status,
        format_slug = EXCLUDED.format_slug,
        allowed_ranks = EXCLUDED.allowed_ranks,
        max_participants = EXCLUDED.max_participants,
        registration_starts_at = EXCLUDED.registration_starts_at,
        registration_closes_at = EXCLUDED.registration_closes_at,
        ready_check_starts_at = EXCLUDED.ready_check_starts_at,
        ready_check_ends_at = EXCLUDED.ready_check_ends_at,
        captain_selection_starts_at = EXCLUDED.captain_selection_starts_at,
        starts_at = EXCLUDED.starts_at,
        match_format = EXCLUDED.match_format,
        final_format = EXCLUDED.final_format,
        captain_response_deadline_minutes = EXCLUDED.captain_response_deadline_minutes,
        teams_count = EXCLUDED.teams_count,
        automation_ready_check_started_at = EXCLUDED.automation_ready_check_started_at,
        automation_ready_check_closed_at = EXCLUDED.automation_ready_check_closed_at,
        automation_captain_round_started_at = EXCLUDED.automation_captain_round_started_at,
        automation_captain_round_finalized_at = EXCLUDED.automation_captain_round_finalized_at,
        automation_assignment_generated_at = EXCLUDED.automation_assignment_generated_at,
        automation_last_error = EXCLUDED.automation_last_error,
        automation_failure_count = EXCLUDED.automation_failure_count,
        automation_retry_after = EXCLUDED.automation_retry_after,
        organizer_user_id = EXCLUDED.organizer_user_id,
        organizer_display_name = EXCLUDED.organizer_display_name,
        organizer_avatar_asset_id = EXCLUDED.organizer_avatar_asset_id,
        participant_count = EXCLUDED.participant_count,
        has_locked_deadlock_roster = EXCLUDED.has_locked_deadlock_roster,
        bracket_revision = EXCLUDED.bracket_revision,
        created_at = EXCLUDED.created_at,
        updated_at = EXCLUDED.updated_at
    """
)


def _normal_sql(value: Any) -> str:
    """Normalize catalog's harmless formatting differences, not semantics."""

    return re.sub(r"\s+", "", _catalog_text(value).lower())


def _catalog_text(value: Any) -> str:
    """Decode PostgreSQL's one-byte ``char`` catalog fields consistently."""

    if isinstance(value, bytes):
        return value.decode("ascii")
    return str(value or "")


def _normal_expression(value: Any) -> str:
    normalized = _normal_sql(value)
    normalized = normalized.replace("::text", "")
    normalized = normalized.replace("::character varying", "")
    while "((" in normalized or "))" in normalized:
        normalized = normalized.replace("((", "(").replace("))", ")")
    return normalized


def _normal_predicate(value: Any) -> str:
    normalized = _normal_expression(value)
    return normalized.replace("(", "").replace(")", "")


def _rows_sync(bind: Connection, statement: Any, parameters: dict[str, Any] | None = None) -> list[Any]:
    return list(bind.execute(statement, parameters or {}).mappings())


async def _rows_async(
    bind: AsyncConnection,
    statement: Any,
    parameters: dict[str, Any] | None = None,
) -> list[Any]:
    result = await bind.execute(statement, parameters or {})
    return list(result.mappings())


def _relation_sync(bind: Connection) -> Any | None:
    return bind.execute(_RELATION_SQL).mappings().first()


async def _relation_async(bind: AsyncConnection) -> Any | None:
    return (await bind.execute(_RELATION_SQL)).mappings().first()


def _validate_table_rows(relation: Any | None, columns: list[Any]) -> int:
    if relation is None:
        raise RecoveryError(
            "platform.tournament_list_read_models is missing; cannot repair a stamped 0051"
        )
    if (
        _catalog_text(relation["relkind"]) != "r"
        or _catalog_text(relation["relpersistence"]) != "p"
        or relation["relispartition"]
    ):
        raise RecoveryError("tournament_list_read_models is not the expected permanent table")

    actual_columns = [
        (
            row["column_name"],
            row["data_type"],
            row["udt_name"],
            row["character_maximum_length"],
            row["is_nullable"],
            _normal_sql(row["column_default"]) if row["column_default"] is not None else None,
        )
        for row in columns
    ]
    expected_columns = [
        (
            name,
            data_type,
            udt_name,
            length,
            nullable,
            _normal_sql(default) if default is not None else None,
        )
        for name, data_type, udt_name, length, nullable, default in EXPECTED_COLUMNS
    ]
    if actual_columns != expected_columns:
        raise RecoveryError(
            "tournament_list_read_models has an incompatible column definition"
        )
    if any(
        row["is_generated"] != "NEVER" or row["identity_generation"] is not None
        for row in columns
    ):
        raise RecoveryError("tournament_list_read_models contains generated or identity columns")
    return int(relation["oid"])


def _validate_constraints_sync(bind: Connection) -> None:
    rows = _rows_sync(bind, _CONSTRAINT_SQL)
    actual_names = {row["conname"] for row in rows}
    if actual_names != set(EXPECTED_CONSTRAINTS):
        raise RecoveryError(
            "tournament_list_read_models constraints do not match the 0051 definition"
        )
    for row in rows:
        expected_type, expected_definition = EXPECTED_CONSTRAINTS[row["conname"]]
        if (
            _catalog_text(row["contype"]) != expected_type
            or not row["convalidated"]
            or _normal_sql(row["definition"]) != _normal_sql(expected_definition)
        ):
            raise RecoveryError(
                f"incompatible tournament_list_read_models constraint: {row['conname']}"
            )
        if _catalog_text(row["contype"]) in {"p", "u"}:
            index_rows = _rows_sync(
                bind,
                _CONSTRAINT_INDEX_SQL,
                {"index_oid": row["conindid"]},
            )
            if len(index_rows) != 1 or any(
                not index_rows[0][field] for field in ("indisvalid", "indisready", "indislive")
            ):
                raise RecoveryError(
                    f"constraint index is invalid: {row['conname']}"
                )


async def _validate_constraints_async(bind: AsyncConnection) -> None:
    rows = await _rows_async(bind, _CONSTRAINT_SQL)
    actual_names = {row["conname"] for row in rows}
    if actual_names != set(EXPECTED_CONSTRAINTS):
        raise RecoveryError(
            "tournament_list_read_models constraints do not match the 0051 definition"
        )
    for row in rows:
        expected_type, expected_definition = EXPECTED_CONSTRAINTS[row["conname"]]
        if (
            _catalog_text(row["contype"]) != expected_type
            or not row["convalidated"]
            or _normal_sql(row["definition"]) != _normal_sql(expected_definition)
        ):
            raise RecoveryError(
                f"incompatible tournament_list_read_models constraint: {row['conname']}"
            )
        if _catalog_text(row["contype"]) in {"p", "u"}:
            index_rows = await _rows_async(
                bind,
                _CONSTRAINT_INDEX_SQL,
                {"index_oid": row["conindid"]},
            )
            if len(index_rows) != 1 or any(
                not index_rows[0][field] for field in ("indisvalid", "indisready", "indislive")
            ):
                raise RecoveryError(
                    f"constraint index is invalid: {row['conname']}"
                )


def validate_projection_sync(bind: Connection) -> int:
    """Validate and return the projection table OID for an Alembic migration."""

    relation = _relation_sync(bind)
    columns = _rows_sync(bind, _COLUMN_SQL)
    table_oid = _validate_table_rows(relation, columns)
    _validate_constraints_sync(bind)
    _validate_preexisting_objects_sync(bind, table_oid)
    return table_oid


async def validate_projection_async(bind: AsyncConnection) -> int:
    """Async counterpart used by the command-line recovery path."""

    relation = await _relation_async(bind)
    columns = await _rows_async(bind, _COLUMN_SQL)
    table_oid = _validate_table_rows(relation, columns)
    await _validate_constraints_async(bind)
    await _validate_preexisting_objects_async(bind, table_oid)
    return table_oid


def backfill_projection_sync(bind: Connection) -> None:
    """Idempotently refresh all projection rows from authoritative tables."""

    bind.execute(_BACKFILL_SQL)


async def backfill_projection_async(bind: AsyncConnection) -> None:
    """Async counterpart used by the command-line recovery path."""

    await bind.execute(_BACKFILL_SQL)


def _index_info_sync(bind: Connection, spec: IndexSpec) -> Any | None:
    return bind.execute(_INDEX_SQL, {"index_name": spec.name}).mappings().first()


async def _index_info_async(bind: AsyncConnection, spec: IndexSpec) -> Any | None:
    return (
        await bind.execute(_INDEX_SQL, {"index_name": spec.name})
    ).mappings().first()


def _validate_index_owners_sync(
    bind: Connection,
    table_oid: int | None,
) -> None:
    """Reject a named object that cannot be the projection's own index."""

    for spec in INDEX_SPECS:
        wrong_schema = bind.execute(
            _WRONG_SCHEMA_INDEX_SQL,
            {"index_name": spec.name},
        ).mappings().first()
        if wrong_schema is not None:
            raise RecoveryError(
                f"object {wrong_schema['index_schema']}.{spec.name} is outside the platform schema"
            )
        info = _index_info_sync(bind, spec)
        if info is None:
            continue
        if info["index_oid"] is None or _catalog_text(info["relkind"]) != "i":
            raise RecoveryError(f"object platform.{spec.name} is not an index")
        if table_oid is None or int(info["table_oid"] or 0) != table_oid:
            raise RecoveryError(
                f"index platform.{spec.name} belongs to a different table"
            )


async def _validate_index_owners_async(
    bind: AsyncConnection,
    table_oid: int | None,
) -> None:
    """Reject a named object that cannot be the projection's own index."""

    for spec in INDEX_SPECS:
        wrong_schema = (
            await bind.execute(_WRONG_SCHEMA_INDEX_SQL, {"index_name": spec.name})
        ).mappings().first()
        if wrong_schema is not None:
            raise RecoveryError(
                f"object {wrong_schema['index_schema']}.{spec.name} is outside the platform schema"
            )
        info = await _index_info_async(bind, spec)
        if info is None:
            continue
        if info["index_oid"] is None or _catalog_text(info["relkind"]) != "i":
            raise RecoveryError(f"object platform.{spec.name} is not an index")
        if table_oid is None or int(info["table_oid"] or 0) != table_oid:
            raise RecoveryError(
                f"index platform.{spec.name} belongs to a different table"
            )


async def _validate_preexisting_objects_async(
    bind: AsyncConnection,
    table_oid: int | None,
) -> None:
    wrong_schema_table = (await bind.execute(_WRONG_SCHEMA_TABLE_SQL)).mappings().first()
    if wrong_schema_table is not None:
        raise RecoveryError(
            "tournament_list_read_models exists outside the platform schema"
        )
    await _validate_index_owners_async(bind, table_oid)


def _validate_preexisting_objects_sync(
    bind: Connection,
    table_oid: int | None,
) -> None:
    wrong_schema_table = bind.execute(_WRONG_SCHEMA_TABLE_SQL).mappings().first()
    if wrong_schema_table is not None:
        raise RecoveryError(
            "tournament_list_read_models exists outside the platform schema"
        )
    _validate_index_owners_sync(bind, table_oid)


def _validate_index_shape(
    bind: Connection,
    spec: IndexSpec,
    info: Any,
    table_oid: int,
) -> bool:
    """Return true for a valid exact index, false for an invalid partial one."""

    if info["index_oid"] is None or _catalog_text(info["relkind"]) != "i":
        raise RecoveryError(f"object platform.{spec.name} is not an index")
    if int(info["table_oid"] or 0) != table_oid:
        raise RecoveryError(
            f"index platform.{spec.name} belongs to a different table"
        )
    valid = all(info[field] for field in ("indisvalid", "indisready", "indislive"))
    if (
        _catalog_text(info["relpersistence"]) != "p"
        or info["relispartition"]
        or info["amname"] != spec.method
        or info["indisunique"]
        or info["indisexclusion"]
        or int(info["indnkeyatts"] or 0) != len(spec.keys)
        or int(info["indnatts"] or 0) != len(spec.keys)
        or _normal_predicate(info["predicate"]) != _normal_predicate(spec.predicate)
    ):
        raise RecoveryError(f"index platform.{spec.name} has an incompatible definition")

    key_rows = _rows_sync(bind, _INDEX_KEYS_SQL, {"index_oid": info["index_oid"]})
    if len(key_rows) != len(spec.keys):
        raise RecoveryError(f"index platform.{spec.name} has an incompatible key list")
    for index, (key, row) in enumerate(zip(spec.keys, key_rows, strict=True)):
        expected_expression = key.startswith("lower(")
        actual_key = (
            _normal_expression(row["key_definition"])
            if expected_expression
            else row["attname"]
        )
        expected_key = _normal_expression(key) if expected_expression else key
        if (
            actual_key != expected_key
            or int(row["indoption"] or 0) != spec.options[index]
            or row["opclass_name"] != spec.opclasses[index]
            or (row["collation_name"] or "none") != spec.collations[index]
        ):
            raise RecoveryError(f"index platform.{spec.name} has an incompatible definition")
    # Only an exact index may be repaired when PostgreSQL left it invalid or
    # unfinished.  A malformed index is rejected even if its catalog validity
    # flags are false; otherwise the historical IF NOT EXISTS behavior could
    # silently bless a wrong object.
    return valid


async def _validate_index_shape_async(
    bind: AsyncConnection,
    spec: IndexSpec,
    info: Any,
    table_oid: int,
) -> bool:
    if info["index_oid"] is None or _catalog_text(info["relkind"]) != "i":
        raise RecoveryError(f"object platform.{spec.name} is not an index")
    if int(info["table_oid"] or 0) != table_oid:
        raise RecoveryError(
            f"index platform.{spec.name} belongs to a different table"
        )
    valid = all(info[field] for field in ("indisvalid", "indisready", "indislive"))
    if (
        _catalog_text(info["relpersistence"]) != "p"
        or info["relispartition"]
        or info["amname"] != spec.method
        or info["indisunique"]
        or info["indisexclusion"]
        or int(info["indnkeyatts"] or 0) != len(spec.keys)
        or int(info["indnatts"] or 0) != len(spec.keys)
        or _normal_predicate(info["predicate"]) != _normal_predicate(spec.predicate)
    ):
        raise RecoveryError(f"index platform.{spec.name} has an incompatible definition")
    key_rows = await _rows_async(
        bind,
        _INDEX_KEYS_SQL,
        {"index_oid": info["index_oid"]},
    )
    if len(key_rows) != len(spec.keys):
        raise RecoveryError(f"index platform.{spec.name} has an incompatible key list")
    for index, (key, row) in enumerate(zip(spec.keys, key_rows, strict=True)):
        expected_expression = key.startswith("lower(")
        actual_key = (
            _normal_expression(row["key_definition"])
            if expected_expression
            else row["attname"]
        )
        expected_key = _normal_expression(key) if expected_expression else key
        if (
            actual_key != expected_key
            or int(row["indoption"] or 0) != spec.options[index]
            or row["opclass_name"] != spec.opclasses[index]
            or (row["collation_name"] or "none") != spec.collations[index]
        ):
            raise RecoveryError(f"index platform.{spec.name} has an incompatible definition")
    # Only an exact index may be repaired when PostgreSQL left it invalid or
    # unfinished.  A malformed index is rejected even if its catalog validity
    # flags are false; otherwise the historical IF NOT EXISTS behavior could
    # silently bless a wrong object.
    return valid


def _drop_sql(spec: IndexSpec) -> Any:
    return text(f'DROP INDEX CONCURRENTLY platform."{spec.name}"')


async def repair_indexes_async(
    bind: AsyncConnection,
    table_oid: int,
) -> None:
    """Repair indexes on an autocommit async connection."""

    # The historical migration installed this opclass dependency in the same
    # autocommit block immediately before the index loop.  Repeat that safe,
    # idempotent prerequisite so recovery also handles a failure at that
    # boundary rather than assuming pg_trgm was committed.
    await bind.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    for spec in INDEX_SPECS:
        info = await _index_info_async(bind, spec)
        if info is not None:
            if await _validate_index_shape_async(bind, spec, info, table_oid):
                continue
            await bind.execute(_drop_sql(spec))
        await bind.execute(text(spec.create_sql))
        created = await _index_info_async(bind, spec)
        if created is None or not await _validate_index_shape_async(
            bind, spec, created, table_oid
        ):
            raise RecoveryError(
                f"index platform.{spec.name} was not valid after concurrent creation"
            )
    await validate_indexes_async(bind, table_oid)


async def validate_indexes_async(bind: AsyncConnection, table_oid: int) -> None:
    """Require every read-model index to be present, valid, and exact."""

    for spec in INDEX_SPECS:
        info = await _index_info_async(bind, spec)
        if info is None or not await _validate_index_shape_async(
            bind, spec, info, table_oid
        ):
            raise RecoveryError(
                f"index platform.{spec.name} is missing or invalid after repair"
            )


async def _current_version(bind: AsyncConnection) -> str | None:
    table_exists = (await bind.execute(_VERSION_TABLE_EXISTS_SQL)).scalar_one()
    if not table_exists:
        return None
    rows = await _rows_async(bind, _VERSION_SQL)
    if len(rows) != 1:
        raise RecoveryError("public.alembic_version must contain exactly one linear revision")
    return str(rows[0]["version_num"])


async def _table_exists(bind: AsyncConnection) -> bool:
    return (await _relation_async(bind)) is not None


async def _recover_partial_0051_locked(engine: AsyncEngine) -> bool:
    async with engine.connect() as bind:
        async with bind.begin():
            current = await _current_version(bind)
            has_table = await _table_exists(bind)
            if current != PARTIAL_REVISION:
                # Future revisions may add unrelated changes after this
                # repair.  Only an object that predates 0051 can collide
                # with its CREATE TABLE; later revisions are left to
                # Alembic (0053 performs the forward validation).
                if has_table and (current is None or current < PARTIAL_REVISION):
                    raise RecoveryError(
                        f"unexpected projection table while Alembic is at {current}"
                    )
                if not has_table and (current is None or current <= PARTIAL_REVISION):
                    # The historical 0051 uses IF NOT EXISTS for indexes.
                    # Refuse a same-named object before it can be silently
                    # accepted after the table is created.
                    await _validate_preexisting_objects_async(bind, None)
                return False
            if not has_table:
                # This is still the 0050 state.  The historical 0051
                # would create the table and then silently accept any
                # same-named index through IF NOT EXISTS, so inspect
                # named objects before allowing Alembic to proceed.
                await _validate_preexisting_objects_async(bind, None)
                return False
            table_oid = await validate_projection_async(bind)
            await backfill_projection_async(bind)

        async with engine.connect() as autocommit_bind:
            autocommit_bind = await autocommit_bind.execution_options(
                isolation_level="AUTOCOMMIT"
            )
            await repair_indexes_async(autocommit_bind, table_oid)

        async with bind.begin():
            current = await _current_version(bind)
            if current != PARTIAL_REVISION:
                raise RecoveryError(
                    "Alembic revision changed while repairing tournament catalog"
                )
            repaired_table_oid = await validate_projection_async(bind)
            if repaired_table_oid != table_oid:
                raise RecoveryError(
                    "tournament catalog table changed while repairing indexes"
                )
            await validate_indexes_async(bind, table_oid)
            update = await bind.execute(
                text(
                    "UPDATE public.alembic_version "
                    "SET version_num = :applied "
                    "WHERE version_num = :partial"
                ),
                {"applied": APPLIED_REVISION, "partial": PARTIAL_REVISION},
            )
            if update.rowcount != 1:
                raise RecoveryError("failed to stamp repaired tournament catalog migration")
        return True


async def recover_partial_0051(engine: AsyncEngine) -> bool:
    """Repair and stamp a committed 0051 partial state.

    Returns ``True`` only when the helper repaired a table and advanced
    ``alembic_version`` from 0050 to 0051.  A fresh database, or one already
    beyond 0051, is a no-op.  A table left behind while Alembic is still before
    0050 is rejected rather than guessed at.
    """

    # A session-level advisory lock serializes recovery attempts while the
    # index DDL runs across independent AUTOCOMMIT connections.  Explicitly
    # release it before returning the pooled connection: ROLLBACK does not
    # release session-level advisory locks.
    async with engine.connect() as lock_bind:
        lock_bind = await lock_bind.execution_options(isolation_level="AUTOCOMMIT")
        await lock_bind.execute(
            text("SELECT pg_advisory_lock(:key)"),
            {"key": ADVISORY_LOCK_KEY},
        )
        try:
            return await _recover_partial_0051_locked(engine)
        finally:
            await lock_bind.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": ADVISORY_LOCK_KEY},
            )


async def _main_async() -> None:
    settings = get_settings()
    validate_platform_settings(settings)
    engine = create_async_engine(settings.platform_database_url, pool_pre_ping=True)
    try:
        repaired = await recover_partial_0051(engine)
    finally:
        await engine.dispose()
    print("Tournament catalog migration recovery: " + ("repaired" if repaired else "no-op"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair a committed, unstamped tournament catalog migration state."
    )
    parser.add_argument(
        "--recover-partial",
        action="store_true",
        help="repair only the 0050 -> 0051 partial state; otherwise refuse",
    )
    return parser.parse_args()


def main() -> None:
    if not _parse_args().recover_partial:
        raise SystemExit("Refusing recovery without --recover-partial")
    try:
        asyncio.run(_main_async())
    except RecoveryError as exc:
        raise SystemExit(f"Tournament catalog migration recovery refused: {exc}") from exc


if __name__ == "__main__":
    main()
