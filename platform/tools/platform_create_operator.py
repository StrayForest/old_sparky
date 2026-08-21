#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from getpass import getpass
import os
from pathlib import Path
import stat
import sys

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from pydantic import EmailStr, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    PasswordCredential,
    PlayerProfile,
    Role,
    User,
    UserRole,
)
from python_packages.platform_infra.security import hash_password, invalidate_user_sessions


OPERATOR_ROLE_SLUGS = frozenset({"authenticated_user", "player", "admin", "superadmin"})
EMAIL_ADAPTER = TypeAdapter(EmailStr)


@dataclass(frozen=True, slots=True)
class OperatorBootstrapResult:
    user_id: str
    email: str
    created: bool
    added_roles: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or idempotently bootstrap a local platform operator account."
    )
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--confirm-email",
        required=True,
        help="Must match --email after whitespace/case normalization.",
    )
    parser.add_argument(
        "--display-name",
        default=None,
        help="Required for a new account; existing display names are preserved when omitted.",
    )
    parser.add_argument(
        "--activate-existing",
        action="store_true",
        help="Explicitly activate and verify an existing non-active account.",
    )
    parser.add_argument(
        "--password-file",
        type=Path,
        default=None,
        help="0600 file containing the initial password; otherwise prompt securely for new users.",
    )
    parser.add_argument("--env-file", type=Path, default=None)
    return parser.parse_args()


def load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'").strip('"')


def normalize_confirmed_email(email: str, confirmation: str) -> str:
    normalized_email = str(EMAIL_ADAPTER.validate_python(email.strip())).lower()
    normalized_confirmation = str(EMAIL_ADAPTER.validate_python(confirmation.strip())).lower()
    if normalized_email != normalized_confirmation:
        raise ValueError("--confirm-email must match --email.")
    return normalized_email


def validate_display_name(display_name: str) -> str:
    normalized = display_name.strip()
    if not 2 <= len(normalized) <= 15:
        raise ValueError("--display-name must contain between 2 and 15 characters.")
    return normalized


def validate_password(password: str) -> str:
    if not 10 <= len(password) <= 128:
        raise ValueError("The initial password must contain between 10 and 128 characters.")
    return password


def read_initial_password(password_file: Path | None) -> str:
    if password_file is not None:
        permissions = stat.S_IMODE(password_file.stat().st_mode)
        if permissions & 0o077:
            raise ValueError("--password-file must not be accessible by group or other users.")
        lines = password_file.read_text(encoding="utf-8").splitlines()
        if len(lines) != 1:
            raise ValueError("--password-file must contain exactly one line.")
        return validate_password(lines[0])

    password = getpass("Initial operator password: ")
    confirmation = getpass("Confirm initial operator password: ")
    if password != confirmation:
        raise ValueError("Password confirmation does not match.")
    return validate_password(password)


async def bootstrap_operator(
    db_session: AsyncSession,
    *,
    email: str,
    display_name: str | None,
    initial_password: str | None,
    activate_existing: bool = False,
) -> OperatorBootstrapResult:
    user = await db_session.scalar(select(User).where(User.email == email).with_for_update())
    created = user is None
    verification_changed = False
    status_changed = False
    if user is None:
        if initial_password is None:
            raise ValueError("An initial password is required to create the operator account.")
        if display_name is None:
            raise ValueError("--display-name is required to create the operator account.")
        user = User(
            email=email,
            display_name=display_name,
            status="active",
            email_verified_at=datetime.now(UTC),
            public_tournament_credits=0,
            private_tournament_credits=0,
        )
        db_session.add(user)
        await db_session.flush()
        db_session.add(
            PasswordCredential(
                user_id=user.id,
                password_hash=hash_password(validate_password(initial_password)),
                password_version="argon2id",
            )
        )
        db_session.add(
            PlayerProfile(
                user_id=user.id,
                display_name=display_name,
                contact_email=email,
            )
        )
    else:
        if user.status != "active":
            if not activate_existing:
                raise ValueError(
                    "Refusing to grant operator roles to a non-active account without "
                    "--activate-existing."
                )
            user.status = "active"
            status_changed = True
        verification_changed = user.email_verified_at is None
        if verification_changed:
            user.email_verified_at = datetime.now(UTC)
        if display_name is not None:
            user.display_name = display_name
        credential = await db_session.get(PasswordCredential, user.id)
        if credential is None:
            raise ValueError("The existing account has no password credential.")
        profile = await db_session.get(PlayerProfile, user.id)
        if profile is None:
            db_session.add(
                PlayerProfile(
                    user_id=user.id,
                    display_name=display_name or user.display_name,
                    contact_email=email,
                )
            )
        else:
            if display_name is not None:
                profile.display_name = display_name
            profile.contact_email = email

    roles = list(
        (
            await db_session.scalars(select(Role).where(Role.slug.in_(OPERATOR_ROLE_SLUGS)))
        ).all()
    )
    if {role.slug for role in roles} != OPERATOR_ROLE_SLUGS:
        raise RuntimeError("One or more required operator roles are missing.")

    added_roles: list[str] = []
    for role in roles:
        existing_role = await db_session.get(
            UserRole,
            {"user_id": user.id, "role_id": role.id},
        )
        if existing_role is None:
            db_session.add(UserRole(user_id=user.id, role_id=role.id))
            added_roles.append(role.slug)

    invalidated_sessions = 0
    if not created and (added_roles or verification_changed or status_changed):
        invalidated_sessions = await invalidate_user_sessions(
            db_session,
            user_id=user.id,
        )

    await write_audit_log(
        db_session,
        actor_user_id=user.id,
        action="platform.operator.bootstrap",
        subject_type="user",
        subject_id=user.id,
        payload={
            "email": email,
            "created": created,
            "roles": sorted(OPERATOR_ROLE_SLUGS),
            "added_roles": sorted(added_roles),
            "email_verified": user.email_verified_at is not None,
            "status_activated": status_changed,
            "sessions_invalidated": invalidated_sessions,
        },
    )
    return OperatorBootstrapResult(
        user_id=user.id,
        email=email,
        created=created,
        added_roles=tuple(sorted(added_roles)),
    )


async def main() -> int:
    args = parse_args()
    if args.env_file is not None:
        load_env_file(args.env_file)
    try:
        email = normalize_confirmed_email(args.email, args.confirm_email)
        display_name = (
            validate_display_name(args.display_name)
            if args.display_name is not None
            else None
        )
        async with session_factory()() as db_session:
            existing_user_id = await db_session.scalar(select(User.id).where(User.email == email))
            initial_password = (
                read_initial_password(args.password_file) if existing_user_id is None else None
            )
            result = await bootstrap_operator(
                db_session,
                email=email,
                display_name=display_name,
                initial_password=initial_password,
                activate_existing=args.activate_existing,
            )
            await db_session.commit()
        state = "created" if result.created else "verified"
        print(
            f"platform-operator: {state} {result.email}; "
            f"added roles: {', '.join(result.added_roles) if result.added_roles else 'none'}"
        )
        return 0
    except (OSError, ValueError) as exc:
        print(f"platform-operator: {exc}", file=sys.stderr)
        return 2
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
