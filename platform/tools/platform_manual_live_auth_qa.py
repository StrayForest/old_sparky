#!/usr/bin/env python3
"""Fail-closed operator tooling for the manual production auth/Turnstile gate.

The browser remains entirely human-driven.  This tool never launches a browser,
submits a Turnstile token, or reads one-time codes from the database or logs.
Secret display is restricted to a root interactive TTY.  Database access exists
only in ``prepare``, ``attest-and-cleanup`` and the non-passing recovery command
``abort-and-cleanup``.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess  # nosec B404 - fixed argv, shell=False mailbox helper boundary.
import sys
import tempfile
from typing import Callable

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.auth_lifecycle import (
    email_delivery_configured,
    email_verification_required,
)
from python_packages.platform_infra.config import (
    PlatformSettings,
    get_settings,
    validate_platform_settings,
)
from python_packages.platform_infra.csrf import csrf_protection_enabled
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    MediaAsset,
    PasswordCredential,
    Tournament,
    TournamentParticipant,
    User,
    UserSession,
)
from python_packages.platform_infra.security import (
    public_registration_enabled,
    validate_auth_security_settings,
    verify_password,
)
from python_packages.platform_infra.turnstile import (
    expected_turnstile_hostname,
    normalized_turnstile_mode,
)
from tools import platform_cleanup_live_user_qa as cleanup_tool
from tools import platform_provision_live_csp_qa as provisioner


EXPECTED_PRODUCTION_ORIGIN = "https://old-sparky.com"
STATE_VERSION = 1
MAX_PRIVATE_JSON_BYTES = 64 * 1024
MAX_BUNDLE_AGE = timedelta(hours=4)
MAX_STATE_AGE = timedelta(hours=2)
MAX_FUTURE_SKEW = timedelta(seconds=60)
MAILBOX_TIMEOUT_SECONDS = 45
MAILBOX_ENV_NAME = "PLATFORM_LIVE_CSP_QA_BUNDLE"
STATE_KEYS = frozenset(
    {
        "version",
        "marker",
        "prepared_at",
        "expected_origin",
        "bundle_fingerprint",
        "reset_password",
    }
)
REQUIRED_AUDIT_SEQUENCE = (
    "auth.register",
    "auth.email_verification.confirm",
    "auth.logout",
    "auth.login",
    "auth.logout",
    "auth.password_reset.request",
    "auth.password_reset.confirm",
    "auth.account.update",
    "auth.logout",
)
class ManualLiveAuthQaError(RuntimeError):
    """A deliberately non-sensitive operator-facing gate failure."""


def _required_bundle_string(
    bundle: provisioner.BundleIdentity,
    key: str,
) -> str:
    value = bundle.payload.get(key)
    if not isinstance(value, str):
        raise ManualLiveAuthQaError("live QA bundle string contract is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ManualGateState:
    marker: str
    prepared_at: datetime
    expected_origin: str
    bundle_fingerprint: tuple[int, int, int, int]
    reset_password: str = field(repr=False)
    path_fingerprint: tuple[int, int, int, int] = field(repr=False)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ManualLiveAuthQaError(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManualLiveAuthQaError(f"{label} must be a UTC timestamp") from exc
    if (
        not value.endswith("Z")
        or parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
    ):
        raise ManualLiveAuthQaError(f"{label} must be a UTC timestamp")
    return parsed.astimezone(UTC)


def _fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def _state_path(bundle_path: Path) -> Path:
    return bundle_path.with_name(f"{bundle_path.stem}.manual-auth-state.json")


def _inventory_path(bundle_path: Path) -> Path:
    return bundle_path.with_name(f"{bundle_path.stem}.manual-auth-inventory.json")


def _abort_inventory_path(bundle_path: Path) -> Path:
    return bundle_path.with_name(f"{bundle_path.stem}.manual-auth-abort-inventory.json")


def _validate_private_target(path: Path, *, bundle_path: Path) -> None:
    provisioner.validate_secret_parent(path)
    if path.parent != bundle_path.parent or path in {bundle_path}:
        raise ManualLiveAuthQaError("manual QA state paths must remain beside the bundle")


def _load_bundle(
    bundle_path: Path,
    *,
    now: datetime,
    require_fresh: bool = True,
) -> provisioner.BundleIdentity:
    try:
        identity = provisioner.load_bundle(bundle_path)
    except (OSError, provisioner.ProvisioningError) as exc:
        raise ManualLiveAuthQaError("the root-only live QA bundle is invalid") from exc
    created_at = _parse_timestamp(identity.payload.get("created_at"), label="bundle created_at")
    checked_at = now.astimezone(UTC)
    if created_at > checked_at + MAX_FUTURE_SKEW:
        raise ManualLiveAuthQaError("the live QA bundle timestamp is in the future")
    if require_fresh and created_at < checked_at - MAX_BUNDLE_AGE:
        raise ManualLiveAuthQaError("the live QA bundle is outside its bounded lifetime")
    return identity


def _parse_fingerprint(value: object) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(type(item) is not int or item < 0 for item in value)
    ):
        raise ManualLiveAuthQaError("manual QA state has an invalid bundle fingerprint")
    return (value[0], value[1], value[2], value[3])


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ManualLiveAuthQaError("manual QA state contains duplicate keys")
        result[key] = value
    return result


def _read_private_json(
    path: Path,
) -> tuple[object, tuple[int, int, int, int]]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ManualLiveAuthQaError("manual QA has not been prepared") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != 0
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size < 1
        or before.st_size > MAX_PRIVATE_JSON_BYTES
    ):
        raise ManualLiveAuthQaError("manual QA state must be a root-owned 0600 regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ManualLiveAuthQaError("manual QA state could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            _fingerprint(opened) != _fingerprint(before)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != 0
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise ManualLiveAuthQaError("manual QA state changed while opening")
        chunks: list[bytes] = []
        remaining = MAX_PRIVATE_JSON_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(raw) > MAX_PRIVATE_JSON_BYTES or _fingerprint(after) != _fingerprint(opened):
            raise ManualLiveAuthQaError("manual QA state changed while reading")
    except OSError as exc:
        raise ManualLiveAuthQaError("manual QA state could not be read safely") from exc
    finally:
        os.close(descriptor)
    try:
        return (
            json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object),
            _fingerprint(opened),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ManualLiveAuthQaError("manual QA state is invalid") from exc


def _load_state(
    path: Path,
    *,
    bundle_path: Path,
    bundle: provisioner.BundleIdentity,
    now: datetime,
    require_fresh: bool = True,
) -> ManualGateState:
    _validate_private_target(path, bundle_path=bundle_path)
    payload, path_fingerprint = _read_private_json(path)
    if (
        not isinstance(payload, dict)
        or set(payload) != STATE_KEYS
        or type(payload.get("version")) is not int
        or payload.get("version") != STATE_VERSION
        or payload.get("marker") != bundle.marker
        or payload.get("expected_origin") != EXPECTED_PRODUCTION_ORIGIN
    ):
        raise ManualLiveAuthQaError("manual QA state has an unexpected schema")
    prepared_at = _parse_timestamp(payload.get("prepared_at"), label="prepared_at")
    checked_at = now.astimezone(UTC)
    if prepared_at > checked_at + MAX_FUTURE_SKEW:
        raise ManualLiveAuthQaError("manual QA preparation timestamp is in the future")
    if require_fresh and prepared_at < checked_at - MAX_STATE_AGE:
        raise ManualLiveAuthQaError("manual QA preparation has expired")
    bundle_fingerprint = _parse_fingerprint(payload.get("bundle_fingerprint"))
    if bundle_fingerprint != bundle.fingerprint:
        raise ManualLiveAuthQaError("the live QA bundle changed after preparation")
    reset_password = payload.get("reset_password")
    try:
        provisioner.validate_password(reset_password, label="manual reset password")
    except provisioner.ProvisioningError as exc:
        raise ManualLiveAuthQaError("manual QA state has an invalid reset password") from exc
    if not isinstance(reset_password, str):
        raise ManualLiveAuthQaError("manual QA state has an invalid reset password")
    return ManualGateState(
        marker=bundle.marker,
        prepared_at=prepared_at,
        expected_origin=EXPECTED_PRODUCTION_ORIGIN,
        bundle_fingerprint=bundle_fingerprint,
        reset_password=reset_password,
        path_fingerprint=path_fingerprint,
    )


def _fsync_parent(path: Path) -> None:
    parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _publish_private_json(path: Path, payload: dict[str, object]) -> None:
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_PRIVATE_JSON_BYTES:
        raise ManualLiveAuthQaError("manual QA private state exceeds its size limit")
    _remove_stale_private_temporaries(path)
    descriptor: int | None = None
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("short private-state write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary_path, path, follow_symlinks=False)
        temporary_path.unlink()
        temporary_path = None
        _fsync_parent(path)
    except FileExistsError as exc:
        raise ManualLiveAuthQaError("manual QA private state already exists") from exc
    except OSError as exc:
        raise ManualLiveAuthQaError(
            "manual QA private-state publication failed; a complete target may be retained"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _remove_stale_private_temporaries(path: Path) -> None:
    pattern = f".{path.name}.*.tmp"
    removed = False
    try:
        candidates = tuple(path.parent.glob(pattern))
    except OSError as exc:
        raise ManualLiveAuthQaError(
            "manual QA private-state directory could not be inspected"
        ) from exc
    for candidate in candidates:
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise ManualLiveAuthQaError(
                "manual QA stale private state could not be inspected"
            ) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_PRIVATE_JSON_BYTES
        ):
            raise ManualLiveAuthQaError(
                "manual QA stale private state has an unsafe type or owner"
            )
        try:
            candidate.unlink()
        except OSError as exc:
            raise ManualLiveAuthQaError(
                "manual QA stale private state could not be removed"
            ) from exc
        removed = True
    if removed:
        _fsync_parent(path)


def _unlink_unchanged_private_file(
    path: Path,
    *,
    expected_fingerprint: tuple[int, int, int, int],
) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ManualLiveAuthQaError("manual QA private state disappeared") from exc
    if (
        _fingerprint(metadata) != expected_fingerprint
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ManualLiveAuthQaError("manual QA private state changed during cleanup")
    try:
        path.unlink()
        parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError as exc:
        raise ManualLiveAuthQaError("manual QA private state could not be removed") from exc


def _validate_runtime_target(
    settings: PlatformSettings,
    *,
    allow_test_environment: bool = False,
) -> None:
    try:
        validate_platform_settings(settings)
        validate_auth_security_settings(settings)
    except RuntimeError as exc:
        raise ManualLiveAuthQaError("platform runtime configuration is unsafe") from exc
    environment = settings.platform_environment.strip().lower()
    if allow_test_environment and environment == "test":
        return
    if environment != "production":
        raise ManualLiveAuthQaError("manual live auth QA is forbidden outside production")
    if settings.platform_web_origin != EXPECTED_PRODUCTION_ORIGIN:
        raise ManualLiveAuthQaError("manual live auth QA requires the canonical production origin")
    if normalized_turnstile_mode(settings) != "always":
        raise ManualLiveAuthQaError("manual live auth QA requires Turnstile always mode")
    if expected_turnstile_hostname(settings) != "old-sparky.com":
        raise ManualLiveAuthQaError("Turnstile must validate the canonical production hostname")
    if not public_registration_enabled(settings):
        raise ManualLiveAuthQaError("production public registration is disabled")
    if not email_verification_required(settings) or not email_delivery_configured(settings):
        raise ManualLiveAuthQaError("production email verification is not fully configured")
    if not csrf_protection_enabled(settings):
        raise ManualLiveAuthQaError("production CSRF protection is disabled")


async def _assert_automated_scope_absent(
    db_session: AsyncSession,
    *,
    bundle: provisioner.BundleIdentity,
) -> None:
    marker = bundle.marker.lower()
    roster_ids = list(bundle.user_ids)
    expected_description = f"Accelerated live browser acceptance {bundle.marker}."
    checks = (
        await db_session.scalar(
            select(func.count()).select_from(User).where(
                or_(
                    func.lower(User.email).contains(marker),
                    User.id.in_(roster_ids),
                )
            )
        ),
        await db_session.scalar(
            select(func.count()).select_from(UserSession).where(
                UserSession.user_id.in_(roster_ids)
            )
        ),
        await db_session.scalar(
            select(func.count()).select_from(Tournament).where(
                or_(
                    Tournament.organizer_user_id.in_(roster_ids),
                    Tournament.description == expected_description,
                )
            )
        ),
        await db_session.scalar(
            select(func.count()).select_from(TournamentParticipant).where(
                TournamentParticipant.user_id.in_(roster_ids)
            )
        ),
        await db_session.scalar(
            select(func.count()).select_from(MediaAsset).where(
                MediaAsset.owner_user_id.in_(roster_ids)
            )
        ),
    )
    if any(int(value or 0) for value in checks):
        raise ManualLiveAuthQaError(
            "automated live QA fixtures are still present; exact cleanup must finish first"
        )


async def prepare_manual_gate(
    *,
    bundle_path: Path,
    now: datetime | None = None,
    allow_test_environment: bool = False,
) -> None:
    checked_at = (now or _utc_now()).astimezone(UTC)
    bundle = _load_bundle(bundle_path, now=checked_at)
    state_path = _state_path(bundle_path)
    inventory_path = _inventory_path(bundle_path)
    abort_inventory_path = _abort_inventory_path(bundle_path)
    _validate_private_target(state_path, bundle_path=bundle_path)
    _validate_private_target(inventory_path, bundle_path=bundle_path)
    _validate_private_target(abort_inventory_path, bundle_path=bundle_path)
    for private_path in (state_path, inventory_path, abort_inventory_path):
        _remove_stale_private_temporaries(private_path)
    if any(
        path.exists() or path.is_symlink()
        for path in (state_path, inventory_path, abort_inventory_path)
    ):
        raise ManualLiveAuthQaError(
            "manual QA state already exists; finish or recover the prior exact cleanup"
        )
    settings = get_settings()
    _validate_runtime_target(settings, allow_test_environment=allow_test_environment)
    async with session_factory()() as db_session:
        await _assert_automated_scope_absent(db_session, bundle=bundle)
        await db_session.rollback()
    registration_password = _required_bundle_string(bundle, "password")
    reset_password = secrets.token_urlsafe(24)
    while reset_password == registration_password:
        reset_password = secrets.token_urlsafe(24)
    _publish_private_json(
        state_path,
        {
            "version": STATE_VERSION,
            "marker": bundle.marker,
            "prepared_at": _timestamp(checked_at),
            "expected_origin": EXPECTED_PRODUCTION_ORIGIN,
            "bundle_fingerprint": list(bundle.fingerprint),
            "reset_password": reset_password,
        },
    )


def _load_prepared(
    *,
    bundle_path: Path,
    now: datetime | None = None,
    require_fresh: bool = True,
) -> tuple[provisioner.BundleIdentity, ManualGateState]:
    checked_at = (now or _utc_now()).astimezone(UTC)
    bundle = _load_bundle(
        bundle_path,
        now=checked_at,
        require_fresh=require_fresh,
    )
    state = _load_state(
        _state_path(bundle_path),
        bundle_path=bundle_path,
        bundle=bundle,
        now=checked_at,
        require_fresh=require_fresh,
    )
    return bundle, state


def secret_for_display(
    *,
    bundle_path: Path,
    kind: str,
    now: datetime | None = None,
) -> str:
    bundle, state = _load_prepared(bundle_path=bundle_path, now=now)
    if kind == "email":
        base_email = _required_bundle_string(bundle, "email")
        return provisioner.marker_email(base_email, bundle.marker)
    if kind == "display-name":
        return manual_display_name(bundle.marker)
    if kind == "registration-password":
        return _required_bundle_string(bundle, "password")
    if kind == "reset-password":
        return state.reset_password
    raise ManualLiveAuthQaError("unsupported manual QA secret kind")


def manual_display_name(marker: str) -> str:
    return f"liveqa-{hashlib.sha256(marker.encode('ascii')).hexdigest()[:8]}"


def write_secret_to_interactive_tty(secret: str) -> None:
    if os.geteuid() != 0:
        raise ManualLiveAuthQaError("manual QA secret display requires root")
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open("/dev/tty", flags)
    except OSError as exc:
        raise ManualLiveAuthQaError("manual QA secret display requires an interactive TTY") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISCHR(metadata.st_mode) or not os.isatty(descriptor):
            raise ManualLiveAuthQaError("manual QA secret display requires an interactive TTY")
        remaining = memoryview(f"{secret}\n".encode("utf-8"))
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise ManualLiveAuthQaError("manual QA secret display was incomplete")
            remaining = remaining[written:]
    finally:
        os.close(descriptor)


def mailbox_code(
    *,
    bundle_path: Path,
    purpose: str,
    now: datetime | None = None,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> str:
    if purpose not in {"email-verification", "password-reset"}:
        raise ManualLiveAuthQaError("unsupported mailbox code purpose")
    bundle, _state = _load_prepared(bundle_path=bundle_path, now=now)
    helper = _required_bundle_string(bundle, "mailbox_helper")
    try:
        result = runner(
            [helper, "code", purpose],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={MAILBOX_ENV_NAME: str(bundle_path)},
            close_fds=True,
            shell=False,
            timeout=MAILBOX_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManualLiveAuthQaError("mailbox helper failed") from exc
    if result.returncode != 0 or result.stderr or not isinstance(result.stdout, bytes):
        raise ManualLiveAuthQaError("mailbox helper failed")
    try:
        code = result.stdout.decode("ascii").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise ManualLiveAuthQaError("mailbox helper returned an invalid code") from exc
    if len(code) != 6 or not code.isascii() or not code.isdigit() or result.stdout != f"{code}\n".encode("ascii"):
        raise ManualLiveAuthQaError("mailbox helper returned an invalid code")
    return code


def _subject_is_user(row: AuditLog, user_id: str) -> bool:
    return row.subject_type == "user" and row.subject_id == user_id


async def attest_manual_flow(
    db_session: AsyncSession,
    *,
    bundle: provisioner.BundleIdentity,
    state: ManualGateState,
    checked_at: datetime,
) -> str:
    base_email = _required_bundle_string(bundle, "email")
    registration_password = _required_bundle_string(bundle, "password")
    expected_email = provisioner.marker_email(base_email, bundle.marker)
    marker_users = list(
        (
            await db_session.scalars(
                select(User).where(func.lower(User.email).contains(bundle.marker.lower()))
            )
        ).all()
    )
    roster_users = list(
        (
            await db_session.scalars(select(User.id).where(User.id.in_(bundle.user_ids)))
        ).all()
    )
    if roster_users or len(marker_users) != 1 or marker_users[0].email != expected_email:
        raise ManualLiveAuthQaError(
            "manual QA marker scope is not exactly one derived account"
        )
    user = marker_users[0]
    if (
        user.status != "active"
        or user.email_verified_at is None
        or user.created_at < state.prepared_at - timedelta(seconds=5)
        or user.display_name != manual_display_name(bundle.marker)
    ):
        raise ManualLiveAuthQaError("manual QA account lifecycle is incomplete")
    credential = await db_session.scalar(
        select(PasswordCredential).where(PasswordCredential.user_id == user.id)
    )
    if credential is None or not verify_password(
        registration_password, credential.password_hash
    ):
        raise ManualLiveAuthQaError(
            "manual QA must restore the registration password after password reset"
        )
    if verify_password(state.reset_password, credential.password_hash):
        raise ManualLiveAuthQaError("manual QA password restoration is incomplete")

    sessions = list(
        (
            await db_session.scalars(
                select(UserSession).where(UserSession.user_id == user.id)
            )
        ).all()
    )
    active_sessions = [
        session
        for session in sessions
        if session.invalidated_at is None and session.expires_at > checked_at
    ]
    if active_sessions or len(sessions) != 4:
        raise ManualLiveAuthQaError(
            "manual QA must finish with logout and exactly four lifecycle sessions"
        )
    session_ids = {session.id for session in sessions}

    rows = list(
        (
            await db_session.scalars(
                select(AuditLog)
                .where(
                    or_(
                        AuditLog.actor_user_id == user.id,
                        (AuditLog.subject_type == "user")
                        & (AuditLog.subject_id == user.id),
                    )
                )
                .order_by(AuditLog.id)
            )
        ).all()
    )
    if tuple(row.action for row in rows) != REQUIRED_AUDIT_SEQUENCE:
        raise ManualLiveAuthQaError("manual QA audit sequence is incomplete or ambiguous")
    required_rows = rows
    if any(row.created_at < state.prepared_at - timedelta(seconds=5) for row in required_rows):
        raise ManualLiveAuthQaError("manual QA audit sequence predates preparation")
    register, verification, logout_one, login, logout_two, reset_request, reset_confirm, account_update, logout_three = required_rows
    if (
        register.actor_user_id is not None
        or not _subject_is_user(register, user.id)
        or register.payload.get("email") != expected_email
        or register.payload.get("verification_required") is not True
        or verification.actor_user_id is not None
        or not _subject_is_user(verification, user.id)
        or login.actor_user_id != user.id
        or not _subject_is_user(login, user.id)
        or reset_request.actor_user_id is not None
        or not _subject_is_user(reset_request, user.id)
        or reset_confirm.actor_user_id is not None
        or not _subject_is_user(reset_confirm, user.id)
        or reset_confirm.payload.get("session_rotated") is not True
        or account_update.actor_user_id != user.id
        or not _subject_is_user(account_update, user.id)
        or account_update.payload.get("password_changed") is not True
        or account_update.payload.get("email_changed") is not False
        or account_update.payload.get("session_rotated") is not True
    ):
        raise ManualLiveAuthQaError("manual QA audit evidence is inconsistent")
    for logout in (logout_one, logout_two, logout_three):
        if (
            logout.actor_user_id != user.id
            or logout.subject_type != "session"
            or logout.subject_id not in session_ids
        ):
            raise ManualLiveAuthQaError("manual QA logout evidence is inconsistent")

    expected_description = f"Accelerated live browser acceptance {bundle.marker}."
    forbidden_counts = (
        await db_session.scalar(
            select(func.count()).select_from(Tournament).where(
                or_(
                    Tournament.organizer_user_id == user.id,
                    Tournament.description == expected_description,
                )
            )
        ),
        await db_session.scalar(
            select(func.count()).select_from(TournamentParticipant).where(
                TournamentParticipant.user_id == user.id
            )
        ),
        await db_session.scalar(
            select(func.count()).select_from(MediaAsset).where(
                MediaAsset.owner_user_id == user.id
            )
        ),
    )
    if any(int(value or 0) for value in forbidden_counts):
        raise ManualLiveAuthQaError(
            "manual auth QA must not create tournament, participant, or media rows"
        )
    return user.id


async def _verify_absent(*, marker: str, user_id: str) -> None:
    expected_description = f"Accelerated live browser acceptance {marker}."
    async with session_factory()() as db_session:
        checks = (
            await db_session.scalar(
                select(func.count()).select_from(User).where(
                    or_(User.id == user_id, func.lower(User.email).contains(marker.lower()))
                )
            ),
            await db_session.scalar(
                select(func.count()).select_from(UserSession).where(
                    UserSession.user_id == user_id
                )
            ),
            await db_session.scalar(
                select(func.count()).select_from(Tournament).where(
                    or_(
                        Tournament.organizer_user_id == user_id,
                        Tournament.description == expected_description,
                    )
                )
            ),
            await db_session.scalar(
                select(func.count()).select_from(TournamentParticipant).where(
                    TournamentParticipant.user_id == user_id
                )
            ),
            await db_session.scalar(
                select(func.count()).select_from(MediaAsset).where(
                    MediaAsset.owner_user_id == user_id
                )
            ),
            await db_session.scalar(
                select(func.count()).select_from(AuditLog).where(
                    or_(
                        AuditLog.actor_user_id == user_id,
                        (AuditLog.subject_type == "user")
                        & (AuditLog.subject_id == user_id),
                    )
                )
            ),
        )
        await db_session.rollback()
    if any(int(value or 0) for value in checks):
        raise ManualLiveAuthQaError("manual QA exact cleanup left owned rows behind")


async def _finish_exact_cleanup(
    *,
    bundle_path: Path,
    bundle: provisioner.BundleIdentity,
    state: ManualGateState,
    inventory_path: Path,
    inventory: cleanup_tool.CleanupInventory,
    user_id: str,
    allow_test_environment: bool,
) -> dict[str, int]:
    inventory_fingerprint = _fingerprint(inventory_path.lstat())
    result = await cleanup_tool.cleanup(
        inventory,
        _allow_test_environment=allow_test_environment,
    )
    await _verify_absent(marker=bundle.marker, user_id=user_id)
    _unlink_unchanged_private_file(
        inventory_path,
        expected_fingerprint=inventory_fingerprint,
    )
    _unlink_unchanged_private_file(
        _state_path(bundle_path),
        expected_fingerprint=state.path_fingerprint,
    )
    return result


def _load_retained_inventory(
    *,
    inventory_path: Path,
    marker: str,
) -> cleanup_tool.CleanupInventory:
    try:
        inventory = cleanup_tool.load_inventory(
            inventory_path,
            expected_marker=marker,
        )
    except (OSError, ValueError) as exc:
        raise ManualLiveAuthQaError("retained manual QA inventory is invalid") from exc
    if len(inventory.user_ids) != 1 or inventory.tournament_ids or inventory.media_ids:
        raise ManualLiveAuthQaError("retained manual QA inventory has unexpected scope")
    return inventory


async def attest_and_cleanup(
    *,
    bundle_path: Path,
    now: datetime | None = None,
    allow_test_environment: bool = False,
) -> dict[str, int]:
    checked_at = (now or _utc_now()).astimezone(UTC)
    bundle, state = _load_prepared(bundle_path=bundle_path, now=checked_at)
    settings = get_settings()
    _validate_runtime_target(settings, allow_test_environment=allow_test_environment)
    inventory_path = _inventory_path(bundle_path)
    abort_inventory_path = _abort_inventory_path(bundle_path)
    _validate_private_target(inventory_path, bundle_path=bundle_path)
    _validate_private_target(abort_inventory_path, bundle_path=bundle_path)
    for private_path in (
        _state_path(bundle_path),
        inventory_path,
        abort_inventory_path,
    ):
        _remove_stale_private_temporaries(private_path)
    if abort_inventory_path.exists() or abort_inventory_path.is_symlink():
        raise ManualLiveAuthQaError(
            "an aborted manual QA cleanup must be recovered with abort-and-cleanup"
        )

    if inventory_path.exists() or inventory_path.is_symlink():
        inventory = _load_retained_inventory(
            inventory_path=inventory_path,
            marker=bundle.marker,
        )
        user_id = inventory.user_ids[0]
    else:
        async with session_factory()() as db_session:
            user_id = await attest_manual_flow(
                db_session,
                bundle=bundle,
                state=state,
                checked_at=checked_at,
            )
            await db_session.rollback()
        _publish_private_json(
            inventory_path,
            {
                "version": 1,
                "marker": bundle.marker,
                "user_ids": [user_id],
                "tournament_ids": [],
                "media_ids": [],
            },
        )
        inventory = cleanup_tool.load_inventory(
            inventory_path,
            expected_marker=bundle.marker,
        )
    return await _finish_exact_cleanup(
        bundle_path=bundle_path,
        bundle=bundle,
        state=state,
        inventory_path=inventory_path,
        inventory=inventory,
        user_id=user_id,
        allow_test_environment=allow_test_environment,
    )


async def _resolve_abort_user_id(
    db_session: AsyncSession,
    *,
    bundle: provisioner.BundleIdentity,
) -> str | None:
    base_email = _required_bundle_string(bundle, "email")
    expected_email = provisioner.marker_email(base_email, bundle.marker)
    marker_users = list(
        (
            await db_session.scalars(
                select(User).where(func.lower(User.email).contains(bundle.marker.lower()))
            )
        ).all()
    )
    roster_user_count = await db_session.scalar(
        select(func.count()).select_from(User).where(User.id.in_(bundle.user_ids))
    )
    roster_session_count = await db_session.scalar(
        select(func.count()).select_from(UserSession).where(
            UserSession.user_id.in_(bundle.user_ids)
        )
    )
    expected_description = f"Accelerated live browser acceptance {bundle.marker}."
    marker_tournament_count = await db_session.scalar(
        select(func.count()).select_from(Tournament).where(
            Tournament.description == expected_description
        )
    )
    roster_owned_counts = (
        await db_session.scalar(
            select(func.count()).select_from(Tournament).where(
                Tournament.organizer_user_id.in_(bundle.user_ids)
            )
        ),
        await db_session.scalar(
            select(func.count()).select_from(TournamentParticipant).where(
                TournamentParticipant.user_id.in_(bundle.user_ids)
            )
        ),
        await db_session.scalar(
            select(func.count()).select_from(MediaAsset).where(
                MediaAsset.owner_user_id.in_(bundle.user_ids)
            )
        ),
    )
    if (
        int(roster_user_count or 0)
        or int(roster_session_count or 0)
        or int(marker_tournament_count or 0)
        or any(int(value or 0) for value in roster_owned_counts)
        or len(marker_users) > 1
        or (marker_users and marker_users[0].email != expected_email)
    ):
        raise ManualLiveAuthQaError("manual QA abort scope is ambiguous")
    if not marker_users:
        return None
    user_id = marker_users[0].id
    forbidden_counts = (
        await db_session.scalar(
            select(func.count()).select_from(Tournament).where(
                Tournament.organizer_user_id == user_id
            )
        ),
        await db_session.scalar(
            select(func.count()).select_from(TournamentParticipant).where(
                TournamentParticipant.user_id == user_id
            )
        ),
        await db_session.scalar(
            select(func.count()).select_from(MediaAsset).where(
                MediaAsset.owner_user_id == user_id
            )
        ),
    )
    if any(int(value or 0) for value in forbidden_counts):
        raise ManualLiveAuthQaError(
            "manual QA abort refuses tournament, participant, or media ownership"
        )
    return user_id


async def abort_and_cleanup(
    *,
    bundle_path: Path,
    now: datetime | None = None,
    allow_test_environment: bool = False,
) -> dict[str, int]:
    checked_at = (now or _utc_now()).astimezone(UTC)
    bundle, state = _load_prepared(
        bundle_path=bundle_path,
        now=checked_at,
        require_fresh=False,
    )
    _validate_runtime_target(
        get_settings(),
        allow_test_environment=allow_test_environment,
    )
    attested_inventory_path = _inventory_path(bundle_path)
    abort_inventory_path = _abort_inventory_path(bundle_path)
    _validate_private_target(attested_inventory_path, bundle_path=bundle_path)
    _validate_private_target(abort_inventory_path, bundle_path=bundle_path)
    for private_path in (
        _state_path(bundle_path),
        attested_inventory_path,
        abort_inventory_path,
    ):
        _remove_stale_private_temporaries(private_path)
    attested_exists = (
        attested_inventory_path.exists() or attested_inventory_path.is_symlink()
    )
    abort_exists = abort_inventory_path.exists() or abort_inventory_path.is_symlink()
    if attested_exists and abort_exists:
        raise ManualLiveAuthQaError("manual QA recovery inventories are ambiguous")
    if attested_exists or abort_exists:
        inventory_path = (
            attested_inventory_path if attested_exists else abort_inventory_path
        )
        inventory = _load_retained_inventory(
            inventory_path=inventory_path,
            marker=bundle.marker,
        )
        user_id = inventory.user_ids[0]
    else:
        inventory_path = abort_inventory_path
        async with session_factory()() as db_session:
            user_id = await _resolve_abort_user_id(db_session, bundle=bundle)
            await db_session.rollback()
        if user_id is None:
            _unlink_unchanged_private_file(
                _state_path(bundle_path),
                expected_fingerprint=state.path_fingerprint,
            )
            return {
                "tournaments": 0,
                "users": 0,
                "media": 0,
                "sessions": 0,
                "audit_logs": 0,
            }
        _publish_private_json(
            inventory_path,
            {
                "version": 1,
                "marker": bundle.marker,
                "user_ids": [user_id],
                "tournament_ids": [],
                "media_ids": [],
            },
        )
        inventory = cleanup_tool.load_inventory(
            inventory_path,
            expected_marker=bundle.marker,
        )
    return await _finish_exact_cleanup(
        bundle_path=bundle_path,
        bundle=bundle,
        state=state,
        inventory_path=inventory_path,
        inventory=inventory,
        user_id=user_id,
        allow_test_environment=allow_test_environment,
    )


async def _run_prepare(bundle_path: Path) -> int:
    try:
        await prepare_manual_gate(bundle_path=bundle_path)
        print("Manual auth QA prepared after exact automated-fixture absence check.")
        return 0
    finally:
        await dispose_engine()


async def _run_attest_cleanup(bundle_path: Path) -> int:
    try:
        result = await attest_and_cleanup(bundle_path=bundle_path)
        print(
            "Manual auth QA attested and exact cleanup verified absent: "
            f"users={result['users']} sessions={result['sessions']} "
            f"tournaments={result['tournaments']} media={result['media']} "
            f"audit_logs={result['audit_logs']}"
        )
        return 0
    finally:
        await dispose_engine()


async def _run_abort_cleanup(bundle_path: Path) -> int:
    try:
        result = await abort_and_cleanup(bundle_path=bundle_path)
        print(
            "Manual auth QA ABORTED; exact cleanup verified absent: "
            f"users={result['users']} sessions={result['sessions']} "
            f"tournaments={result['tournaments']} media={result['media']} "
            f"audit_logs={result['audit_logs']}"
        )
        return 0
    finally:
        await dispose_engine()


def _require_root() -> None:
    if os.geteuid() != 0:
        raise ManualLiveAuthQaError("manual live auth QA requires root")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Human-driven production auth/Turnstile QA helper."
    )
    parser.add_argument("--bundle-path", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    commands.add_parser("show-email")
    commands.add_parser("show-display-name")
    password_parser = commands.add_parser("show-password")
    password_parser.add_argument(
        "kind",
        choices=("registration", "reset"),
    )
    code_parser = commands.add_parser("code")
    code_parser.add_argument(
        "purpose",
        choices=("email-verification", "password-reset"),
    )
    commands.add_parser("attest-and-cleanup")
    commands.add_parser("abort-and-cleanup")
    args = parser.parse_args(argv)
    try:
        _require_root()
        if args.command == "prepare":
            return asyncio.run(_run_prepare(args.bundle_path))
        if args.command == "show-email":
            secret = secret_for_display(bundle_path=args.bundle_path, kind="email")
            write_secret_to_interactive_tty(secret)
            return 0
        if args.command == "show-display-name":
            display_name = secret_for_display(
                bundle_path=args.bundle_path,
                kind="display-name",
            )
            write_secret_to_interactive_tty(display_name)
            return 0
        if args.command == "show-password":
            secret = secret_for_display(
                bundle_path=args.bundle_path,
                kind=f"{args.kind}-password",
            )
            write_secret_to_interactive_tty(secret)
            return 0
        if args.command == "code":
            code = mailbox_code(bundle_path=args.bundle_path, purpose=args.purpose)
            write_secret_to_interactive_tty(code)
            return 0
        if args.command == "attest-and-cleanup":
            return asyncio.run(_run_attest_cleanup(args.bundle_path))
        if args.command == "abort-and-cleanup":
            return asyncio.run(_run_abort_cleanup(args.bundle_path))
    except ManualLiveAuthQaError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("Manual auth QA failed.", file=sys.stderr)
        return 1
    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
