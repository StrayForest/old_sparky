from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from python_packages.platform_domain.deadlock.constants import POOL_LIST, ROLE_OPTIONS

class DreamSlotEditorError(ValueError):
    """Raised when a dream-slot payload is invalid."""


def build_default_slot_payload(slot_number: int) -> dict[str, Any]:
    return {
        "team_id": None,
        "slot_number": int(slot_number),
        "allowed_roles": [],
        "desired_heroes": [],
    }


def normalize_slot_payload(
    slot: Mapping[str, Any] | "DreamSlot" | None,
    *,
    default_slot_number: int = 1,
) -> dict[str, Any]:
    if slot is None:
        return build_default_slot_payload(default_slot_number)

    if isinstance(slot, DreamSlot):
        return slot.to_payload()

    data = dict(slot)
    slot_number = int(data.get("slot_number") or default_slot_number)
    normalized = build_default_slot_payload(slot_number)
    normalized["team_id"] = data.get("team_id")

    allowed_roles: list[str] = []
    for role in data.get("allowed_roles") or []:
        if role in ROLE_OPTIONS and role not in allowed_roles:
            allowed_roles.append(role)
    normalized["allowed_roles"] = allowed_roles
    normalized["desired_heroes"] = list(data.get("desired_heroes") or [])
    return normalized


def validate_dream_slot_payload(
    slot: Mapping[str, Any] | "DreamSlot" | None,
    *,
    default_slot_number: int = 1,
    supported_heroes: Collection[str] = POOL_LIST,
) -> dict[str, Any]:
    normalized = normalize_slot_payload(slot, default_slot_number=default_slot_number)
    normalized["allowed_roles"] = [role for role in ROLE_OPTIONS if role in normalized["allowed_roles"]]
    desired_heroes = [hero for hero in normalized["desired_heroes"] if hero in supported_heroes]
    if len(desired_heroes) != len(normalized["desired_heroes"]):
        raise DreamSlotEditorError("Unknown hero in dream-slot payload.")
    if len(desired_heroes) > 5:
        raise DreamSlotEditorError("No more than 5 desired heroes can be selected.")
    normalized["desired_heroes"] = desired_heroes
    return normalized


@dataclass(frozen=True, slots=True)
class DreamSlot:
    team_id: str | None
    slot_number: int
    allowed_roles: tuple[str, ...] = ()
    desired_heroes: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        slot: Mapping[str, Any] | "DreamSlot" | None,
        *,
        default_slot_number: int = 1,
    ) -> "DreamSlot":
        normalized = normalize_slot_payload(slot, default_slot_number=default_slot_number)
        return cls(
            team_id=str(normalized["team_id"]) if normalized["team_id"] is not None else None,
            slot_number=int(normalized["slot_number"]),
            allowed_roles=tuple(normalized["allowed_roles"]),
            desired_heroes=tuple(normalized["desired_heroes"]),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "slot_number": self.slot_number,
            "allowed_roles": list(self.allowed_roles),
            "desired_heroes": list(self.desired_heroes),
        }


def expand_dream_slot_payloads(
    team_ids: Sequence[str],
    slot_rows: Sequence[Mapping[str, Any] | DreamSlot],
    *,
    total_slots: int = 6,
) -> dict[str, tuple[DreamSlot, ...]]:
    if total_slots < 1:
        raise ValueError("total_slots must be greater than 0.")

    rows_by_team: dict[str, dict[int, DreamSlot]] = {str(team_id): {} for team_id in team_ids}
    for row in slot_rows:
        slot = DreamSlot.from_mapping(row)
        if slot.team_id is None:
            continue
        team_id = str(slot.team_id)
        if team_id not in rows_by_team:
            continue
        rows_by_team[team_id][slot.slot_number] = slot

    expanded: dict[str, tuple[DreamSlot, ...]] = {}
    for team_id in team_ids:
        normalized_team_id = str(team_id)
        slots: list[DreamSlot] = []
        existing_slots = rows_by_team.get(normalized_team_id, {})
        for slot_number in range(1, total_slots + 1):
            slots.append(
                existing_slots.get(
                    slot_number,
                    DreamSlot(
                        team_id=normalized_team_id,
                        slot_number=slot_number,
                        allowed_roles=tuple(ROLE_OPTIONS),
                        desired_heroes=(),
                    ),
                )
            )
        expanded[normalized_team_id] = tuple(slots)
    return expanded
