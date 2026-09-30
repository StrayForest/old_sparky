"""Scoped password fixture support for database-backed integration tests.

The API integration suites create many users whose password is never changed or
re-hashed by the scenario. They can store one precomputed Argon2id value for
that fixture password, while authentication tests continue to exercise the
real production hasher and verifier independently.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock, get_ident
from typing import Callable, Iterator

from apps.platform_api.app.api.routes import registration as registration_routes


INTEGRATION_PASSWORD = "integration-pass-123"

# Generated once with pwdlib 0.3.0's PasswordHash.recommended() for the
# fixture password above. This is test data only; production code and its
# Argon2id parameters are not changed by this optimization.
INTEGRATION_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "I2JAvIcWbyUpoH1muaIcew$skYp5TS01VX+OTDOAPvmC8w/yg7j4o62wpPX7hSY4qQ"
)

_REGISTRATION_HASH_TARGET = "apps.platform_api.app.api.routes.registration.hash_password"


class _FixtureAuthorization:
    """Authorization copied by ContextVar but valid only in its owner task."""

    __slots__ = ("owner_thread_id", "owner_task")

    def __init__(self, owner_thread_id: int, owner_task: object) -> None:
        self.owner_thread_id = owner_thread_id
        self.owner_task = owner_task


_authorization: ContextVar[_FixtureAuthorization | None] = ContextVar(
    "integration_password_fixture_authorization",
    default=None,
)
_patch_lock = RLock()


@dataclass(slots=True)
class _DispatcherLifecycle:
    original: Callable[[str], str]
    active: bool = True


@dataclass(slots=True)
class _ActivePatch:
    lifecycle: _DispatcherLifecycle
    dispatcher: Callable[[str], str]
    active_count: int = 1


_active_patch: _ActivePatch | None = None


def _current_task() -> object | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        # A copied async context in a worker thread has no running task and
        # therefore remains unpatched.
        return None


def _running_task() -> object:
    task = _current_task()
    if task is None:
        raise RuntimeError(
            "integration password fixture requires a running asyncio task"
        )
    return task


def _make_registration_hash_dispatcher(
    lifecycle: _DispatcherLifecycle,
) -> Callable[[str], str]:
    """Create a dispatcher whose fallback cannot outlive its captured original."""

    original = lifecycle.original

    def dispatcher(password: str) -> str:
        authorization = _authorization.get()
        if (
            lifecycle.active
            and authorization is not None
            and authorization.owner_thread_id == get_ident()
            and authorization.owner_task is _current_task()
        ):
            return fixed_integration_password_hash(password)
        return original(password)

    return dispatcher


def fixed_integration_password_hash(password: str) -> str:
    """Return the fixed fixture hash and reject accidental other passwords."""

    if password != INTEGRATION_PASSWORD:
        raise AssertionError(
            "the fixed integration password hash may only be used for the "
            f"{INTEGRATION_PASSWORD!r} fixture"
        )
    return INTEGRATION_PASSWORD_HASH


@contextmanager
def patch_integration_registration_hash() -> Iterator[None]:
    """Install a task-local registration shortcut for one fixture call.

    The route module has one process-global function attribute, so overlapping
    contexts share one dispatcher and a reference count. Opening the scope
    requires a running asyncio task. The dispatcher is never authorized merely
    because a ContextVar was inherited by a child task or worker thread: both
    its owner-task and owner-thread identities must match.
    """

    global _active_patch

    owner_task = _running_task()
    token = _authorization.set(_FixtureAuthorization(get_ident(), owner_task))
    installed = False
    active: _ActivePatch | None = None
    try:
        with _patch_lock:
            active = _active_patch
            if active is None:
                original = registration_routes.hash_password
                lifecycle = _DispatcherLifecycle(original=original)
                dispatcher = _make_registration_hash_dispatcher(lifecycle)
                active = _ActivePatch(lifecycle=lifecycle, dispatcher=dispatcher)
                registration_routes.hash_password = dispatcher
                _active_patch = active
            elif registration_routes.hash_password is not active.dispatcher:
                registration_routes.hash_password = active.dispatcher
                raise RuntimeError(
                    f"{_REGISTRATION_HASH_TARGET} changed while the fixture dispatcher was active; "
                    "the dispatcher was restored"
                )
            else:
                active.active_count += 1
            installed = True
        yield
    finally:
        _authorization.reset(token)
        if installed:
            with _patch_lock:
                if active is None or _active_patch is not active:
                    if active is not None:
                        active.lifecycle.active = False
                        registration_routes.hash_password = active.lifecycle.original
                    _active_patch = None
                    raise RuntimeError(
                        "integration password fixture patch state changed unexpectedly; "
                        "the original hash was restored"
                    )

                mutation_error: RuntimeError | None = None
                if registration_routes.hash_password is not active.dispatcher:
                    mutation_error = RuntimeError(
                        f"{_REGISTRATION_HASH_TARGET} changed during fixture scope; "
                        "the original hash was restored"
                    )

                active.active_count -= 1
                if active.active_count == 0:
                    active.lifecycle.active = False
                    registration_routes.hash_password = active.lifecycle.original
                    _active_patch = None
                else:
                    registration_routes.hash_password = active.dispatcher

                if mutation_error is not None:
                    raise mutation_error
