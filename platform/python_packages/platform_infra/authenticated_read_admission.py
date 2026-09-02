"""Optional process-local admission control for authenticated API reads."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from fastapi.responses import JSONResponse

from python_packages.platform_infra.config import PlatformSettings, get_settings
from python_packages.platform_infra.performance import (
    record_authenticated_read_admission,
)


@dataclass(frozen=True, slots=True)
class AuthenticatedReadAdmissionSnapshot:
    limit: int
    inflight: int
    waiters: int
    admitted_total: int
    shed_total: int


class AuthenticatedReadAdmission:
    """Bound in-flight authenticated reads without creating an unbounded queue."""

    def __init__(self, *, limit: int, max_waiters: int, wait_timeout_ms: float) -> None:
        self.limit = max(1, int(limit))
        self.max_waiters = max(0, int(max_waiters))
        self.wait_timeout_seconds = max(0.0, float(wait_timeout_ms)) / 1000
        self._condition = asyncio.Condition()
        self._inflight = 0
        self._waiters = 0
        self._admitted_total = 0
        self._shed_total = 0

    async def acquire(self) -> tuple[bool, float, AuthenticatedReadAdmissionSnapshot]:
        started_at = perf_counter()
        async with self._condition:
            if self._inflight >= self.limit:
                if self._waiters >= self.max_waiters:
                    self._shed_total += 1
                    return False, perf_counter() - started_at, self.snapshot()
                self._waiters += 1
                try:
                    deadline = perf_counter() + self.wait_timeout_seconds
                    while self._inflight >= self.limit:
                        remaining = deadline - perf_counter()
                        if remaining <= 0:
                            self._shed_total += 1
                            return False, perf_counter() - started_at, self.snapshot()
                        try:
                            await asyncio.wait_for(
                                self._condition.wait(), timeout=remaining
                            )
                        except TimeoutError:
                            self._shed_total += 1
                            return False, perf_counter() - started_at, self.snapshot()
                finally:
                    self._waiters -= 1
            self._inflight += 1
            self._admitted_total += 1
            return True, perf_counter() - started_at, self.snapshot()

    async def release(self) -> None:
        async with self._condition:
            if self._inflight > 0:
                self._inflight -= 1
            self._condition.notify(1)

    def snapshot(self) -> AuthenticatedReadAdmissionSnapshot:
        return AuthenticatedReadAdmissionSnapshot(
            limit=self.limit,
            inflight=self._inflight,
            waiters=self._waiters,
            admitted_total=self._admitted_total,
            shed_total=self._shed_total,
        )


_controller: AuthenticatedReadAdmission | None = None
_controller_config: tuple[int, int, float] | None = None


def get_authenticated_read_admission_controller(
    settings: PlatformSettings | None = None,
) -> AuthenticatedReadAdmission:
    global _controller, _controller_config
    resolved = settings or get_settings()
    config = (
        int(resolved.platform_authenticated_read_admission_concurrency),
        int(resolved.platform_authenticated_read_admission_max_waiters),
        float(resolved.platform_authenticated_read_admission_wait_timeout_ms),
    )
    if _controller is None or _controller_config != config:
        _controller = AuthenticatedReadAdmission(
            limit=config[0],
            max_waiters=config[1],
            wait_timeout_ms=config[2],
        )
        _controller_config = config
    return _controller


def _has_session_cookie(scope: dict[str, Any], cookie_name: str) -> bool:
    expected = cookie_name.encode("latin-1") + b"="
    for raw_name, raw_value in scope.get("headers") or []:
        if raw_name.lower() != b"cookie":
            continue
        return any(
            part.strip().startswith(expected)
            for part in raw_value.split(b";")
        )
    return False


class AuthenticatedReadAdmissionMiddleware:
    """Optionally gate cookie-bearing API reads before FastAPI DB dependencies."""

    def __init__(self, app: Any, settings_factory: Any = get_settings) -> None:
        self.app = app
        self.settings_factory = settings_factory

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        settings = self.settings_factory()
        method = str(scope.get("method") or "GET").upper()
        path = str(scope.get("path") or "")
        should_gate = (
            settings.platform_authenticated_read_admission_enabled
            and method in {"GET", "HEAD"}
            and path.startswith("/api/v1/")
            and not path.startswith("/api/v1/health/")
            and _has_session_cookie(
                scope,
                settings.platform_session_cookie_name,
            )
        )
        if not should_gate:
            await self.app(scope, receive, send)
            return

        controller = get_authenticated_read_admission_controller(settings)
        admitted, wait_seconds, snapshot = await controller.acquire()
        record_authenticated_read_admission(
            wait_seconds=wait_seconds,
            limit=snapshot.limit,
            inflight=snapshot.inflight,
            admitted=admitted,
        )
        if not admitted:
            await JSONResponse(
                status_code=503,
                headers={"Retry-After": "1"},
                content={
                    "detail": "The platform is temporarily busy. Retry the request shortly.",
                    "code": "AUTHENTICATED_READ_OVERLOADED",
                },
            )(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            await controller.release()
