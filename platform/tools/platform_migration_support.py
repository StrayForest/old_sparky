"""Small, dependency-lazy contracts shared by migration verification paths.

The migration gate and the backend preflight must agree on the repository's
Alembic graph without copying a revision ID into either caller.  This module
keeps the source-head and database-head checks in one place while importing
Alembic/SQLAlchemy only when a resource-bearing caller actually needs them.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import stat
import subprocess
import time
from typing import Sequence
from urllib.parse import urlsplit


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = PLATFORM_ROOT / "alembic.ini"
ALEMBIC_SCRIPT_LOCATION = PLATFORM_ROOT / "alembic"
MIGRATION_SCHEMA = "platform"
DISPOSABLE_DATABASE_NAME = "platformdb_test"
MIGRATION_SUBPROCESS_TIMEOUT_SECONDS = 180.0
MIGRATION_DIAGNOSTIC_ENV = "PLATFORM_MIGRATION_DIAGNOSTICS_FILE"
_MIGRATION_DIAGNOSTIC_MAX_BYTES = 64 * 1024
_MIGRATION_DIAGNOSTIC_STAGES = frozenset(
    {
        "scenario-target-validated",
        "schema-reset-started",
        "schema-reset-completed",
        "alembic-upgrade-started",
        "alembic-upgrade-finished",
        "alembic-upgrade-timeout",
        "alembic-upgrade-error",
        "alembic-downgrade-started",
        "alembic-downgrade-finished",
        "alembic-downgrade-timeout",
        "alembic-downgrade-error",
        "alembic-current-started",
        "alembic-current-finished",
        "alembic-current-timeout",
        "alembic-current-error",
        "alembic-connection-started",
        "alembic-connection-opened",
        "alembic-connection-failed",
        "alembic-migrations-started",
        "alembic-migrations-completed",
        "alembic-migrations-error",
        "alembic-connection-closed",
        "migration-completed",
    }
)


def record_migration_progress(stage: str, *, returncode: int | None = None) -> None:
    """Append one fixed, bounded stage record to the verifier-owned private file."""

    if stage not in _MIGRATION_DIAGNOSTIC_STAGES:
        return
    raw_path = os.environ.get(MIGRATION_DIAGNOSTIC_ENV, "")
    if not raw_path or len(raw_path) > 4096:
        return
    if returncode is not None and not 0 <= returncode <= 255:
        return
    path = Path(raw_path)
    if not path.is_absolute():
        return
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = -1
    try:
        parent = path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            return
        descriptor = os.open(path, flags)
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.geteuid()
            or current.st_nlink != 1
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_size >= _MIGRATION_DIAGNOSTIC_MAX_BYTES
        ):
            return
        suffix = "" if returncode is None else f" returncode={returncode}"
        monotonic_ns = time.monotonic_ns()
        record = f"stage={stage} monotonic_ns={monotonic_ns}{suffix}\n".encode("ascii")
        if len(record) <= 160 and current.st_size + len(record) <= _MIGRATION_DIAGNOSTIC_MAX_BYTES:
            os.write(descriptor, record)
    except (OSError, ValueError):
        # Diagnostics must never change the migration's resource or failure behavior.
        return
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


class MigrationContractError(RuntimeError):
    """Raised when a migration graph, target or database state is unsafe."""


class MigrationHeadInvariantError(MigrationContractError):
    """Raised when source and database heads are not one matching revision."""


class MigrationCommandError(MigrationContractError):
    """Raised when a migration subprocess has an unexpected exit status."""

    def __init__(
        self,
        *,
        label: str,
        command: Sequence[str],
        returncode: int,
        output: str,
    ) -> None:
        self.label = label
        self.command = tuple(command)
        self.returncode = returncode
        self.output = output
        super().__init__(
            f"{label} returned {returncode}; output:\n{output[-4000:]}"
        )


class MigrationCommandTimeout(MigrationContractError):
    """Raised when an Alembic/recovery subprocess exceeds its hard deadline."""

    def __init__(
        self,
        *,
        label: str,
        command: Sequence[str],
        timeout_seconds: float = MIGRATION_SUBPROCESS_TIMEOUT_SECONDS,
        output: str = "",
    ) -> None:
        self.label = label
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds
        self.output = output
        super().__init__(
            f"{label} exceeded {timeout_seconds:g}s; output:\n{output[-4000:]}"
        )


@dataclass(frozen=True, slots=True)
class MigrationHeadState:
    """The source graph and the two database representations of its head."""

    source_heads: tuple[str, ...]
    database_heads: tuple[str, ...]
    version_rows: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DisposableMigrationTarget:
    """A validated target that is safe for destructive migration fixtures."""

    url: str
    database_name: str
    schema: str


def validate_disposable_migration_target(
    database_url: object,
    *,
    environment: object,
    schema: object,
) -> DisposableMigrationTarget:
    """Validate the only database/schema boundary the migration gate may mutate.

    This check is intentionally independent of application settings and client
    libraries.  A host name, production database name, alternate schema,
    query-string override, or non-test environment is rejected before a
    connection is constructed.  The URL may contain the CI test credentials;
    its literal loopback host and exact database path are the safety boundary.
    """

    if environment != "test":
        raise MigrationContractError(
            "migration target requires PLATFORM_ENVIRONMENT=test"
        )
    if schema != MIGRATION_SCHEMA:
        raise MigrationContractError(
            f"migration target requires schema={MIGRATION_SCHEMA!r}"
        )
    if not isinstance(database_url, str) or not database_url.strip():
        raise MigrationContractError("migration target database URL is missing")
    if database_url != database_url.strip() or any(
        character in database_url for character in "\x00\r\n\t"
    ):
        raise MigrationContractError("migration target database URL is malformed")
    if "?" in database_url or "#" in database_url:
        raise MigrationContractError(
            "migration target database URL must not contain a query or fragment"
        )
    try:
        parts = urlsplit(database_url)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise MigrationContractError("migration target database URL is malformed") from exc
    if parts.scheme not in {"postgresql+asyncpg", "postgresql", "postgres"}:
        raise MigrationContractError("migration target database URL has an unsupported scheme")
    if not parts.netloc or hostname is None:
        raise MigrationContractError("migration target database URL must include a host")
    if any(delimiter in parts.netloc for delimiter in (",", ";")):
        raise MigrationContractError("migration target database URL must contain one host")
    if parts.netloc.count("@") > 1:
        raise MigrationContractError("migration target database URL has ambiguous userinfo")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise MigrationContractError(
            "migration target database host must be a literal loopback IP"
        ) from exc
    if not address.is_loopback:
        raise MigrationContractError(
            "migration target database host must be a literal loopback IP"
        )
    if port is not None and not 1 <= port <= 65535:
        raise MigrationContractError("migration target database port is invalid")
    expected_path = f"/{DISPOSABLE_DATABASE_NAME}"
    if parts.path != expected_path:
        raise MigrationContractError(
            f"migration target database must be exactly {DISPOSABLE_DATABASE_NAME!r}"
        )
    return DisposableMigrationTarget(
        url=database_url,
        database_name=DISPOSABLE_DATABASE_NAME,
        schema=MIGRATION_SCHEMA,
    )


def _alembic_config():
    # Keep Alembic lazy so DB-free catalog and classifier checks do not need
    # application imports or a configured database client.
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_SCRIPT_LOCATION))
    return config


def _script_directory():
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_alembic_config())


def source_heads() -> tuple[str, ...]:
    """Return the source graph heads through Alembic's official API."""

    heads = tuple(sorted(_script_directory().get_heads()))
    if len(heads) != 1:
        raise MigrationHeadInvariantError(
            f"source Alembic graph must have exactly one head; found {heads!r}"
        )
    return heads


def source_head() -> str:
    """Return the single source head without a hardcoded revision constant."""

    return source_heads()[0]


def _downgrade_safety_issues(revision: str) -> tuple[str, ...]:
    """Inspect a historical downgrade body before selecting a live test edge.

    A downgrade containing an explicit ``raise`` or a bare ``pass`` is not a
    reversible fixture candidate.  The database exercise still remains the
    final authority; this source inspection prevents the scenario from
    accidentally choosing a known irreversible historical revision.
    """

    script = _script_directory().get_revision(revision)
    if script is None or script.path is None:
        raise MigrationContractError(f"source revision {revision!r} is unavailable")
    try:
        tree = ast.parse(Path(script.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise MigrationContractError(
            f"source revision {revision!r} cannot be inspected"
        ) from exc
    downgrade = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "downgrade"
        ),
        None,
    )
    if downgrade is None:
        return ("downgrade function is missing",)
    issues: list[str] = []
    body = list(downgrade.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if not body:
        issues.append("downgrade is an irreversible no-op")
    for node in ast.walk(downgrade):
        if isinstance(node, ast.Raise):
            issues.append("downgrade explicitly refuses execution")
        elif isinstance(node, ast.Pass):
            issues.append("downgrade is an irreversible no-op")
    return tuple(dict.fromkeys(issues))


def select_reversible_range() -> tuple[str, str]:
    """Select the latest substantive edge after downgrade safety inspection."""

    script_directory = _script_directory()
    candidate = source_head()
    visited: set[str] = set()
    while candidate not in visited:
        visited.add(candidate)
        revision = script_directory.get_revision(candidate)
        if revision is None:
            raise MigrationContractError(f"source revision {candidate!r} is unavailable")
        parent = revision.down_revision
        if not isinstance(parent, str) or not parent:
            raise MigrationContractError(
                f"source revision {candidate!r} has no single downgrade parent"
            )
        issues = _downgrade_safety_issues(candidate)
        if not issues:
            return parent, candidate
        if issues != ("downgrade is an irreversible no-op",):
            raise MigrationContractError(
                f"source revision {candidate!r} is not a safe reversible-range candidate: "
                + "; ".join(issues)
            )
        candidate = parent
    raise MigrationContractError("source Alembic graph contains a revision cycle")


def inspect_database_heads(
    connection: object,
    *,
    source: Sequence[str] | None = None,
) -> MigrationHeadState:
    """Inspect Alembic's current heads and version rows via official APIs."""

    from alembic.migration import MigrationContext
    from sqlalchemy import text

    source_heads_value = tuple(source) if source is not None else source_heads()
    migration_context = MigrationContext.configure(connection)
    database_heads = tuple(sorted(migration_context.get_current_heads()))
    rows = tuple(
        str(value)
        for value in connection.execute(
            text(
                "SELECT version_num FROM public.alembic_version "
                "ORDER BY version_num"
            )
        ).scalars()
    )
    return MigrationHeadState(
        source_heads=source_heads_value,
        database_heads=database_heads,
        version_rows=rows,
    )


def assert_single_head_state(
    connection: object,
    *,
    source: Sequence[str] | None = None,
    expected_database: Sequence[str] | None = None,
) -> MigrationHeadState:
    """Require exactly one source head, current head and version-table row."""

    state = inspect_database_heads(connection, source=source)
    if len(state.source_heads) != 1:
        raise MigrationHeadInvariantError(
            f"source Alembic graph must have exactly one head; found {state.source_heads!r}"
        )
    if len(state.database_heads) != 1:
        raise MigrationHeadInvariantError(
            f"database Alembic state must have exactly one current head; "
            f"found {state.database_heads!r}"
        )
    if len(state.version_rows) != 1:
        raise MigrationHeadInvariantError(
            f"public.alembic_version must have exactly one row; "
            f"found {state.version_rows!r}"
        )
    if state.database_heads != state.version_rows:
        raise MigrationHeadInvariantError(
            "Alembic current heads and version-table rows disagree: "
            f"heads={state.database_heads!r} rows={state.version_rows!r}"
        )
    expected_heads = (
        tuple(expected_database)
        if expected_database is not None
        else state.source_heads
    )
    if state.database_heads != expected_heads:
        raise MigrationHeadInvariantError(
            "Alembic database head does not match source head: "
            f"source={expected_heads!r} database={state.database_heads!r}"
        )
    return state


def run_migration_subprocess(
    command: Sequence[str],
    *,
    label: str,
    timeout_seconds: float = MIGRATION_SUBPROCESS_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one migration-owned subprocess with a typed hard timeout."""

    try:
        result = subprocess.run(
            list(command),
            cwd=PLATFORM_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or exc.stderr or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        raise MigrationCommandTimeout(
            label=label,
            command=command,
            timeout_seconds=timeout_seconds,
            output=str(output),
        ) from exc
    if check and result.returncode:
        raise MigrationCommandError(
            label=label,
            command=command,
            returncode=result.returncode,
            output=result.stdout,
        )
    return result
