from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
import logging
import re
from time import perf_counter
from typing import Any, Iterator
from uuid import uuid4

from sqlalchemy import event

from python_packages.platform_infra.config import get_settings

logger = logging.getLogger("platform.performance")
QA_PHASE_RE = re.compile(
    r"^(?:write|browser|scale|qa|lifecycle|mass|registration|ready|captain|"
    r"auto|assignment|post|teammate|opponent|bracket|mixed|tournament|"
    r"completed|match|initial)_[a-z0-9_]{1,63}$"
)


@dataclass(slots=True)
class RequestPerformanceMetrics:
    request_id: str
    method: str
    path: str
    started_at: float
    sql_query_count: int = 0
    sql_time_seconds: float = 0.0
    max_sql_time_seconds: float = 0.0
    compute_blocks: int = 0
    compute_time_seconds: float = 0.0
    response_bytes: int = 0
    pool_checkout_wait_seconds: float = 0.0
    redis_read_model_events: list[dict[str, Any]] = field(default_factory=list)
    profile_read_model_events: list[dict[str, Any]] = field(default_factory=list)
    tournament_profile_access_events: list[dict[str, Any]] = field(default_factory=list)
    profile_read_model_fallback_count: int = 0
    # Ready Vote owns one dedicated session checkout for the complete
    # auth/preflight/upsert/commit transaction. Keep this separate from the
    # aggregate pool wait and from generic SQL timing so physical checkout
    # cardinality is visible in request_perf diagnostics.
    ready_vote_checkout_count: int = 0
    ready_vote_checkout_ms: float = 0.0
    ready_vote_admission_inflight: int = 0
    ready_vote_admission_limit: int = 0
    ready_vote_admission_wait_ms: float = 0.0
    ready_vote_admitted_total: int = 0
    ready_vote_shed_total: int = 0
    ready_vote_controller_state: str = "-"
    ready_vote_controller_limit_changes: int = 0
    ready_vote_cpu_pressure: float = 0.0
    ready_vote_pool_wait_ms: float = 0.0
    ready_vote_cpu_monitor_sample_ms: float = 0.0
    ready_vote_cpu_monitor_samples: int = 0
    ready_vote_admission_shed: bool = False
    qa_phase: str | None = None
    cf_ray: str | None = None
    client_fingerprint: str | None = None
    ready_vote_spans: dict[str, float] = field(default_factory=dict)


_current_metrics: ContextVar[RequestPerformanceMetrics | None] = ContextVar(
    "platform_request_performance_metrics",
    default=None,
)
_instrumented_engine_ids: set[int] = set()


def install_sqlalchemy_query_metrics(sync_engine: Any) -> None:
    engine_id = id(sync_engine)
    if engine_id in _instrumented_engine_ids:
        return
    _instrumented_engine_ids.add(engine_id)

    @event.listens_for(sync_engine, "before_cursor_execute")
    def before_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        context._platform_query_started_at = perf_counter()

    @event.listens_for(sync_engine, "after_cursor_execute")
    def after_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        started_at = getattr(context, "_platform_query_started_at", None)
        if started_at is None:
            return
        metrics = _current_metrics.get()
        if metrics is None:
            return
        elapsed = perf_counter() - started_at
        metrics.sql_query_count += 1
        metrics.sql_time_seconds += elapsed
        metrics.max_sql_time_seconds = max(metrics.max_sql_time_seconds, elapsed)


def qa_phase_from_scope(scope: dict[str, Any]) -> str | None:
    for raw_name, raw_value in scope.get("headers") or []:
        if raw_name.lower() != b"x-platform-qa-phase":
            continue
        value = raw_value.decode("ascii", errors="ignore").lower()
        return value if QA_PHASE_RE.fullmatch(value) else None
    return None


def _header_from_scope(scope: dict[str, Any], name: bytes) -> str | None:
    for raw_name, raw_value in scope.get("headers") or []:
        if raw_name.lower() == name:
            value = raw_value.decode("ascii", errors="ignore").strip()
            return value[:80] if value else None
    return None


def _client_fingerprint(scope: dict[str, Any]) -> str | None:
    client = scope.get("client")
    if not client or not client[0]:
        return None
    import hmac
    from hashlib import sha256

    settings = get_settings()
    return hmac.new(
        settings.platform_secret_key.encode("utf-8"),
        str(client[0]).encode("utf-8"),
        sha256,
    ).hexdigest()[:16]


def start_request_metrics(
    method: str,
    path: str,
    *,
    qa_phase: str | None = None,
    request_id: str | None = None,
    cf_ray: str | None = None,
    client_fingerprint: str | None = None,
) -> Token[RequestPerformanceMetrics | None]:
    metrics = RequestPerformanceMetrics(
        request_id=request_id or uuid4().hex[:12],
        method=method,
        path=path,
        started_at=perf_counter(),
        qa_phase=qa_phase,
        cf_ray=cf_ray,
        client_fingerprint=client_fingerprint,
    )
    return _current_metrics.set(metrics)


def current_request_metrics() -> RequestPerformanceMetrics | None:
    return _current_metrics.get()


def record_pool_checkout_wait(elapsed_seconds: float) -> None:
    metrics = _current_metrics.get()
    if metrics is not None:
        metrics.pool_checkout_wait_seconds += max(0.0, elapsed_seconds)


def record_redis_read_model_event(
    *,
    model: str,
    outcome: str,
    get_ms: float = 0.0,
    build_ms: float = 0.0,
    set_ms: float = 0.0,
    payload_bytes: int = 0,
    revision: int | None = None,
) -> None:
    """Attach shared Redis read-model evidence to the current request.

    The event stays on the existing request performance record so lifecycle
    QA and normal request diagnostics use one metrics pipeline.
    """

    metrics = _current_metrics.get()
    if metrics is None:
        return
    metrics.redis_read_model_events.append(
        {
            "model": str(model),
            "outcome": str(outcome),
            "get_ms": round(max(0.0, float(get_ms)), 3),
            "build_ms": round(max(0.0, float(build_ms)), 3),
            "set_ms": round(max(0.0, float(set_ms)), 3),
            "payload_bytes": max(0, int(payload_bytes)),
            "revision": int(revision) if revision is not None else None,
        }
    )


def record_profile_read_model_event(
    event: str,
    *,
    build_ms: float = 0.0,
    pipeline_ms: float = 0.0,
    payload_bytes: int = 0,
    revision: int | None = None,
) -> None:
    metrics = _current_metrics.get()
    if metrics is None:
        return
    metrics.profile_read_model_events.append(
        {
            "event": str(event),
            "build_ms": round(max(0.0, float(build_ms)), 3),
            "pipeline_ms": round(max(0.0, float(pipeline_ms)), 3),
            "payload_bytes": max(0, int(payload_bytes)),
            "revision": int(revision) if revision is not None else None,
        }
    )
    if str(event) == "profile_read_model_db_fallback":
        metrics.profile_read_model_fallback_count += 1


def record_tournament_profile_access_event(
    event: str,
    *,
    pipeline_ms: float = 0.0,
    build_ms: float = 0.0,
    payload_bytes: int = 0,
    revision: int | None = None,
) -> None:
    metrics = _current_metrics.get()
    if metrics is None:
        return
    metrics.tournament_profile_access_events.append(
        {
            "event": str(event),
            "pipeline_ms": round(max(0.0, float(pipeline_ms)), 3),
            "build_ms": round(max(0.0, float(build_ms)), 3),
            "payload_bytes": max(0, int(payload_bytes)),
            "revision": int(revision) if revision is not None else None,
        }
    )


def record_ready_vote_checkout(elapsed_seconds: float) -> None:
    """Record the dedicated Ready Vote checkout at its sole owner."""

    metrics = _current_metrics.get()
    if metrics is None:
        return
    metrics.ready_vote_checkout_count += 1
    metrics.ready_vote_checkout_ms += max(0.0, elapsed_seconds) * 1000


def record_ready_vote_span(name: str, elapsed_seconds: float) -> None:
    metrics = _current_metrics.get()
    if metrics is not None and name.startswith("ready_vote_"):
        metrics.ready_vote_spans[name] = max(0.0, elapsed_seconds)


def record_ready_vote_admission_start(
    *,
    snapshot: Any,
    wait_ms: float,
    admitted: bool,
) -> None:
    """Attach the process-local admission decision to the current request."""

    metrics = _current_metrics.get()
    if metrics is None:
        return
    metrics.ready_vote_admission_inflight = int(snapshot.inflight)
    metrics.ready_vote_admission_limit = int(snapshot.limit)
    metrics.ready_vote_admission_wait_ms = max(0.0, float(wait_ms))
    metrics.ready_vote_admitted_total = int(snapshot.admitted_total)
    metrics.ready_vote_shed_total = int(snapshot.shed_total)
    metrics.ready_vote_controller_state = str(snapshot.state)
    metrics.ready_vote_controller_limit_changes = int(snapshot.limit_changes)
    metrics.ready_vote_cpu_pressure = max(0.0, float(snapshot.cpu_pressure))
    metrics.ready_vote_cpu_monitor_sample_ms = max(
        0.0,
        float(snapshot.cpu_monitor_sample_ms),
    )
    metrics.ready_vote_cpu_monitor_samples = int(snapshot.cpu_monitor_samples)
    metrics.ready_vote_admission_shed = not admitted


def record_ready_vote_admission_completion(*, snapshot: Any, pool_wait_ms: float) -> None:
    """Record completion pressure without changing the request's admission snapshot."""

    metrics = _current_metrics.get()
    if metrics is None:
        return
    metrics.ready_vote_pool_wait_ms = max(0.0, float(pool_wait_ms))
    # Keep cumulative controller values current for slow-request diagnostics.
    metrics.ready_vote_admitted_total = int(snapshot.admitted_total)
    metrics.ready_vote_shed_total = int(snapshot.shed_total)
    metrics.ready_vote_controller_limit_changes = int(snapshot.limit_changes)
    metrics.ready_vote_cpu_pressure = max(0.0, float(snapshot.cpu_pressure))
    metrics.ready_vote_cpu_monitor_sample_ms = max(
        0.0,
        float(snapshot.cpu_monitor_sample_ms),
    )
    metrics.ready_vote_cpu_monitor_samples = int(snapshot.cpu_monitor_samples)


def reset_request_metrics(token: Token[RequestPerformanceMetrics | None]) -> None:
    _current_metrics.reset(token)


@contextmanager
def measure_compute_block() -> Iterator[None]:
    started_at = perf_counter()
    try:
        yield
    finally:
        metrics = _current_metrics.get()
        if metrics is not None:
            metrics.compute_blocks += 1
            metrics.compute_time_seconds += perf_counter() - started_at


class RequestPerformanceMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        if not settings.platform_perf_log_enabled:
            await self.app(scope, receive, send)
            return

        token = start_request_metrics(
            method=str(scope.get("method") or "GET"),
            path=str(scope.get("path") or ""),
            qa_phase=qa_phase_from_scope(scope),
            request_id=_header_from_scope(scope, b"x-request-id"),
            cf_ray=_header_from_scope(scope, b"cf-ray"),
            client_fingerprint=_client_fingerprint(scope),
        )
        metrics = current_request_metrics()
        status_code = 500

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status") or status_code)
                if metrics is not None and metrics.redis_read_model_events:
                    headers = list(message.get("headers") or [])
                    outcome_header = ",".join(
                        f"{event['model']}:{event['outcome']}"
                        for event in metrics.redis_read_model_events
                    )
                    headers.append(
                        (b"x-platform-read-model", outcome_header.encode("ascii"))
                    )
                    message = {**message, "headers": headers}
            elif message["type"] == "http.response.body" and metrics is not None:
                metrics.response_bytes += len(message.get("body") or b"")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            finished_metrics = current_request_metrics()
            reset_request_metrics(token)
            if finished_metrics is not None:
                self._log_if_slow(scope, finished_metrics, status_code)

    def _log_if_slow(
        self,
        scope: dict[str, Any],
        metrics: RequestPerformanceMetrics,
        status_code: int,
    ) -> None:
        settings = get_settings()
        total_seconds = perf_counter() - metrics.started_at
        total_ms = total_seconds * 1000
        sql_ms = metrics.sql_time_seconds * 1000
        route = scope.get("route")
        route_path = getattr(route, "path", None) or metrics.path
        is_ready_vote_route = (
            metrics.method.upper() == "POST"
            and str(route_path).endswith("/deadlock/ready-check/vote")
        )
        should_log = (
            status_code >= 500
            or (
                settings.platform_perf_log_mutations
                and metrics.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
                and not is_ready_vote_route
            )
            or total_ms >= settings.platform_perf_slow_request_ms
            or sql_ms >= settings.platform_perf_slow_db_ms
            or metrics.sql_query_count >= settings.platform_perf_sql_count_threshold
            # Ready Vote is instrumented on every request, but a successful
            # burst must not turn the pool/scheduling span into one INFO log
            # per vote. Slow requests and failures still remain observable.
            or (
                not is_ready_vote_route
                and metrics.pool_checkout_wait_seconds >= 0.1
            )
            or (
                is_ready_vote_route
                and metrics.ready_vote_admission_wait_ms >= 25.0
            )
            or any(
                event["outcome"] in {"error", "fallback_db"}
                for event in metrics.redis_read_model_events
            )
            or any(
                event["event"].endswith(("redis_error", "db_fallback"))
                for event in (
                    metrics.profile_read_model_events
                    + metrics.tournament_profile_access_events
                )
            )
        )
        if not should_log:
            return

        log_method = logger.warning if status_code >= 500 else logger.info
        log_method(
            "request_perf request_id=%s method=%s path=%s route=%s status=%s "
            "total_ms=%.2f sql_ms=%.2f sql_count=%s max_sql_ms=%.2f "
            "compute_ms=%.2f compute_blocks=%s "
            "ready_vote_auth_ms=%.2f ready_vote_checkout_count=%s "
            "ready_vote_checkout_ms=%.2f "
            "ready_vote_preflight_ms=%.2f ready_vote_upsert_ms=%.2f "
            "ready_vote_commit_ms=%.2f "
            "ready_vote_response_ms=%.2f "
            "ready_vote_admission_inflight=%s ready_vote_admission_limit=%s "
            "ready_vote_admission_wait_ms=%.2f ready_vote_admitted_total=%s "
            "ready_vote_shed_total=%s ready_vote_controller_state=%s "
            "ready_vote_controller_limit_changes=%s ready_vote_cpu_pressure=%.2f "
            "ready_vote_admission_shed=%s ready_vote_pool_wait_ms=%.2f "
            "ready_vote_cpu_monitor_sample_ms=%.2f "
            "ready_vote_cpu_monitor_samples=%s "
            "redis_read_model_models=%s redis_read_model_outcomes=%s "
            "redis_read_model_get_ms=%.2f redis_read_model_build_ms=%.2f "
            "redis_read_model_set_ms=%.2f redis_read_model_payload_bytes=%s "
            "redis_read_model_revisions=%s "
            "profile_read_model_metrics=%s tournament_profile_access_metrics=%s "
            "profile_read_model_fallback_count=%s "
            "response_bytes=%s qa_phase=%s "
            "pool_wait_ms=%.2f cf_ray=%s client=%s",
            metrics.request_id,
            metrics.method,
            metrics.path,
            route_path,
            status_code,
            total_ms,
            sql_ms,
            metrics.sql_query_count,
            metrics.max_sql_time_seconds * 1000,
            metrics.compute_time_seconds * 1000,
            metrics.compute_blocks,
            metrics.ready_vote_spans.get("ready_vote_auth_ms", 0.0) * 1000,
            metrics.ready_vote_checkout_count,
            metrics.ready_vote_checkout_ms,
            metrics.ready_vote_spans.get("ready_vote_preflight_ms", 0.0) * 1000,
            metrics.ready_vote_spans.get("ready_vote_upsert_ms", 0.0) * 1000,
            metrics.ready_vote_spans.get("ready_vote_commit_ms", 0.0) * 1000,
            metrics.ready_vote_spans.get("ready_vote_response_ms", 0.0) * 1000,
            metrics.ready_vote_admission_inflight,
            metrics.ready_vote_admission_limit,
            metrics.ready_vote_admission_wait_ms,
            metrics.ready_vote_admitted_total,
            metrics.ready_vote_shed_total,
            metrics.ready_vote_controller_state,
            metrics.ready_vote_controller_limit_changes,
            metrics.ready_vote_cpu_pressure,
            metrics.ready_vote_admission_shed,
            metrics.ready_vote_pool_wait_ms,
            metrics.ready_vote_cpu_monitor_sample_ms,
            metrics.ready_vote_cpu_monitor_samples,
            "|".join(
                str(event["model"])
                for event in metrics.redis_read_model_events
            ) or "-",
            "|".join(
                str(event["outcome"])
                for event in metrics.redis_read_model_events
            ) or "-",
            sum(float(event.get("get_ms") or 0) for event in metrics.redis_read_model_events),
            sum(float(event.get("build_ms") or 0) for event in metrics.redis_read_model_events),
            sum(float(event.get("set_ms") or 0) for event in metrics.redis_read_model_events),
            sum(int(event.get("payload_bytes") or 0) for event in metrics.redis_read_model_events),
            "|".join(
                str(event["revision"])
                for event in metrics.redis_read_model_events
                if event.get("revision") is not None
            ) or "-",
            "|".join(
                str(event["event"])
                for event in metrics.profile_read_model_events
            ) or "-",
            "|".join(
                str(event["event"])
                for event in metrics.tournament_profile_access_events
            ) or "-",
            metrics.profile_read_model_fallback_count,
            metrics.response_bytes,
            metrics.qa_phase or "-",
            metrics.pool_checkout_wait_seconds * 1000,
            metrics.cf_ray or "-",
            metrics.client_fingerprint or "-",
        )
