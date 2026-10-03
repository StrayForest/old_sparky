#!/usr/bin/env python3
"""Read-only candidate Alembic graph and database revision guard."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import re
import sys


_REVISION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def validate_forward_revision_state(
    current_revisions: Sequence[str],
    revision_parents: Mapping[str, Sequence[str]],
    *,
    allow_empty: bool = False,
) -> dict[str, object]:
    """Validate one database revision is on the candidate's sole forward path."""

    if type(allow_empty) is not bool:
        raise ValueError("empty-database allowance must be boolean")
    if not isinstance(revision_parents, Mapping) or not revision_parents:
        raise ValueError("candidate revision graph is empty")
    if len(revision_parents) > 4096:
        raise ValueError("candidate revision graph is too large")

    parents_by_revision: dict[str, tuple[str, ...]] = {}
    for revision, parents in revision_parents.items():
        if not isinstance(revision, str) or _REVISION_RE.fullmatch(revision) is None:
            raise ValueError("candidate revision identifier is malformed")
        if isinstance(parents, (str, bytes)) or not isinstance(parents, Sequence):
            raise ValueError("candidate revision parents are malformed")
        normalized = tuple(parents)
        if len(normalized) > 8:
            raise ValueError("candidate revision has too many parents")
        if any(
            not isinstance(parent, str) or _REVISION_RE.fullmatch(parent) is None
            for parent in normalized
        ) or len(set(normalized)) != len(normalized):
            raise ValueError("candidate revision parents are malformed")
        parents_by_revision[revision] = normalized

    for parents in parents_by_revision.values():
        if any(parent not in parents_by_revision for parent in parents):
            raise ValueError("candidate revision graph has an unknown parent")

    children = {parent for parents in parents_by_revision.values() for parent in parents}
    heads = sorted(set(parents_by_revision) - children)
    if len(heads) != 1:
        raise ValueError("candidate revision graph must have one head")
    head = heads[0]

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(revision: str) -> None:
        if revision in visiting:
            raise ValueError("candidate revision graph contains a cycle")
        if revision in visited:
            return
        visiting.add(revision)
        for parent in parents_by_revision[revision]:
            visit(parent)
        visiting.remove(revision)
        visited.add(revision)

    for revision in parents_by_revision:
        visit(revision)

    if isinstance(current_revisions, (str, bytes)) or not isinstance(
        current_revisions, Sequence
    ):
        raise ValueError("database revision rows are malformed")
    current_rows = tuple(current_revisions)
    if len(current_rows) > 2:
        raise ValueError("database revision row set is too large")
    if not current_rows:
        if not allow_empty:
            raise ValueError("database revision is missing")
        return {
            "current_revision": None,
            "head_revision": head,
            "allow_empty": True,
        }
    if len(current_rows) != 1:
        raise ValueError("database must have one current revision")
    current = current_rows[0]
    if not isinstance(current, str) or _REVISION_RE.fullmatch(current) is None:
        raise ValueError("database revision is malformed")
    if current not in parents_by_revision:
        raise ValueError("database revision is unknown to candidate")

    reachable: set[str] = set()
    pending = [head]
    while pending:
        revision = pending.pop()
        if revision in reachable:
            continue
        reachable.add(revision)
        pending.extend(parents_by_revision[revision])
    if current not in reachable:
        raise ValueError("database revision is not an ancestor of candidate head")

    return {
        "current_revision": current,
        "head_revision": head,
        "allow_empty": False,
    }


def _candidate_revision_parents(candidate_dir: Path) -> dict[str, tuple[str, ...]]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    if candidate_dir.is_symlink():
        raise ValueError("candidate release directory is a symlink")
    candidate = candidate_dir.resolve(strict=True)
    if not candidate.is_dir():
        raise ValueError("candidate release directory is unavailable")
    config_path = candidate / "alembic.ini"
    if not config_path.is_file() or config_path.is_symlink():
        raise ValueError("candidate Alembic configuration is unavailable")
    config = Config(str(config_path))
    scripts = ScriptDirectory.from_config(config)
    script_heads = scripts.get_heads()
    if len(script_heads) != 1:
        raise ValueError("candidate Alembic graph must have one head")
    script_path = Path(scripts.dir)
    if not script_path.is_absolute():
        script_path = candidate / script_path
    if script_path.is_symlink():
        raise ValueError("candidate Alembic scripts are a symlink")
    script_dir = script_path.resolve(strict=True)
    if not script_dir.is_relative_to(candidate) or script_dir.is_symlink():
        raise ValueError("candidate Alembic scripts escape the release")

    result: dict[str, tuple[str, ...]] = {}
    for revision in scripts.walk_revisions():
        down_revision = revision.down_revision
        if down_revision is None:
            parents: tuple[str, ...] = ()
        elif isinstance(down_revision, str):
            parents = (down_revision,)
        elif isinstance(down_revision, tuple) and all(
            isinstance(value, str) for value in down_revision
        ):
            parents = down_revision
        else:
            raise ValueError("candidate Alembic parent metadata is malformed")
        dependencies = revision.dependencies
        if dependencies is None:
            dependency_revisions: tuple[str, ...] = ()
        elif isinstance(dependencies, str):
            dependency_revisions = (dependencies,)
        elif isinstance(dependencies, tuple) and all(
            isinstance(value, str) for value in dependencies
        ):
            dependency_revisions = dependencies
        else:
            raise ValueError("candidate Alembic dependency metadata is malformed")
        result[revision.revision] = tuple(
            dict.fromkeys((*parents, *dependency_revisions))
        )
    inferred_heads = set(result) - {
        parent for revision_parents in result.values() for parent in revision_parents
    }
    if inferred_heads != set(script_heads):
        raise ValueError("candidate Alembic head metadata is inconsistent")
    return result


async def _read_current_revisions(*, allow_empty: bool) -> tuple[str, ...]:
    from sqlalchemy.engine import make_url
    import asyncpg

    raw_url = os.environ.get("PLATFORM_DATABASE_URL", "")
    if not raw_url:
        raise ValueError("database URL is unavailable")
    url = make_url(raw_url)
    if (
        url.drivername not in {"postgresql+asyncpg", "postgresql"}
        or url.database != "platformdb"
    ):
        raise ValueError("database URL driver is unsupported")
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    connection = await asyncpg.connect(
        dsn=dsn,
        timeout=10,
        server_settings={
            "application_name": "oldsparky-release-migration-guard",
            "statement_timeout": "10000",
            "lock_timeout": "10000",
            "default_transaction_read_only": "on",
        },
    )
    try:
        async with connection.transaction(readonly=True):
            identity = await connection.fetchrow(
                "SELECT current_database() AS database_name, "
                "current_schema() AS schema_name, "
                "to_regclass('alembic_version')::oid AS resolved_oid"
            )
            if (
                identity is None
                or identity["database_name"] != "platformdb"
                or identity["schema_name"] not in {"platform", "public"}
            ):
                raise ValueError("database identity or schema is unsupported")
            registries = await connection.fetch(
                "SELECT c.oid, n.nspname AS schema_name, "
                "c.relkind::text AS relkind "
                "FROM pg_catalog.pg_class AS c "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE c.relname = 'alembic_version' "
                "ORDER BY n.nspname"
            )
            if not registries:
                if allow_empty:
                    return ()
                raise ValueError("database revision registry is missing")
            if len(registries) != 1:
                raise ValueError("database revision registry is ambiguous")
            registry = registries[0]
            if (
                registry["relkind"] != "r"
                or registry["schema_name"] not in {"platform", "public"}
                or identity["resolved_oid"] != registry["oid"]
            ):
                raise ValueError("database revision registry is not canonical")
            rows = await connection.fetch(
                'SELECT version_num FROM "'
                + registry["schema_name"]
                + '".alembic_version '
                "ORDER BY version_num LIMIT 2"
            )
        return tuple(row["version_num"] for row in rows)
    finally:
        await connection.close()


async def _run(candidate_dir: Path, allow_empty: bool) -> None:
    parents = _candidate_revision_parents(candidate_dir)
    current = await _read_current_revisions(allow_empty=allow_empty)
    validate_forward_revision_state(current, parents, allow_empty=allow_empty)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-dir", required=True, type=Path)
    parser.add_argument("--allow-empty-database", action="store_true")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args.candidate_dir, args.allow_empty_database))
    except Exception:
        print("Candidate migration path is not a safe forward upgrade.", file=sys.stderr)
        return 1
    print("RELEASE_MIGRATION_GUARD status=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
