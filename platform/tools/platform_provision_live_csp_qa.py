#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
from typing import Callable
from uuid import UUID

from pydantic import EmailStr, TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_domain.deadlock.constants import (
    CAPTAIN_PRIORITY_OPTIONS,
    PLAYTIME_OPTIONS,
    RANKS,
    ROLE_OPTIONS,
)
from python_packages.platform_domain.deadlock.registration import (
    RegistrationPayload,
    validate_registration_payload,
)
from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.config import (
    PlatformSettings,
    get_settings,
    validate_platform_settings,
)
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    DeadlockProfile,
    PasswordCredential,
    PlayerProfile,
    Role,
    Tournament,
    User,
    UserRole,
    UserSession,
)
from python_packages.platform_infra.security import (
    hash_password,
    new_session_token,
    session_token_digest,
)


ROSTER_SIZE = 13
BUNDLE_VERSION = 1
MARKER_PATTERN = re.compile(r"^liveqa-[a-z0-9-]{6,56}$")
MAX_BUNDLE_BYTES = 64 * 1024
LIVE_BROWSER_SESSION_TTL = timedelta(hours=1)
EMAIL_ADAPTER = TypeAdapter(EmailStr)
REQUIRED_ROLE_SLUGS = frozenset({"authenticated_user", "player"})
RECEIVING_DOMAIN = "auth.old-sparky.com"
EXPECTED_PRODUCTION_ORIGIN = "https://old-sparky.com"
BUNDLE_KEYS = frozenset(
    {
        "version",
        "marker",
        "created_at",
        "email",
        "password",
        "mailbox_helper",
        "roster_accounts",
    }
)
ROSTER_ACCOUNT_KEYS = frozenset({"id", "email", "password"})


class ProvisioningError(RuntimeError):
    """A deliberately non-sensitive operator-facing provisioning failure."""


@dataclass(frozen=True, slots=True)
class BundleIdentity:
    marker: str
    user_ids: tuple[str, ...]
    payload: dict[str, object]
    fingerprint: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class InventoryIdentity:
    marker: str
    user_ids: tuple[str, ...]
    payload: dict[str, object]
    fingerprint: tuple[int, int, int, int]


def validate_marker(value: str) -> str:
    marker = value.strip().lower()
    if marker != value or not MARKER_PATTERN.fullmatch(marker):
        raise ProvisioningError(
            "marker must be lowercase, start with liveqa-, and contain only letters, digits, or dashes"
        )
    return marker


def validate_email(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ProvisioningError(f"{label} must be an email address")
    try:
        normalized = str(EMAIL_ADAPTER.validate_python(value.strip())).lower()
    except ValueError as exc:
        raise ProvisioningError(f"{label} must be an email address") from exc
    if len(normalized) > 254 or not normalized.isascii():
        raise ProvisioningError(f"{label} must be a bounded ASCII email address")
    return normalized


def validate_password(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not 10 <= len(value) <= 128:
        raise ProvisioningError(f"{label} must contain between 10 and 128 characters")
    return value


def marker_email(base_email: str, marker: str) -> str:
    normalized_base = validate_email(base_email, label="primary email")
    if marker in normalized_base:
        raise ProvisioningError(
            "primary email must not already contain the liveqa marker"
        )
    local, domain = normalized_base.rsplit("@", 1)
    if domain != RECEIVING_DOMAIN:
        raise ProvisioningError("primary email must use the approved receiving domain")
    candidate = f"{local}+{marker}@{domain}"
    if len(candidate) > 254 or len(candidate.rsplit("@", 1)[0]) > 64:
        raise ProvisioningError("marker-derived primary email is too long")
    return validate_email(candidate, label="marker-derived primary email")


def roster_emails(base_email: str, marker: str) -> tuple[str, ...]:
    normalized_base = validate_email(base_email, label="primary email")
    if marker in normalized_base:
        raise ProvisioningError(
            "primary email must not already contain the liveqa marker"
        )
    domain = normalized_base.rsplit("@", 1)[1]
    if domain != RECEIVING_DOMAIN:
        raise ProvisioningError("primary email must use the approved receiving domain")
    emails = tuple(
        validate_email(
            f"roster{index:02d}+{marker}@{domain}",
            label=f"roster email {index}",
        )
        for index in range(1, ROSTER_SIZE + 1)
    )
    if any(email.count(marker) != 1 for email in emails):
        raise ProvisioningError(
            "every roster email must contain the liveqa marker exactly once"
        )
    if len(set(emails)) != ROSTER_SIZE:
        raise ProvisioningError("roster emails must be unique")
    marker_email(normalized_base, marker)
    return emails


def generate_unique_passwords(
    count: int,
    *,
    factory: Callable[[], str] | None = None,
) -> tuple[str, ...]:
    if count < 1:
        raise ProvisioningError("password count must be positive")
    generate = factory or (lambda: secrets.token_urlsafe(24))
    passwords: list[str] = []
    attempts = 0
    while len(passwords) < count and attempts < count * 20:
        attempts += 1
        candidate = generate()
        if not isinstance(candidate, str) or not 10 <= len(candidate) <= 128:
            continue
        if candidate not in passwords:
            passwords.append(candidate)
    if len(passwords) != count:
        raise ProvisioningError("could not generate the required unique passwords")
    return tuple(passwords)


def _path_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def validate_secret_parent(path: Path) -> None:
    if not path.is_absolute():
        raise ProvisioningError("bundle path must be absolute")
    parent = path.parent
    try:
        metadata = parent.lstat()
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise ProvisioningError("bundle parent must already exist") from exc
    if resolved_parent != parent or not stat.S_ISDIR(metadata.st_mode):
        raise ProvisioningError("bundle parent must be a real directory, not a symlink")
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ProvisioningError("bundle parent must be root-owned with mode 0700")


def _validate_root_controlled_chain(path: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ProvisioningError("mailbox helper is unavailable") from exc
    if resolved != path:
        raise ProvisioningError(
            "mailbox helper path must not contain symlinked components"
        )
    current = Path(path.anchor)
    for component in path.parts[1:-1]:
        current /= component
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ProvisioningError("mailbox helper ancestor is unavailable") from exc
        mode = stat.S_IMODE(metadata.st_mode)
        sticky_root_directory = bool(metadata.st_mode & stat.S_ISVTX)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or ((mode & 0o022) != 0 and not sticky_root_directory)
        ):
            raise ProvisioningError(
                "mailbox helper path must stay under root-controlled directories"
            )


def validate_mailbox_helper(path: Path) -> str:
    if not path.is_absolute():
        raise ProvisioningError("mailbox helper path must be absolute")
    _validate_root_controlled_chain(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProvisioningError("mailbox helper is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o500
        or metadata.st_nlink != 1
    ):
        raise ProvisioningError(
            "mailbox helper must be a single-link root-owned regular file with mode 0500"
        )
    return str(path)


def _validated_created_at(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
    ):
        raise ProvisioningError("bundle created_at must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProvisioningError("bundle created_at must be a UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ProvisioningError("bundle created_at must be a UTC timestamp")
    return value


def validate_bundle_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != BUNDLE_KEYS:
        raise ProvisioningError("bundle has an unexpected schema")
    if (
        type(payload.get("version")) is not int
        or payload.get("version") != BUNDLE_VERSION
    ):
        raise ProvisioningError("bundle version must be 1")
    marker = validate_marker(str(payload.get("marker", "")))
    _validated_created_at(payload.get("created_at"))
    base_email = validate_email(payload.get("email"), label="bundle email")
    validate_password(payload.get("password"), label="bundle password")
    marker_email(base_email, marker)
    helper_value = payload.get("mailbox_helper")
    if not isinstance(helper_value, str):
        raise ProvisioningError("bundle mailbox_helper must be an absolute path")
    validate_mailbox_helper(Path(helper_value))
    accounts = payload.get("roster_accounts")
    if not isinstance(accounts, list) or len(accounts) != ROSTER_SIZE:
        raise ProvisioningError("bundle must contain exactly 13 roster accounts")
    seen_ids: set[str] = set()
    seen_emails: set[str] = set()
    for index, account in enumerate(accounts, start=1):
        if not isinstance(account, dict) or set(account) != ROSTER_ACCOUNT_KEYS:
            raise ProvisioningError("roster account has an unexpected schema")
        raw_id = account.get("id")
        if not isinstance(raw_id, str):
            raise ProvisioningError("roster account id must be a canonical UUID")
        try:
            normalized_id = str(UUID(raw_id))
        except ValueError as exc:
            raise ProvisioningError(
                "roster account id must be a canonical UUID"
            ) from exc
        if raw_id != normalized_id or raw_id in seen_ids:
            raise ProvisioningError("roster account ids must be unique canonical UUIDs")
        email = validate_email(account.get("email"), label=f"roster email {index}")
        if email.count(marker) != 1 or email in seen_emails:
            raise ProvisioningError(
                "roster emails must be unique and contain the marker exactly once"
            )
        validate_password(account.get("password"), label=f"roster password {index}")
        seen_ids.add(raw_id)
        seen_emails.add(email)
    return payload


def load_bundle(path: Path) -> BundleIdentity:
    validate_secret_parent(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProvisioningError("existing bundle is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > MAX_BUNDLE_BYTES
    ):
        raise ProvisioningError(
            "existing bundle must be a root-owned 0600 regular file"
        )
    try:
        payload = validate_bundle_payload(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProvisioningError(
            "existing bundle must contain valid UTF-8 JSON"
        ) from exc
    accounts = payload["roster_accounts"]
    if not isinstance(accounts, list):
        raise ProvisioningError("bundle roster contract is invalid")
    return BundleIdentity(
        marker=str(payload["marker"]),
        user_ids=tuple(str(account["id"]) for account in accounts),
        payload=payload,
        fingerprint=_path_fingerprint(metadata),
    )


def _stage_secret_json(path: Path, payload: dict[str, object]) -> Path:
    encoded = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_BUNDLE_BYTES:
        raise ProvisioningError("generated bundle exceeds 64 KiB")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_secret_json(
    path: Path,
    payload: dict[str, object],
    *,
    expected_existing: BundleIdentity | InventoryIdentity | None,
) -> None:
    temporary = _stage_secret_json(path, payload)
    try:
        if expected_existing is None:
            if path.exists() or path.is_symlink():
                raise ProvisioningError("bundle target appeared during provisioning")
        else:
            try:
                current = path.lstat()
            except OSError as exc:
                raise ProvisioningError(
                    "existing bundle changed during provisioning"
                ) from exc
            if _path_fingerprint(current) != expected_existing.fingerprint:
                raise ProvisioningError("existing bundle changed during provisioning")
        os.replace(temporary, path)
        _fsync_parent(path)
    finally:
        temporary.unlink(missing_ok=True)


def _bundle_timestamp(now: datetime) -> str:
    resolved = now.astimezone(UTC).replace(microsecond=0)
    return resolved.isoformat().replace("+00:00", "Z")


async def _marker_user_ids(db_session: AsyncSession, marker: str) -> set[str]:
    return set(
        str(value)
        for value in (
            await db_session.scalars(
                select(User.id).where(func.lower(User.email).contains(marker.lower()))
            )
        ).all()
    )


async def _marker_tournament_ids(
    db_session: AsyncSession,
    marker: str,
) -> set[str]:
    expected_description = f"Accelerated live browser acceptance {marker}."
    return set(
        str(value)
        for value in (
            await db_session.scalars(
                select(Tournament.id).where(
                    Tournament.description == expected_description
                )
            )
        ).all()
    )


def validate_recovery_state_absent(bundle_path: Path) -> None:
    recovery_paths = (
        bundle_path.with_name(f"{bundle_path.stem}.manual-auth-state.json"),
        bundle_path.with_name(f"{bundle_path.stem}.manual-auth-inventory.json"),
        bundle_path.with_name(
            f"{bundle_path.stem}.manual-auth-abort-inventory.json"
        ),
    )
    if any(path.exists() or path.is_symlink() for path in recovery_paths):
        raise ProvisioningError(
            "refusing to replace a bundle while manual QA recovery state exists"
        )
    try:
        has_automated_recovery = any(
            entry.name.startswith("live-user-qa.")
            for entry in bundle_path.parent.iterdir()
        )
    except OSError as exc:
        raise ProvisioningError("bundle recovery directory could not be inspected") from exc
    if has_automated_recovery:
        raise ProvisioningError(
            "refusing to replace a bundle while automated QA recovery state exists"
        )


async def validate_replacement_scope(
    db_session: AsyncSession,
    *,
    marker: str,
    existing: BundleIdentity | None,
) -> None:
    if await _marker_user_ids(db_session, marker):
        raise ProvisioningError("refusing to reuse a marker that already owns users")
    if await _marker_tournament_ids(db_session, marker):
        raise ProvisioningError("refusing to reuse a marker that already owns tournaments")
    if existing is None:
        return
    if existing.marker == marker:
        raise ProvisioningError("replacement bundle must use a different marker")
    remaining_old_ids = set(
        str(value)
        for value in (
            await db_session.scalars(
                select(User.id).where(User.id.in_(existing.user_ids))
            )
        ).all()
    )
    remaining_old_marker_ids = await _marker_user_ids(db_session, existing.marker)
    remaining_old_tournament_ids = await _marker_tournament_ids(
        db_session,
        existing.marker,
    )
    if remaining_old_ids or remaining_old_marker_ids or remaining_old_tournament_ids:
        raise ProvisioningError(
            "refusing to replace a bundle before its prior exact QA scope is absent"
        )


async def _resolve_player_fixture_contract(
    db_session: AsyncSession,
) -> tuple[RegistrationPayload, list[Role]]:
    registration = RegistrationPayload(
        rank="Phantom",
        subrank=3,
        playtime="1501-2000",
        roles=list(ROLE_OPTIONS),
        pool=[],
        captain_priority="neutral",
    )
    validate_registration_payload(registration)
    if (
        registration.rank not in RANKS
        or registration.playtime not in PLAYTIME_OPTIONS
        or registration.captain_priority not in CAPTAIN_PRIORITY_OPTIONS
    ):
        raise ProvisioningError("internal Deadlock profile fixture is invalid")
    resolved_roles = list(
        (
            await db_session.scalars(
                select(Role).where(Role.slug.in_(REQUIRED_ROLE_SLUGS))
            )
        ).all()
    )
    if {role.slug for role in resolved_roles} != REQUIRED_ROLE_SLUGS:
        raise ProvisioningError("required player roles are missing")
    return registration, resolved_roles


async def create_roster(
    db_session: AsyncSession,
    *,
    marker: str,
    emails: tuple[str, ...],
    passwords: tuple[str, ...],
    now: datetime,
) -> list[dict[str, str]]:
    if len(emails) != ROSTER_SIZE or len(passwords) != ROSTER_SIZE:
        raise ProvisioningError("exactly 13 roster credentials are required")
    registration, resolved_roles = await _resolve_player_fixture_contract(db_session)

    accounts: list[dict[str, str]] = []
    for index, (email, password) in enumerate(
        zip(emails, passwords, strict=True), start=1
    ):
        user = await _create_player_fixture(
            db_session,
            email=email,
            password=password,
            display_name=f"LiveQA R{index:02d}",
            now=now,
            registration=registration,
            resolved_roles=resolved_roles,
            audit_action="platform.liveqa.roster.provision",
            audit_payload={"marker": marker, "roster_index": index},
        )
        accounts.append({"id": user.id, "email": email, "password": password})
    await db_session.flush()
    return accounts


async def _create_player_fixture(
    db_session: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str,
    now: datetime,
    registration: RegistrationPayload,
    resolved_roles: list[Role],
    audit_action: str,
    audit_payload: dict[str, object],
) -> User:
    user = User(
        email=email,
        display_name=display_name,
        status="active",
        email_verified_at=now,
        public_tournament_credits=0,
        private_tournament_credits=0,
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(
        PasswordCredential(
            user_id=user.id,
            password_hash=hash_password(password),
            password_version="argon2id",  # nosec B106 - algorithm identifier, not a password.
        )
    )
    db_session.add(
        PlayerProfile(
            user_id=user.id,
            display_name=user.display_name,
            contact_email=email,
        )
    )
    db_session.add(
        DeadlockProfile(
            user_id=user.id,
            rank=registration.rank,
            subrank=registration.subrank,
            playtime=registration.playtime,
            roles=list(registration.roles),
            pool=list(registration.pool or []),
            captain_priority=registration.captain_priority,
        )
    )
    for role in resolved_roles:
        db_session.add(UserRole(user_id=user.id, role_id=role.id))
    await write_audit_log(
        db_session,
        actor_user_id=None,
        action=audit_action,
        subject_type="user",
        subject_id=user.id,
        payload=audit_payload,
    )
    return user


def validate_runtime_target(
    settings: PlatformSettings,
    *,
    allow_test_environment: bool = False,
) -> None:
    try:
        validate_platform_settings(settings)
    except RuntimeError as exc:
        raise ProvisioningError("platform runtime configuration is unsafe") from exc
    environment = settings.platform_environment.strip().lower()
    if environment == "production":
        if settings.platform_web_origin != EXPECTED_PRODUCTION_ORIGIN:
            raise ProvisioningError(
                "production live QA requires the canonical old-sparky.com origin"
            )
        return
    if allow_test_environment and environment == "test":
        return
    raise ProvisioningError("refusing to provision outside production")


async def provision_bundle(
    *,
    marker: str,
    bundle_path: Path,
    primary_email: str,
    mailbox_helper: Path,
    now: datetime | None = None,
    _allow_test_environment: bool = False,
) -> dict[str, object]:
    marker = validate_marker(marker)
    validate_secret_parent(bundle_path)
    helper = validate_mailbox_helper(mailbox_helper)
    normalized_primary = validate_email(primary_email, label="primary email")
    emails = roster_emails(normalized_primary, marker)
    passwords = generate_unique_passwords(ROSTER_SIZE + 1)
    existing = (
        load_bundle(bundle_path)
        if bundle_path.exists() or bundle_path.is_symlink()
        else None
    )
    validate_recovery_state_absent(bundle_path)
    settings = get_settings()
    validate_runtime_target(
        settings,
        allow_test_environment=_allow_test_environment,
    )
    created_at = (now or datetime.now(UTC)).astimezone(UTC)

    publication_started = False
    async with session_factory()() as db_session:
        try:
            await validate_replacement_scope(
                db_session, marker=marker, existing=existing
            )
            accounts = await create_roster(
                db_session,
                marker=marker,
                emails=emails,
                passwords=passwords[1:],
                now=created_at,
            )
            payload: dict[str, object] = {
                "version": BUNDLE_VERSION,
                "marker": marker,
                "created_at": _bundle_timestamp(created_at),
                "email": normalized_primary,
                "password": passwords[0],
                "mailbox_helper": helper,
                "roster_accounts": accounts,
            }
            validate_bundle_payload(payload)
            publication_started = True
            publish_secret_json(bundle_path, payload, expected_existing=existing)
            await db_session.commit()
            return payload
        except BaseException as exc:
            try:
                await db_session.rollback()
            except BaseException:
                pass
            if publication_started:
                raise ProvisioningError(
                    "bundle publication or database commit outcome is unknown; "
                    "retain the root-only bundle for exact recovery before retrying"
                ) from exc
            raise


def prepare_inventory(*, bundle_path: Path, inventory_path: Path) -> str:
    bundle = load_bundle(bundle_path)
    validate_secret_parent(inventory_path)
    payload: dict[str, object] = {
        "version": 1,
        "marker": bundle.marker,
        "user_ids": list(bundle.user_ids),
        "tournament_ids": [],
        "media_ids": [],
    }
    publish_secret_json(inventory_path, payload, expected_existing=None)
    return bundle.marker


def _load_preseeded_inventory(
    path: Path,
    *,
    bundle: BundleIdentity,
) -> InventoryIdentity:
    validate_secret_parent(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProvisioningError("preseeded inventory is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > MAX_BUNDLE_BYTES
    ):
        raise ProvisioningError(
            "preseeded inventory must be a root-owned 0600 regular file"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProvisioningError("preseeded inventory must contain valid JSON") from exc
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"version", "marker", "user_ids", "tournament_ids", "media_ids"}
        or payload.get("version") != 1
        or payload.get("marker") != bundle.marker
        or not isinstance(payload.get("user_ids"), list)
        or payload.get("tournament_ids") != []
        or payload.get("media_ids") != []
    ):
        raise ProvisioningError("preseeded inventory has an unexpected schema")
    raw_user_ids = payload["user_ids"]
    if not isinstance(raw_user_ids, list):
        raise ProvisioningError("preseeded inventory user contract is invalid")
    if (
        any(not isinstance(value, str) for value in raw_user_ids)
        or len(raw_user_ids) != ROSTER_SIZE
        or len(set(raw_user_ids)) != ROSTER_SIZE
        or set(raw_user_ids) != set(bundle.user_ids)
    ):
        raise ProvisioningError(
            "preseeded inventory must exactly match the bundle roster"
        )
    return InventoryIdentity(
        marker=bundle.marker,
        user_ids=tuple(raw_user_ids),
        payload=payload,
        fingerprint=_path_fingerprint(metadata),
    )


def _automation_player_email(bundle: BundleIdentity) -> str:
    base_email = validate_email(bundle.payload["email"], label="bundle email")
    domain = base_email.rsplit("@", 1)[1]
    return validate_email(
        f"workflow+{bundle.marker}@{domain}",
        label="workflow player email",
    )


async def prepare_browser_sessions(
    *,
    bundle_path: Path,
    inventory_path: Path,
    sessions_path: Path,
    now: datetime | None = None,
    _allow_test_environment: bool = False,
) -> str:
    bundle = load_bundle(bundle_path)
    inventory = _load_preseeded_inventory(inventory_path, bundle=bundle)
    validate_secret_parent(sessions_path)
    if sessions_path.exists() or sessions_path.is_symlink():
        raise ProvisioningError("browser session target must not already exist")
    settings = get_settings()
    validate_runtime_target(
        settings,
        allow_test_environment=_allow_test_environment,
    )
    if (
        settings.platform_environment.strip().lower() == "production"
        and (
            not settings.platform_cookie_secure
            or not settings.platform_session_cookie_name.startswith("__Host-")
        )
    ):
        raise ProvisioningError("production browser session cookie settings are unsafe")

    issued_at = (now or datetime.now(UTC)).astimezone(UTC)
    expires_at = issued_at + LIVE_BROWSER_SESSION_TTL
    automation_email = _automation_player_email(bundle)
    publication_started = False
    async with session_factory()() as db_session:
        try:
            roster_users = list(
                (
                    await db_session.scalars(
                        select(User).where(User.id.in_(bundle.user_ids))
                    )
                ).all()
            )
            users_by_id = {str(user.id): user for user in roster_users}
            accounts = bundle.payload["roster_accounts"]
            if not isinstance(accounts, list):
                raise ProvisioningError("bundle roster contract is invalid")
            expected_email_by_id = {
                str(account["id"]): str(account["email"])
                for account in accounts
            }
            if (
                set(users_by_id) != set(bundle.user_ids)
                or any(
                    user.status != "active"
                    or user.email_verified_at is None
                    or user.email != expected_email_by_id[str(user.id)]
                    for user in roster_users
                )
            ):
                raise ProvisioningError(
                    "bundle roster users do not exactly match active verified accounts"
                )
            existing_sessions = await db_session.scalar(
                select(func.count(UserSession.id)).where(
                    UserSession.user_id.in_(bundle.user_ids),
                    UserSession.invalidated_at.is_(None),
                    UserSession.expires_at > issued_at,
                )
            )
            if int(existing_sessions or 0) != 0:
                raise ProvisioningError(
                    "bundle roster already has active sessions; cleanup before retrying"
                )
            if await db_session.scalar(
                select(User.id).where(func.lower(User.email) == automation_email)
            ):
                raise ProvisioningError(
                    "workflow player already exists; exact cleanup is required"
                )

            registration, resolved_roles = await _resolve_player_fixture_contract(
                db_session
            )
            automation_password = generate_unique_passwords(1)[0]
            automation_user = await _create_player_fixture(
                db_session,
                email=automation_email,
                password=automation_password,
                display_name="LiveQA Player",
                now=issued_at,
                registration=registration,
                resolved_roles=resolved_roles,
                audit_action="platform.liveqa.workflow-player.provision",
                audit_payload={"marker": bundle.marker},
            )
            await db_session.flush()

            roster_session_rows: list[tuple[str, str, UserSession]] = []
            for user_id in bundle.user_ids:
                token = new_session_token()
                session_row = UserSession(
                    user_id=user_id,
                    token_digest=session_token_digest(token),
                    ip_address="127.0.0.1",
                    user_agent=f"platform-live-csp-qa:{bundle.marker}",
                    expires_at=expires_at,
                )
                db_session.add(session_row)
                roster_session_rows.append((user_id, token, session_row))
            automation_token = new_session_token()
            automation_session = UserSession(
                user_id=automation_user.id,
                token_digest=session_token_digest(automation_token),
                ip_address="127.0.0.1",
                user_agent=f"platform-live-csp-qa:{bundle.marker}",
                expires_at=expires_at,
            )
            db_session.add(automation_session)
            await db_session.flush()

            sessions_payload: dict[str, object] = {
                "version": 1,
                "marker": bundle.marker,
                "cookie_name": settings.platform_session_cookie_name,
                "created_at": _bundle_timestamp(issued_at),
                "expires_at": _bundle_timestamp(expires_at),
                "roster_sessions": [
                    {
                        "user_id": user_id,
                        "session_id": session_row.id,
                        "token": token,
                    }
                    for user_id, token, session_row in roster_session_rows
                ],
                "workflow_player": {
                    "user_id": automation_user.id,
                    "session_id": automation_session.id,
                    "token": automation_token,
                },
            }
            updated_inventory = dict(inventory.payload)
            updated_inventory["user_ids"] = [
                *inventory.user_ids,
                automation_user.id,
            ]
            publication_started = True
            publish_secret_json(
                inventory_path,
                updated_inventory,
                expected_existing=inventory,
            )
            publish_secret_json(
                sessions_path,
                sessions_payload,
                expected_existing=None,
            )
            await db_session.commit()
            return bundle.marker
        except BaseException as exc:
            try:
                await db_session.rollback()
            except BaseException:
                pass
            if publication_started:
                raise ProvisioningError(
                    "browser session publication or database commit outcome is unknown; "
                    "retain the root-only inventory and session file for exact recovery"
                ) from exc
            raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision exact root-only live CSP QA users."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    provision = subparsers.add_parser("provision")
    provision.add_argument("--marker", required=True)
    provision.add_argument("--bundle-path", required=True, type=Path)
    provision.add_argument("--primary-email", required=True)
    provision.add_argument("--mailbox-helper", required=True, type=Path)
    prepare = subparsers.add_parser("prepare-inventory")
    prepare.add_argument("--bundle-path", required=True, type=Path)
    prepare.add_argument("--inventory-path", required=True, type=Path)
    browser_sessions = subparsers.add_parser("prepare-browser-sessions")
    browser_sessions.add_argument("--bundle-path", required=True, type=Path)
    browser_sessions.add_argument("--inventory-path", required=True, type=Path)
    browser_sessions.add_argument("--sessions-path", required=True, type=Path)
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        raise ProvisioningError("live CSP QA provisioning must run as root")
    if args.command == "prepare-inventory":
        marker = prepare_inventory(
            bundle_path=args.bundle_path,
            inventory_path=args.inventory_path,
        )
        print(marker)
        return 0
    if args.command == "prepare-browser-sessions":
        marker = await prepare_browser_sessions(
            bundle_path=args.bundle_path,
            inventory_path=args.inventory_path,
            sessions_path=args.sessions_path,
        )
        print(marker)
        return 0
    payload = await provision_bundle(
        marker=args.marker,
        bundle_path=args.bundle_path,
        primary_email=args.primary_email,
        mailbox_helper=args.mailbox_helper,
    )
    print(
        "Live CSP QA bundle provisioned: "
        f"marker={payload['marker']} roster_accounts={ROSTER_SIZE} bundle={args.bundle_path}"
    )
    return 0


def main() -> int:
    args = parse_args()

    async def run() -> int:
        try:
            return await async_main(args)
        finally:
            await dispose_engine()

    try:
        return asyncio.run(run())
    except ProvisioningError as exc:
        print(f"Live CSP QA provisioning refused: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print(
            "Live CSP QA provisioning failed without publishing credentials.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
