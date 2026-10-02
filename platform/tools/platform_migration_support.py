"""Small, dependency-lazy contracts shared by migration verification paths.

The migration gate and the backend preflight must agree on the repository's
Alembic graph without copying a revision ID into either caller.  This module
keeps the source-head and database-head checks in one place while importing
Alembic/SQLAlchemy only when a resource-bearing caller actually needs them.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
from typing import Sequence
from urllib.parse import urlsplit


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = PLATFORM_ROOT / "alembic.ini"
ALEMBIC_SCRIPT_LOCATION = PLATFORM_ROOT / "alembic"
MIGRATION_SCHEMA = "platform"
DISPOSABLE_DATABASE_NAME = "platformdb_test"
MIGRATION_SCENARIO_DEADLINE_SECONDS = 180.0
MIGRATION_GATE_TIMEOUT_SECONDS = 210.0
MIGRATION_DIAGNOSTIC_BYTES = 4096

_ACTIVE_DEADLINE: ContextVar[float | None] = ContextVar("platform_migration_deadline", default=None)
_MIGRATION_ENV_KEYS = frozenset("PATH HOME LANG LC_ALL PYTHONPATH PLATFORM_ENVIRONMENT PLATFORM_DATABASE_URL PLATFORM_DB_SCHEMA PLATFORM_SECRET_KEY PLATFORM_REDIS_URL PLATFORM_OBJECT_STORAGE_BACKEND PLATFORM_DB_CONNECT_TIMEOUT_SECONDS PLATFORM_DB_COMMAND_TIMEOUT_SECONDS PLATFORM_DB_STATEMENT_TIMEOUT_MS PLATFORM_DB_LOCK_TIMEOUT_MS PLATFORM_ALEMBIC_DB_CONNECT_TIMEOUT_SECONDS PLATFORM_ALEMBIC_DB_COMMAND_TIMEOUT_SECONDS PLATFORM_ALEMBIC_DB_STATEMENT_TIMEOUT_MS PLATFORM_ALEMBIC_DB_LOCK_TIMEOUT_MS".split())


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
        self.output = _redact_diagnostic(output)
        super().__init__(
            f"{label} returned {returncode}; output:\n{self.output}"
        )


class MigrationCommandTimeout(MigrationContractError):
    """Raised when an Alembic/recovery subprocess exceeds its hard deadline."""

    def __init__(
        self,
        *,
        label: str,
        command: Sequence[str],
        timeout_seconds: float = MIGRATION_SCENARIO_DEADLINE_SECONDS,
        output: str = "",
    ) -> None:
        self.label = label
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds
        self.output = _redact_diagnostic(output)
        super().__init__(
            f"{label} exceeded {timeout_seconds:g}s; output:\n{self.output}"
        )


class MigrationCleanupUnproven(MigrationContractError):
    pass


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


def _redact_diagnostic(output: object, env: dict[str, str] | None = None) -> str:
    text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
    for key, value in (env or {}).items():
        if value and re.search(r"PASSWORD|SECRET|TOKEN|PRIVATE|API[_-]?KEY|DATABASE_URL|REDIS_URL", key, re.I):
            text = text.replace(value, "<redacted>")
    text = re.sub(r"(?i)(://[^/\s:@]+):[^@\s]+@", r"\1:<redacted>@", text)
    text = re.sub(r"(?i)(password|secret|token|private[_-]?key|api[_-]?key)\s*[=:]\s*[^\s,;]+", r"\1=<redacted>", text)
    return text[-MIGRATION_DIAGNOSTIC_BYTES:]


def migration_command_env(env: dict[str, str] | None = None) -> dict[str, str]:
    return {key: value for key, value in (os.environ if env is None else env).items() if key in _MIGRATION_ENV_KEYS}


@contextmanager
def migration_scenario_deadline(seconds: float = MIGRATION_SCENARIO_DEADLINE_SECONDS):
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
        raise ValueError("migration scenario deadline must be positive")
    deadline = time.monotonic() + float(seconds)
    token = _ACTIVE_DEADLINE.set(deadline)
    try:
        yield deadline
    finally:
        _ACTIVE_DEADLINE.reset(token)


def run_migration_subprocess(
    command: Sequence[str],
    *,
    label: str,
    deadline: float | None = None,
    timeout_seconds: float | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    active = _ACTIVE_DEADLINE.get()
    if active is not None and (deadline is not None or timeout_seconds is not None):
        raise MigrationContractError("migration subprocesses must share one deadline")
    timeout = MIGRATION_SCENARIO_DEADLINE_SECONDS if timeout_seconds is None else float(timeout_seconds)
    deadline = active if active is not None else deadline or time.monotonic() + timeout
    child_env = migration_command_env(env)
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    tail = bytearray()

    def pump(until: float) -> bool:
        while True:
            for key, _ in selector.select(0):
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 4096)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    raise MigrationCleanupUnproven(f"{label} cleanup could not be proven") from exc
                if chunk:
                    tail[:] = (tail + chunk)[-MIGRATION_DIAGNOSTIC_BYTES:]
                else:
                    selector.unregister(stream)
                    stream.close()
            if process.poll() is not None and not selector.get_map():
                process.wait()
                return True
            remaining = until - time.monotonic()
            if remaining <= 0:
                return False
            selector.select(min(remaining, 0.05))

    def stop() -> None:
        if process is not None and process.poll() is None:
            if deadline - time.monotonic() > 1.0:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if pump(min(deadline, time.monotonic() + 1.0)):
                    return
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if not pump(deadline):
            raise MigrationCleanupUnproven(
                f"{label} cleanup could not be proven; {_redact_diagnostic(bytes(tail), child_env)}"
            )

    try:
        if deadline <= time.monotonic():
            raise MigrationCommandTimeout(label=label, command=command, timeout_seconds=timeout)
        try:
            process = subprocess.Popen(list(command), cwd=PLATFORM_ROOT, env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, bufsize=0)
        except OSError as exc:
            raise MigrationCommandError(
                label=label, command=command, returncode=-1,
                output=f"{type(exc).__name__}: executable unavailable",
            ) from exc
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        if not pump(max(time.monotonic(), deadline - min(2.0, max(0.0, deadline - time.monotonic()) / 10))):
            stop()
            raise MigrationCommandTimeout(
                label=label, command=command, timeout_seconds=timeout,
                output=_redact_diagnostic(bytes(tail), child_env),
            )
        output = _redact_diagnostic(bytes(tail), child_env)
        result = subprocess.CompletedProcess(list(command), process.returncode, output, None)
        if check and result.returncode:
            raise MigrationCommandError(
                label=label, command=command, returncode=result.returncode, output=output,
            )
        return result
    except BaseException as primary:
        if process is not None and process.poll() is None:
            try:
                stop()
            except BaseException as cleanup_error:
                if hasattr(primary, "add_note"):
                    primary.add_note("migration subprocess cleanup could not be proven")
                raise primary from cleanup_error
        raise
    finally:
        selector.close()
        if process is not None and process.stdout is not None:
            process.stdout.close()
