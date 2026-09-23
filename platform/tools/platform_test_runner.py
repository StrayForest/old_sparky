#!/usr/bin/env python3
"""Run one catalog-owned deterministic Python test contour.

The runner keeps normal unittest semantics, but constructs the suite from the
AST ownership catalog and emits a small machine-readable timing/skip summary.
The integration and privileged contours are intentionally serial: the
canonical runner provides the isolated ``platformdb_test`` and Redis
environment, while this process never invents worker databases or namespaces.
"""

from __future__ import annotations

import asyncio
import argparse
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
import grp
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import shutil
import sys
import time
import unittest
from typing import Iterable, Iterator, Mapping, Sequence
import ipaddress
from urllib.parse import urlsplit


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

try:  # Script execution from platform/tools.
    from platform_test_catalog import (
        BACKEND_AGGREGATE,
        BACKEND_CONTOURS,
        CONTOUR_METADATA,
        CONTOUR_TIMEOUT_SECONDS,
        INTENTIONAL_SKIP_IDS,
        TestCase,
        VERIFICATION_CONTOUR,
        catalog_issues,
        cases_for_contour,
        discover_test_cases,
        test_id_digest,
    )
except ModuleNotFoundError:  # Import as tools.platform_test_runner in tests.
    from tools.platform_test_catalog import (  # type: ignore[no-redef]
        BACKEND_AGGREGATE,
        BACKEND_CONTOURS,
        CONTOUR_METADATA,
        CONTOUR_TIMEOUT_SECONDS,
        INTENTIONAL_SKIP_IDS,
        TestCase,
        VERIFICATION_CONTOUR,
        catalog_issues,
        cases_for_contour,
        discover_test_cases,
        test_id_digest,
    )

try:
    from platform_verification_lock import VerificationLockError, verification_resource_lock
except ModuleNotFoundError:  # Import as tools.platform_test_runner in tests.
    from tools.platform_verification_lock import (  # type: ignore[no-redef]
        VerificationLockError,
        verification_resource_lock,
    )


TEST_ENV_CONTOURS = frozenset((BACKEND_AGGREGATE, *BACKEND_CONTOURS))
TEST_RESOURCE_CONTOURS = frozenset((BACKEND_AGGREGATE, "backend-integration"))
INTEGRATION_REQUIRED_ROLE_SLUGS = frozenset(
    {
        "authenticated_user",
        "player",
        "organizer",
        "moderator",
        "editor",
        "admin",
        "superadmin",
    }
)


class TestResourceConfigurationError(ValueError):
    """Raised when a deterministic contour cannot prove its local target."""


@dataclass(frozen=True, slots=True)
class TestResourceConfiguration:
    """The parsed, safety-checked targets used by the test resource helpers.

    This value contains configuration only.  Constructing it never resolves a
    hostname, opens a socket, pings a service, or imports a client library.
    Keeping the validated URLs in one immutable value also prevents a later
    helper from accidentally falling back to an unvalidated settings default.
    """

    environment: str
    database_url: str
    database_host: str
    database_name: str
    database_schema: str
    redis_url: str
    redis_host: str
    redis_database: str


def _configuration_value(
    settings: object | Mapping[str, object] | None,
    attribute: str,
    environment_name: str,
) -> object | None:
    """Read a setting without applying an unsafe default.

    The aggregate CI job intentionally runs with the system interpreter and
    therefore cannot import the application settings package.  Its environment
    is the source of truth in that mode.  Unit tests may pass a small mapping or
    settings double directly; neither path performs I/O.
    """

    if settings is None:
        return os.environ.get(environment_name)
    if isinstance(settings, Mapping):
        return settings.get(attribute)
    return getattr(settings, attribute, None)


def _parse_local_test_url(
    raw_url: object | None,
    *,
    label: str,
    schemes: frozenset[str],
    expected_path: str,
) -> tuple[str, str]:
    """Parse one exact loopback URL without DNS or client-library behavior."""

    if not isinstance(raw_url, str) or not raw_url or raw_url != raw_url.strip():
        raise TestResourceConfigurationError(f"{label} must be a non-empty URL.")
    if any(character in raw_url for character in "\x00\r\n\t"):
        raise TestResourceConfigurationError(f"{label} contains unsafe control characters.")
    # Query strings can carry alternate database names, Redis DB selectors,
    # socket options, or host lists understood by a client but invisible in the
    # path check.  Fragments are never sent to a server and are likewise an
    # ambiguous configuration typo.  Reject their syntax, including empty
    # ``?``/``#`` suffixes, rather than normalizing it.
    if "?" in raw_url or "#" in raw_url:
        raise TestResourceConfigurationError(
            f"{label} must not contain a query string or fragment."
        )
    try:
        parts = urlsplit(raw_url)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise TestResourceConfigurationError(f"{label} is malformed.") from exc
    if parts.scheme not in schemes or not parts.netloc or hostname is None:
        raise TestResourceConfigurationError(
            f"{label} must include an explicit supported scheme and host."
        )
    # A libpq/Redis URL with a host list must never be accepted as a single
    # loopback host.  Reject delimiters in the authority before interpreting
    # ``hostname``.  More than one raw ``@`` is also an unsafe userinfo trick.
    if any(delimiter in parts.netloc for delimiter in (",", ";")):
        raise TestResourceConfigurationError(f"{label} must contain one host only.")
    if parts.netloc.count("@") > 1:
        raise TestResourceConfigurationError(f"{label} contains ambiguous userinfo.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise TestResourceConfigurationError(
            f"{label} host must be a literal loopback IP address."
        ) from exc
    if not address.is_loopback:
        raise TestResourceConfigurationError(
            f"{label} host must be a literal loopback IP address."
        )
    if port is not None and not 1 <= port <= 65535:
        raise TestResourceConfigurationError(f"{label} port is outside 1..65535.")
    if parts.path != expected_path:
        raise TestResourceConfigurationError(
            f"{label} must target exactly {expected_path}."
        )
    return raw_url, hostname


def validate_test_resource_configuration(
    settings: object | Mapping[str, object] | None = None,
) -> TestResourceConfiguration:
    """Fail closed on the complete local test boundary, without touching it.

    The validator is deliberately pure: it reads values already supplied by a
    settings object or the process environment and performs only syntax and
    literal-IP checks.  Callers must invoke it before importing/constructing a
    database or Redis client, and only then may a resource-bearing contour run
    its liveness/readiness checks.
    """

    environment = _configuration_value(settings, "platform_environment", "PLATFORM_ENVIRONMENT")
    if environment != "test":
        raise TestResourceConfigurationError(
            "PLATFORM_ENVIRONMENT must be exactly 'test' for deterministic test contours."
        )
    database_schema = _configuration_value(settings, "platform_db_schema", "PLATFORM_DB_SCHEMA")
    if database_schema != "platform":
        raise TestResourceConfigurationError(
            "PLATFORM_DB_SCHEMA must be exactly 'platform' for deterministic test contours."
        )
    database_url = _configuration_value(
        settings,
        "platform_database_url",
        "PLATFORM_DATABASE_URL",
    )
    normalized_database_url, database_host = _parse_local_test_url(
        database_url,
        label="PLATFORM_DATABASE_URL",
        schemes=frozenset({"postgresql+asyncpg", "postgresql", "postgres"}),
        expected_path="/platformdb_test",
    )
    redis_url = _configuration_value(settings, "platform_redis_url", "PLATFORM_REDIS_URL")
    normalized_redis_url, redis_host = _parse_local_test_url(
        redis_url,
        label="PLATFORM_REDIS_URL",
        schemes=frozenset({"redis", "rediss"}),
        expected_path="/15",
    )
    return TestResourceConfiguration(
        environment=environment,
        database_url=normalized_database_url,
        database_host=database_host,
        database_name="platformdb_test",
        database_schema=database_schema,
        redis_url=normalized_redis_url,
        redis_host=redis_host,
        redis_database="15",
    )


def _integration_preflight_error(
    *,
    migration_heads: Sequence[str],
    role_slugs: Iterable[str],
    expected_head: str,
) -> str | None:
    """Return a stable diagnostic when the shared integration DB is not ready."""

    if tuple(migration_heads) != (expected_head,):
        return (
            "backend-integration requires exactly one migrated Alembic head "
            f"({expected_head}); run the migration gate before this contour"
        )
    missing_roles = sorted(INTEGRATION_REQUIRED_ROLE_SLUGS - set(role_slugs))
    if missing_roles:
        return (
            "backend-integration is missing required seed roles: "
            + ", ".join(missing_roles)
            + "; run the migration gate before this contour"
        )
    return None


def _expected_integration_head() -> str:
    """Read the migration scenario's single source of the disposable head."""

    try:
        from platform_migration_scenario import HEAD_REVISION
    except ModuleNotFoundError:  # Import as tools.platform_test_runner in tests.
        from tools.platform_migration_scenario import HEAD_REVISION  # type: ignore[no-redef]
    return HEAD_REVISION


def _require_integration_resources_ready() -> None:
    """Fail before test discovery when the shared integration resources are unready.

    The integration suite assumes a migrated disposable schema with the role
    seed from the initial migration.  Without this guard, a direct contour
    invocation after the runner's cleanup can spend several minutes turning a
    missing seed into a cascade of misleading HTTP 503 failures.
    """

    # Safety validation is intentionally the first operation in this helper.
    # Do not move imports/client construction above it: this contour is the
    # only one allowed to perform liveness/readiness checks.
    try:
        configuration = validate_test_resource_configuration()
    except TestResourceConfigurationError as exc:
        raise SystemExit(f"LOCAL GATE BLOCKED: {exc}") from exc
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from redis.asyncio import from_url
    except (ImportError, ModuleNotFoundError) as exc:
        raise SystemExit(
            "LOCAL GATE BLOCKED: backend-integration preflight dependencies are unavailable."
        ) from exc

    expected_head = _expected_integration_head()

    async def inspect_database() -> tuple[list[str], list[str]]:
        async_engine = create_async_engine(configuration.database_url, pool_pre_ping=True)
        try:
            async with async_engine.connect() as connection:
                heads = [
                    str(value)
                    for value in (
                        await connection.scalars(
                            text("SELECT version_num FROM public.alembic_version")
                        )
                    ).all()
                ]
                roles = [
                    str(value)
                    for value in (
                        await connection.scalars(text("SELECT slug FROM platform.roles"))
                    ).all()
                ]
                return heads, roles
        finally:
            await async_engine.dispose()

    try:
        migration_heads, role_slugs = asyncio.run(inspect_database())
    except Exception as exc:
        raise SystemExit(
            "LOCAL GATE BLOCKED: backend-integration PostgreSQL preflight is unavailable; "
            "run the migration gate before this contour."
        ) from exc

    error = _integration_preflight_error(
        migration_heads=migration_heads,
        role_slugs=role_slugs,
        expected_head=expected_head,
    )
    if error:
        raise SystemExit("LOCAL GATE BLOCKED: " + error)

    async def check_redis() -> None:
        redis = from_url(configuration.redis_url, decode_responses=False)
        try:
            await redis.ping()
        finally:
            await redis.aclose()

    try:
        asyncio.run(check_redis())
    except Exception as exc:
        raise SystemExit(
            "LOCAL GATE BLOCKED: backend-integration Redis DB15 preflight is unavailable."
        ) from exc


def _teardown_test_resources() -> None:
    """Return only the guarded test database and Redis DB to empty state."""

    # Cleanup is a resource operation too.  Revalidate immediately before it,
    # even when the test body was already validated at process entry.
    try:
        configuration = validate_test_resource_configuration()
    except TestResourceConfigurationError as exc:
        raise RuntimeError(f"refusing unsafe test resource teardown: {exc}") from exc

    async def reset() -> None:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from redis.asyncio import from_url

        async_engine = create_async_engine(configuration.database_url, pool_pre_ping=True)
        try:
            async with async_engine.begin() as connection:
                await connection.execute(text("SET LOCAL lock_timeout = '5s'"))
                await connection.execute(
                    text(
                        """
                        DO $cleanup$
                        DECLARE table_name text;
                        BEGIN
                          FOR table_name IN
                            SELECT tablename
                            FROM pg_catalog.pg_tables
                            WHERE schemaname = 'platform'
                          LOOP
                            EXECUTE format(
                              'TRUNCATE TABLE platform.%I RESTART IDENTITY CASCADE',
                              table_name
                            );
                          END LOOP;
                        END
                        $cleanup$
                        """
                    )
                )
                table_names = await connection.scalars(
                    text(
                        "SELECT tablename FROM pg_catalog.pg_tables "
                        "WHERE schemaname = 'platform' ORDER BY tablename"
                    )
                )
                for table_name in table_names:
                    quoted = str(table_name).replace('"', '""')
                    count = await connection.scalar(
                        text(f'SELECT count(*) FROM platform."{quoted}"')
                    )
                    if int(count or 0) != 0:
                        raise RuntimeError("platformdb_test teardown left rows")
        finally:
            await async_engine.dispose()

        redis = from_url(configuration.redis_url, decode_responses=False)
        try:
            await redis.ping()
            await redis.flushdb()
            if int(await redis.dbsize()) != 0:
                raise RuntimeError("Redis DB15 teardown left keys")
        finally:
            await redis.aclose()

    asyncio.run(reset())


def _require_test_environment(contour: str) -> TestResourceConfiguration | None:
    """Refuse backend contours unless the isolated test target is selected.

    ``platform_run_tests.sh`` performs the same check before it starts this
    process.  Keeping the guard here closes the direct ``platform_verify.py
    <sub-contour>`` and direct-runner paths as well; a caller cannot bypass a
    production-database refusal by invoking the Python runner directly.
    """

    if contour not in TEST_ENV_CONTOURS:
        return None
    try:
        return validate_test_resource_configuration()
    except TestResourceConfigurationError as exc:
        raise SystemExit(f"LOCAL GATE BLOCKED: {exc}") from exc


@dataclass(slots=True)
class TestTiming:
    test_id: str
    duration_ms: float
    outcome: str


class TimingResult(unittest.TextTestResult):
    """Text result with per-test duration and explicit skip ownership."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.timings: list[TestTiming] = []
        self.skipped_ids: list[str] = []
        self.skip_reasons: dict[str, str] = {}
        self.timed_out = False
        self.timeout_message: str | None = None
        self._started_at: float | None = None

    @staticmethod
    def _test_id(test: unittest.case.TestCase) -> str:
        return test.id()

    def startTest(self, test: unittest.case.TestCase) -> None:
        self._started_at = time.perf_counter()
        super().startTest(test)

    def addSuccess(self, test: unittest.case.TestCase) -> None:
        self._record(test, "passed")
        super().addSuccess(test)

    def addFailure(self, test: unittest.case.TestCase, err: object) -> None:
        self._record(test, "failed")
        super().addFailure(test, err)

    def addError(self, test: unittest.case.TestCase, err: object) -> None:
        self._record(test, "error")
        if (
            isinstance(err, tuple)
            and len(err) >= 2
            and isinstance(err[1], ContourTimeout)
        ):
            self.timed_out = True
            self.timeout_message = str(err[1])
            self.shouldStop = True
        super().addError(test, err)

    def addSkip(self, test: unittest.case.TestCase, reason: str) -> None:
        test_id = self._test_id(test)
        self.skipped_ids.append(test_id)
        self.skip_reasons[test_id] = reason
        self._record(test, "skipped")
        super().addSkip(test, reason)

    def addExpectedFailure(self, test: unittest.case.TestCase, err: object) -> None:
        self._record(test, "expected-failure")
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test: unittest.case.TestCase) -> None:
        self._record(test, "unexpected-success")
        super().addUnexpectedSuccess(test)

    def _record(self, test: unittest.case.TestCase, outcome: str) -> None:
        started = self._started_at
        duration_ms = 0.0 if started is None else (time.perf_counter() - started) * 1000
        self.timings.append(
            TestTiming(
                test_id=self._test_id(test),
                duration_ms=round(duration_ms, 3),
                outcome=outcome,
            )
        )
        self._started_at = None


class TimingRunner(unittest.TextTestRunner):
    resultclass = TimingResult

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.last_result: TimingResult | None = None

    def _makeResult(self) -> TimingResult:
        result = super()._makeResult()
        self.last_result = result
        return result


class ContourTimeout(TimeoutError):
    """Raised when a deterministic contour exceeds its catalog deadline."""

    def __init__(self, contour: str, timeout_seconds: int) -> None:
        self.contour = contour
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"{contour} exceeded its {timeout_seconds}s deterministic contour timeout"
        )


@contextmanager
def _contour_deadline(contour: str) -> Iterator[None]:
    """Enforce the catalog timeout for direct and registry-launched calls."""

    timeout_seconds = CONTOUR_TIMEOUT_SECONDS[contour]
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)

    def handle_timeout(_signum: int, _frame: object) -> None:
        raise ContourTimeout(contour, timeout_seconds)

    signal.signal(signal.SIGALRM, handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer != (0.0, 0.0):
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _flatten_ids(cases: Iterable[TestCase]) -> tuple[str, ...]:
    return tuple(case.test_id for case in cases)


def _selector_matches(test_id: str, selector: str) -> bool:
    return test_id == selector or test_id.startswith(selector + ".")


def _select_cases(
    contour: str,
    selectors: Sequence[str],
) -> tuple[TestCase, ...]:
    all_cases = discover_test_cases()
    issues = catalog_issues(all_cases)
    if issues:
        raise SystemExit("CATALOG CONTRACT FAIL: " + " | ".join(issues))
    owned = cases_for_contour(contour, all_cases)
    if not selectors:
        return owned
    selected: list[TestCase] = []
    unknown: list[str] = []
    for selector in selectors:
        matches = [case for case in owned if _selector_matches(case.test_id, selector)]
        if not matches:
            unknown.append(selector)
        selected.extend(matches)
    if unknown:
        raise SystemExit(
            "FOCUS CONTRACT FAIL: selector is not owned by "
            f"{contour}: {', '.join(unknown)}"
        )
    by_id = {case.test_id: case for case in selected}
    return tuple(by_id.values())


def _load_suite(test_ids: Sequence[str]) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_id in test_ids:
        loaded = loader.loadTestsFromName(test_id)
        if loaded.countTestCases() != 1:
            raise SystemExit(
                "DISCOVERY CONTRACT FAIL: unittest resolved "
                f"{loaded.countTestCases()} tests for {test_id}"
            )
        suite.addTests(loaded)
    return suite


def _privileged_preflight(cases: Sequence[TestCase]) -> None:
    if not cases:
        return
    _require_root_identity("backend-privileged")
    modules = {case.module for case in cases}
    if "test_platform_media_processor" in modules:
        if importlib.util.find_spec("PIL") is None:
            raise SystemExit(
                "LOCAL GATE BLOCKED: backend-privileged requires Pillow for "
                "test_platform_media_processor."
            )
        if not shutil.which("runuser") or not shutil.which("test"):
            raise SystemExit(
                "LOCAL GATE BLOCKED: backend-privileged media identity tests "
                "require /usr/bin/runuser and /usr/bin/test."
            )
        try:
            grp.getgrnam("oldsparky-media")
        except KeyError as exc:
            raise SystemExit(
                "LOCAL GATE BLOCKED: backend-privileged media identity tests "
                "require the oldsparky-media group."
            ) from exc
    if "test_platform_live_qa_wrappers" in modules and not Path("/usr/bin/setpriv").is_file():
        raise SystemExit(
            "LOCAL GATE BLOCKED: backend-privileged wrapper tests require /usr/bin/setpriv."
        )


def _require_root_identity(contour: str) -> None:
    """Fail before any resource validation, lock acquisition or test discovery."""

    if os.geteuid() != 0:
        raise SystemExit(
            f"LOCAL GATE BLOCKED: {contour} requires the root test user; "
            "it must not be converted into a skip."
        )


def _requires_root_before_resources(args: argparse.Namespace) -> bool:
    """Return whether this invocation executes a root-owned contour.

    The aggregate artifact-verification path is deliberately exempt: it only
    reads component manifests and is the DB-free backend job.  A normal
    aggregate run still owns the root-required catalog and must fail before
    touching the shared test resources.
    """

    if not CONTOUR_METADATA[args.contour].get("requires_root"):
        return False
    return not (args.contour == BACKEND_AGGREGATE and args.component_dir is not None)


def _write_json(path: Path | None, payload: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _manifest(cases: Sequence[TestCase], contour: str) -> dict[str, object]:
    ordered_cases = sorted(cases, key=lambda case: case.test_id)
    return {
        "schema": 1,
        "contour": contour,
        "aggregate": contour == BACKEND_AGGREGATE,
        "serial": contour in {BACKEND_AGGREGATE, "backend-integration", "backend-privileged"},
        "tests": [
            {
                "id": case.test_id,
                "module": case.module,
                "class": case.class_name,
                "method": case.method_name,
                "line": case.line,
                "async": case.is_async,
                "owner": case.contour,
            }
            for case in ordered_cases
        ],
    }


def _summary(
    *,
    contour: str,
    cases: Sequence[TestCase],
    result: TimingResult | None,
    elapsed_ms: float,
    status: str,
) -> dict[str, object]:
    timings = [] if result is None else [asdict(item) for item in result.timings]
    skipped = [] if result is None else sorted(result.skipped_ids)
    expected_ids = sorted(case.test_id for case in cases)
    executed_ids = sorted(item["test_id"] for item in timings)
    expected_set = set(expected_ids)
    executed_set = set(executed_ids)
    duplicate_ids = sorted(
        test_id for test_id in executed_set if executed_ids.count(test_id) > 1
    )
    missing_ids = sorted(expected_set - executed_set)
    unexpected_ids = sorted(executed_set - expected_set)
    execution_complete = (
        result is not None
        and len(executed_ids) == len(expected_ids)
        and not missing_ids
        and not duplicate_ids
        and not unexpected_ids
    )
    return {
        "schema": 1,
        "contour": contour,
        "status": status,
        "tests_selected": len(cases),
        "tests_run": 0 if result is None else result.testsRun,
        "failures": 0 if result is None else len(result.failures),
        "errors": 0 if result is None else len(result.errors),
        "expected_failures": 0 if result is None else len(result.expectedFailures),
        "unexpected_successes": 0
        if result is None
        else len(result.unexpectedSuccesses),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "skip_reasons": {}
        if result is None
        else dict(sorted(result.skip_reasons.items())),
        "expected_ids": expected_ids,
        "executed_ids": executed_ids,
        "missing_ids": missing_ids,
        "duplicate_ids": duplicate_ids,
        "unexpected_ids": unexpected_ids,
        "execution_complete": execution_complete,
        "elapsed_ms": round(elapsed_ms, 3),
        "timeout_seconds": CONTOUR_TIMEOUT_SECONDS[contour],
        "test_id_digest": test_id_digest(expected_ids),
        "timings": sorted(timings, key=lambda item: item["duration_ms"], reverse=True),
    }


def _read_component_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"component result is not a regular file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"component result is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"component result must be a JSON object: {path}")
    return payload


def _is_finite_nonnegative_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value)) and value >= 0
    except (OverflowError, ValueError):
        return False


def verify_backend_components(component_dir: Path) -> dict[str, object]:
    """Verify successful, exactly-once manifests for the backend DAG.

    This is deliberately a read-only aggregate operation.  It does not import
    application code or contact PostgreSQL/Redis; each component has already
    executed its catalog-owned IDs and supplied an immutable manifest/summary.
    Missing, duplicate, failed or incomplete component artifacts are errors,
    including when a caller tries to present a partial backend as successful.
    """

    if component_dir.is_symlink() or not component_dir.is_dir():
        raise ValueError(f"backend component result directory is unavailable: {component_dir}")
    all_cases = discover_test_cases()
    issues = catalog_issues(all_cases)
    if issues:
        raise ValueError("catalog contract failed: " + " | ".join(issues))

    manifest_paths = sorted(component_dir.rglob("manifest.json"))
    expected_contours = tuple(BACKEND_CONTOURS)
    manifests_by_contour: dict[str, tuple[Path, dict[str, object]]] = {}
    for path in manifest_paths:
        payload = _read_component_json(path)
        contour = payload.get("contour")
        if contour not in expected_contours:
            raise ValueError(f"unexpected backend component manifest contour: {contour!r}")
        if contour in manifests_by_contour:
            raise ValueError(f"duplicate backend component manifest: {contour}")
        manifests_by_contour[contour] = (path, payload)
    missing_manifests = sorted(set(expected_contours) - set(manifests_by_contour))
    if missing_manifests:
        raise ValueError("missing backend component manifests: " + ", ".join(missing_manifests))

    component_ids: dict[str, set[str]] = {}
    component_summaries: dict[str, dict[str, object]] = {}
    for contour in expected_contours:
        manifest_path, manifest = manifests_by_contour[contour]
        if manifest.get("schema") != 1 or manifest.get("aggregate") is not False:
            raise ValueError(f"invalid non-aggregate manifest for {contour}")
        entries = manifest.get("tests")
        if not isinstance(entries, list):
            raise ValueError(f"manifest tests are missing for {contour}")
        ids = [
            entry.get("id")
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        ]
        expected_ids = sorted(
            case.test_id for case in cases_for_contour(contour, all_cases)
        )
        if len(ids) != len(entries) or ids != expected_ids:
            actual_ids = set(ids)
            missing = sorted(set(expected_ids) - actual_ids)
            extra = sorted(actual_ids - set(expected_ids))
            raise ValueError(
                f"manifest IDs must be the exact sorted catalog list for {contour}: "
                f"missing={missing[:3]!r} extra={extra[:3]!r}"
            )
        summary_path = manifest_path.parent / "summary.json"
        summary = _read_component_json(summary_path)
        if summary.get("schema") != 1 or summary.get("contour") != contour:
            raise ValueError(f"invalid summary for {contour}")
        if summary.get("status") != "passed":
            raise ValueError(f"backend component did not pass: {contour}")
        for count_field in (
            "tests_selected",
            "tests_run",
            "failures",
            "errors",
            "expected_failures",
            "unexpected_successes",
            "skipped_count",
        ):
            count_value = summary.get(count_field)
            if not isinstance(count_value, int) or isinstance(count_value, bool) or count_value < 0:
                raise ValueError(f"backend component {count_field} is malformed: {contour}")
        if summary.get("tests_selected") != len(expected_ids):
            raise ValueError(f"selected test count drift for {contour}")
        if summary.get("tests_run") != len(expected_ids):
            raise ValueError(f"backend component did not run every ID: {contour}")
        if summary.get("execution_complete") is not True:
            raise ValueError(f"backend component exactly-once proof is missing: {contour}")
        if summary.get("failures") != 0 or summary.get("errors") != 0:
            raise ValueError(f"backend component has failures/errors: {contour}")
        if (
            summary.get("expected_failures") != 0
            or summary.get("unexpected_successes") != 0
        ):
            raise ValueError(f"backend component has non-passing outcomes: {contour}")
        if summary.get("unexpected_skips") != []:
            raise ValueError(f"backend component has unapproved skips: {contour}")

        if summary.get("test_id_digest") != test_id_digest(expected_ids):
            raise ValueError(f"backend component test-ID digest is inconsistent: {contour}")
        if summary.get("expected_ids") != expected_ids:
            raise ValueError(f"backend component expected-ID evidence is not canonical: {contour}")
        executed = summary.get("executed_ids")
        if executed != expected_ids:
            raise ValueError(
                f"backend component executed IDs must be the exact sorted catalog list: {contour}"
            )
        for field in ("missing_ids", "duplicate_ids", "unexpected_ids"):
            if summary.get(field) != []:
                raise ValueError(f"backend component {field} evidence is not empty: {contour}")

        skipped = summary.get("skipped")
        if not isinstance(skipped, list) or any(
            not isinstance(test_id, str) for test_id in skipped
        ):
            raise ValueError(f"backend component skipped-ID evidence is malformed: {contour}")
        if skipped != sorted(skipped) or len(set(skipped)) != len(skipped):
            raise ValueError(f"backend component skipped-ID evidence is not canonical: {contour}")
        if any(test_id not in expected_ids for test_id in skipped):
            raise ValueError(f"backend component skipped-ID evidence is unexpected: {contour}")
        if summary.get("skipped_count") != len(skipped):
            raise ValueError(f"backend component skipped count is inconsistent: {contour}")
        skip_reasons = summary.get("skip_reasons")
        if not isinstance(skip_reasons, dict) or set(skip_reasons) != set(skipped):
            raise ValueError(f"backend component skip reasons are inconsistent: {contour}")
        if any(
            not isinstance(reason, str) or not reason
            for reason in skip_reasons.values()
        ):
            raise ValueError(f"backend component skip reasons are malformed: {contour}")

        timings = summary.get("timings")
        if not isinstance(timings, list) or len(timings) != len(expected_ids):
            raise ValueError(f"backend component timings are empty or incomplete: {contour}")
        timing_ids: list[str] = []
        outcome_counts: dict[str, int] = {}
        for timing in timings:
            if not isinstance(timing, dict):
                raise ValueError(f"backend component timing entry is malformed: {contour}")
            test_id = timing.get("test_id")
            duration_ms = timing.get("duration_ms")
            outcome = timing.get("outcome")
            if not isinstance(test_id, str):
                raise ValueError(f"backend component timing ID is malformed: {contour}")
            if not _is_finite_nonnegative_number(duration_ms):
                raise ValueError(f"backend component timing duration is malformed: {contour}")
            if outcome not in {
                "passed",
                "failed",
                "error",
                "skipped",
                "expected-failure",
                "unexpected-success",
            }:
                raise ValueError(f"backend component timing outcome is malformed: {contour}")
            timing_ids.append(test_id)
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        if sorted(timing_ids) != expected_ids or len(set(timing_ids)) != len(expected_ids):
            raise ValueError(f"backend component timing IDs are inconsistent: {contour}")
        if outcome_counts.get("failed", 0) != summary.get("failures"):
            raise ValueError(f"backend component failure timing count is inconsistent: {contour}")
        if outcome_counts.get("error", 0) != summary.get("errors"):
            raise ValueError(f"backend component error timing count is inconsistent: {contour}")
        if outcome_counts.get("expected-failure", 0) != summary.get("expected_failures"):
            raise ValueError(f"backend component expected-failure timing count is inconsistent: {contour}")
        if outcome_counts.get("unexpected-success", 0) != summary.get("unexpected_successes"):
            raise ValueError(f"backend component unexpected-success timing count is inconsistent: {contour}")
        if outcome_counts.get("skipped", 0) != len(skipped):
            raise ValueError(f"backend component skip timing count is inconsistent: {contour}")
        if {
            timing["test_id"]
            for timing in timings
            if timing["outcome"] == "skipped"
        } != set(skipped):
            raise ValueError(f"backend component skipped timings are inconsistent: {contour}")
        elapsed_ms = summary.get("elapsed_ms")
        if not _is_finite_nonnegative_number(elapsed_ms):
            raise ValueError(f"backend component elapsed timing is malformed: {contour}")
        if summary.get("timeout_seconds") != CONTOUR_TIMEOUT_SECONDS[contour]:
            raise ValueError(f"backend component timeout budget is inconsistent: {contour}")

        actual_ids = set(ids)
        component_ids[contour] = actual_ids
        component_summaries[contour] = summary

    overlap = sorted(
        test_id
        for index, contour in enumerate(expected_contours)
        for other in expected_contours[index + 1 :]
        for test_id in component_ids[contour].intersection(component_ids[other])
    )
    if overlap:
        raise ValueError("backend component ownership overlaps: " + ", ".join(overlap))
    expected_backend_ids = {
        case.test_id for case in cases_for_contour(BACKEND_AGGREGATE, all_cases)
    }
    union = set().union(*(component_ids[contour] for contour in expected_contours))
    if union != expected_backend_ids:
        raise ValueError("backend component union does not equal the catalog backend aggregate")
    expected_backend_id_list = sorted(expected_backend_ids)
    aggregate_timings = [
        timing
        for contour in expected_contours
        for timing in component_summaries[contour]["timings"]
    ]
    aggregate_timings.sort(key=lambda item: item["test_id"])
    skipped = sorted(
        test_id
        for contour in expected_contours
        for test_id in component_summaries[contour]["skipped"]
    )
    skip_reasons = {
        test_id: component_summaries[contour]["skip_reasons"][test_id]
        for contour in expected_contours
        for test_id in component_summaries[contour]["skipped"]
    }
    return {
        "schema": 1,
        "contour": BACKEND_AGGREGATE,
        "status": "passed",
        "components": list(expected_contours),
        "tests_selected": len(expected_backend_ids),
        "tests_run": sum(int(component_summaries[item]["tests_run"]) for item in expected_contours),
        "failures": 0,
        "errors": 0,
        "expected_failures": 0,
        "unexpected_successes": 0,
        "skipped": skipped,
        "skipped_count": len(skipped),
        "skip_reasons": skip_reasons,
        "expected_ids": expected_backend_id_list,
        "executed_ids": expected_backend_id_list,
        "missing_ids": [],
        "duplicate_ids": [],
        "unexpected_ids": [],
        "unexpected_skips": [],
        "test_id_digest": test_id_digest(expected_backend_id_list),
        "execution_complete": True,
        "timeout_seconds": CONTOUR_TIMEOUT_SECONDS[BACKEND_AGGREGATE],
        "timings": aggregate_timings,
        "component_elapsed_ms": {
            item: component_summaries[item].get("elapsed_ms", 0)
            for item in expected_contours
        },
    }


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one catalog-owned platform test contour.")
    parser.add_argument(
        "--contour",
        choices=(BACKEND_AGGREGATE, *BACKEND_CONTOURS, VERIFICATION_CONTOUR),
        default=BACKEND_AGGREGATE,
    )
    parser.add_argument("--focused", nargs="+", metavar="SELECTOR")
    parser.add_argument("--list", action="store_true", dest="list_ids")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument(
        "--component-dir",
        type=Path,
        help="verify backend component manifests without executing tests",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("selectors", nargs="*")
    args = parser.parse_args(argv)
    if args.focused and args.selectors:
        parser.error("use either --focused selectors or positional selectors, not both")
    return args


def _run_contour(args: argparse.Namespace) -> int:
    if args.component_dir is not None and args.contour != BACKEND_AGGREGATE:
        raise SystemExit("--component-dir is valid only for the backend aggregate")
    selectors = tuple(args.focused or args.selectors)
    started = time.perf_counter()
    cases: tuple[TestCase, ...] = ()
    runner: TimingRunner | None = None
    result: TimingResult | None = None
    timeout_message: str | None = None
    cleanup_error: str | None = None
    if args.component_dir is not None:
        try:
            with _contour_deadline(args.contour):
                aggregate = verify_backend_components(args.component_dir)
        except (ContourTimeout, ValueError) as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            status = "timeout" if isinstance(exc, ContourTimeout) else "failed"
            payload = {
                **(
                    aggregate
                    if "aggregate" in locals() and isinstance(aggregate, dict)
                    else {
                        "schema": 1,
                        "contour": BACKEND_AGGREGATE,
                        "status": status,
                        "tests_selected": 0,
                        "tests_run": 0,
                        "execution_complete": False,
                    }
                ),
                "status": status,
                "elapsed_ms": round(elapsed_ms, 3),
                "error": str(exc),
            }
            _write_json(args.summary, payload)
            print(f"[TEST AGGREGATE] contour={args.contour} status={status}", flush=True)
            print(f"[TEST AGGREGATE FAIL] {exc}", file=sys.stderr)
            return 124 if status == "timeout" else 1
        elapsed_ms = (time.perf_counter() - started) * 1000
        aggregate["elapsed_ms"] = round(elapsed_ms, 3)
        _write_json(args.summary, aggregate)
        if args.manifest is not None:
            _write_json(
                args.manifest,
                {
                    **_manifest(
                        tuple(
                            case
                            for case in discover_test_cases()
                            if case.contour in BACKEND_CONTOURS
                        ),
                        BACKEND_AGGREGATE,
                    ),
                },
            )
        print(
            "[TEST AGGREGATE] "
            f"contour={args.contour} status=passed "
            f"selected={aggregate['tests_selected']} run={aggregate['tests_run']} "
            f"duration_ms={elapsed_ms:.1f}",
            flush=True,
        )
        return 0
    try:
        with _contour_deadline(args.contour):
            cases = _select_cases(args.contour, selectors)
            if args.list_ids:
                for case in cases:
                    print(case.test_id)
                _write_json(args.manifest, _manifest(cases, args.contour))
                return 0
            _privileged_preflight(
                cases
                if CONTOUR_METADATA[args.contour].get("requires_root")
                else ()
            )
            suite = _load_suite(_flatten_ids(cases))
            runner = TimingRunner(verbosity=0 if args.quiet else 1)
            result = runner.run(suite)
            if result.timed_out:
                timeout_message = result.timeout_message
    except ContourTimeout as exc:
        result = None if runner is None else runner.last_result
        timeout_message = str(exc)
    finally:
        if (
            args.contour in TEST_RESOURCE_CONTOURS
            and args.component_dir is None
            and not args.list_ids
        ):
            try:
                _teardown_test_resources()
            except Exception as exc:
                cleanup_error = f"{type(exc).__name__}: {exc}"
                print(
                    f"[TEST CLEANUP FAIL] {cleanup_error}",
                    file=sys.stderr,
                )
            else:
                print(
                    "[TEST CLEANUP] database=platformdb_test redis_db=15 status=passed",
                    flush=True,
                )
    elapsed_ms = (time.perf_counter() - started) * 1000
    strict_skips = args.contour in {
        BACKEND_AGGREGATE,
        *BACKEND_CONTOURS,
        VERIFICATION_CONTOUR,
    }
    skip_reasons = {} if result is None else result.skip_reasons
    unexpected_skips = (
        sorted(
            test_id
            for test_id, reason in skip_reasons.items()
            if INTENTIONAL_SKIP_IDS.get(test_id) != reason
        )
        if strict_skips
        else []
    )
    status = (
        "timeout"
        if timeout_message is not None
        else "passed"
        if result is not None
        and result.wasSuccessful()
        and not result.expectedFailures
        and not result.unexpectedSuccesses
        and not unexpected_skips
        and cleanup_error is None
        and len(result.timings) == len(cases)
        and {item.test_id for item in result.timings}
        == {case.test_id for case in cases}
        else "failed"
    )
    payload = _summary(
        contour=args.contour,
        cases=cases,
        result=result,
        elapsed_ms=elapsed_ms,
        status=status,
    )
    payload["unexpected_skips"] = unexpected_skips
    if not payload["execution_complete"] and timeout_message is None:
        print(
            "[TEST FAIL] deterministic contour did not execute each selected ID "
            "exactly once",
            file=sys.stderr,
        )
    if timeout_message is not None:
        payload["timeout_message"] = timeout_message
    if cleanup_error is not None:
        payload["cleanup_error"] = cleanup_error
    _write_json(args.manifest, _manifest(cases, args.contour))
    _write_json(args.summary, payload)
    print(
        "[TEST SUMMARY] "
        f"contour={args.contour} selected={len(cases)} "
        f"run={0 if result is None else result.testsRun} "
        f"failed={0 if result is None else len(result.failures)} "
        f"errors={0 if result is None else len(result.errors)} "
        f"skipped={0 if result is None else len(result.skipped)} "
        f"duration_ms={elapsed_ms:.1f}",
        flush=True,
    )
    if timeout_message is not None:
        print(f"[TEST TIMEOUT] {timeout_message}", file=sys.stderr)
    if unexpected_skips:
        print(
            "[TEST FAIL] unexpected skips in strict contour: "
            + ", ".join(unexpected_skips),
            file=sys.stderr,
        )
    return 0 if status == "passed" else 124 if status == "timeout" else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    # Root-owned contours must reject an unprivileged caller before config
    # validation, lock acquisition, integration preflight, catalog discovery
    # or any cleanup path can touch shared resources.
    if _requires_root_before_resources(args):
        _require_root_identity(args.contour)
    _require_test_environment(args.contour)
    # Aggregate execution and both resource-bearing sub-contours must share
    # one host-level boundary.  DB-free contours intentionally stay outside
    # this context so local verification does not become globally serial.
    lock_context = (
        verification_resource_lock(args.contour)
        if args.component_dir is None and not args.list_ids
        else nullcontext()
    )
    try:
        with lock_context:
            if (
                args.component_dir is None
                and not args.list_ids
                and args.contour in {BACKEND_AGGREGATE, "backend-integration"}
            ):
                _require_integration_resources_ready()
            return _run_contour(args)
    except VerificationLockError as exc:
        raise SystemExit(f"LOCAL GATE BLOCKED: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
