"""Add a partial covering index for Ready Vote session authentication.

Revision ID: 20260830_0048
Revises: 20260829_0047
Create Date: 2026-08-30

The unique token-digest index remains the session identity and correctness
guard. This separate partial index is intentionally not a duplicate: it lets
PostgreSQL satisfy the Ready Vote session-side projection from the index when
``invalidated_at IS NULL`` and ``user_id``/``expires_at`` are needed. The
``users.status`` and email-verification predicates still require the existing
primary-key lookup on ``users``; fusing them into this index is not possible.
The auth and transactional preflight projections therefore remain separate.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260830_0048"
down_revision = "20260829_0047"
branch_labels = None
depends_on = None


INDEX_NAME = "ix_sessions_ready_vote_auth"
EXPECTED_INDEXDEF = (
    "CREATE INDEX ix_sessions_ready_vote_auth ON platform.sessions USING btree "
    "(token_digest) INCLUDE (user_id, expires_at) WHERE (invalidated_at IS NULL)"
)
EXPECTED_PREDICATE = "(invalidated_at IS NULL)"
EXPECTED_INCLUDE_COLUMNS = ("user_id", "expires_at")

_INDEX_CATALOG_QUERY = sa.text(
    """
    SELECT pg_index.indisvalid AS is_valid,
           pg_index.indisready AS is_ready,
           pg_get_indexdef(pg_class.oid) AS indexdef,
           pg_get_expr(pg_index.indpred, pg_index.indrelid) AS predicate,
           COALESCE(
               ARRAY(
                   SELECT attribute.attname
                   FROM unnest(pg_index.indkey) WITH ORDINALITY AS key(attnum, position)
                   JOIN pg_attribute AS attribute
                     ON attribute.attrelid = pg_index.indrelid
                    AND attribute.attnum = key.attnum
                   WHERE key.position > pg_index.indnkeyatts
                   ORDER BY key.position
               ),
               ARRAY[]::text[]
           ) AS include_columns
    FROM pg_class
    JOIN pg_namespace
      ON pg_namespace.oid = pg_class.relnamespace
    JOIN pg_index
      ON pg_index.indexrelid = pg_class.oid
    WHERE pg_namespace.nspname = 'platform'
      AND pg_class.relname = :index_name
    """
)


class IndexCatalogState:
    __slots__ = ("is_valid", "is_ready", "indexdef", "predicate", "include_columns")

    def __init__(
        self,
        *,
        is_valid: bool,
        is_ready: bool,
        indexdef: str,
        predicate: str | None,
        include_columns: tuple[str, ...],
    ) -> None:
        self.is_valid = is_valid
        self.is_ready = is_ready
        self.indexdef = indexdef
        self.predicate = predicate
        self.include_columns = include_columns


def _normalize_catalog_sql(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.split()).lower()


def _index_matches_expected(state: IndexCatalogState) -> bool:
    return (
        _normalize_catalog_sql(state.indexdef)
        == _normalize_catalog_sql(EXPECTED_INDEXDEF)
        and _normalize_catalog_sql(state.predicate)
        == _normalize_catalog_sql(EXPECTED_PREDICATE)
        and state.include_columns == EXPECTED_INCLUDE_COLUMNS
    )


def _index_action(state: IndexCatalogState | None) -> str:
    if state is None:
        return "create"
    if _index_matches_expected(state):
        return "keep" if state.is_valid and state.is_ready else "recreate"
    if not state.is_valid:
        return "recreate"
    raise RuntimeError(
        f"Index {INDEX_NAME!r} exists with a valid but unexpected catalog definition; "
        "refusing to accept it."
    )


def _read_index_catalog() -> IndexCatalogState | None:
    row = (
        op.get_bind()
        .execute(_INDEX_CATALOG_QUERY, {"index_name": INDEX_NAME})
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return IndexCatalogState(
        is_valid=bool(row["is_valid"]),
        is_ready=bool(row["is_ready"]),
        indexdef=str(row["indexdef"]),
        predicate=row["predicate"],
        include_columns=tuple(row["include_columns"] or ()),
    )


def _drop_index_concurrently() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS platform.{INDEX_NAME}")


def _drop_invalid_index_if_present() -> None:
    """Remove only a failed/interrupted build so a retry can recreate it."""

    state = _read_index_catalog()
    if state is not None and _index_action(state) == "recreate":
        _drop_index_concurrently()


def _create_index_concurrently() -> None:
    try:
        with op.get_context().autocommit_block():
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                f"{INDEX_NAME} ON platform.sessions (token_digest) "
                "INCLUDE (user_id, expires_at) "
                "WHERE invalidated_at IS NULL"
            )
    except Exception:
        # CREATE INDEX CONCURRENTLY can leave an invalid catalog entry after a
        # failed build. Remove that entry before propagating the failure so a
        # later Alembic retry starts from a clean state. A valid conflicting
        # index is never removed: _index_action raises for it.
        _drop_invalid_index_if_present()
        raise


def upgrade() -> None:
    action = _index_action(_read_index_catalog())
    if action == "keep":
        return
    if action == "recreate":
        # Only an invalid/interrupted build is safe to replace. A valid index
        # with a conflicting definition is rejected by _index_action above.
        _drop_index_concurrently()

    _create_index_concurrently()
    final_state = _read_index_catalog()
    final_action = _index_action(final_state)
    if final_action == "keep":
        return
    if final_action == "recreate":
        _drop_invalid_index_if_present()
    raise RuntimeError(
        f"Index {INDEX_NAME!r} did not become valid and ready after creation; "
        "refusing to accept it."
    )


def downgrade() -> None:
    state = _read_index_catalog()
    if state is not None:
        _index_action(state)
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            f"platform.{INDEX_NAME}"
        )
