from __future__ import annotations
from dataclasses import dataclass

from python_packages.platform_domain.deadlock.constants import (
    CAPTAIN_PRIORITY_OPTIONS,
    PLAYTIME_OPTIONS,
    RANKS,
    ROLE_OPTIONS,
)


class RegistrationValidationError(ValueError):
    """Raised when the registration payload breaks Old Sparky Arena rules."""


@dataclass(frozen=True, slots=True)
class RegistrationPayload:
    rank: str
    subrank: int
    playtime: str
    roles: list[str]
    pool: list[str] | None = None
    captain_priority: str | None = None


def normalize_pool(pool: list[str] | None) -> list[str]:
    if not pool:
        return []
    unique_items = {
        "The Doorman" if hero.strip().lower() == "doorman" else hero.strip()
        for hero in pool
        if hero and hero.strip()
    }
    return sorted(unique_items, key=lambda value: value.lower())


def normalize_roles(roles: list[str] | None) -> list[str]:
    if not roles:
        return []
    return [role for role in ROLE_OPTIONS if role in roles]


def validate_registration_payload(payload: RegistrationPayload) -> None:
    if payload.rank not in RANKS:
        raise RegistrationValidationError("Rank is missing or invalid.")
    if not 1 <= int(payload.subrank) <= 6:
        raise RegistrationValidationError("Subrank is missing or invalid.")
    if payload.playtime not in PLAYTIME_OPTIONS:
        raise RegistrationValidationError("Playtime is missing or invalid.")
    if not payload.roles or any(role not in ROLE_OPTIONS for role in payload.roles):
        raise RegistrationValidationError("Roles are missing or invalid.")
    if payload.captain_priority is not None and payload.captain_priority not in CAPTAIN_PRIORITY_OPTIONS:
        raise RegistrationValidationError("Captain priority is missing or invalid.")
