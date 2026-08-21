from __future__ import annotations

import hashlib


TEAM_NAME_ADJECTIVES = (
    "Алые",
    "Синие",
    "Белые",
    "Черные",
    "Серые",
    "Рыжие",
    "Желтые",
    "Медные",
    "Лунные",
    "Дикие",
    "Тихие",
    "Быстрые",
    "Хитрые",
    "Смелые",
    "Грозные",
    "Ночные",
)

TEAM_NAME_MASCOTS = (
    "Быки",
    "Волки",
    "Лисы",
    "Коты",
    "Рыси",
    "Тигры",
    "Львы",
    "Орлы",
    "Совы",
    "Вороны",
    "Акулы",
    "Кобры",
    "Бизоны",
    "Кабаны",
    "Барсы",
    "Ястребы",
)

TEAM_NAME_CATALOG = tuple(
    f"{adjective} {mascot}"
    for adjective in TEAM_NAME_ADJECTIVES
    for mascot in TEAM_NAME_MASCOTS
)


def choose_available_team_name(seed: str, unavailable_names: set[str]) -> str:
    """Choose a stable pseudo-random catalog name without a casefold collision."""

    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    start_index = int.from_bytes(digest[:4], byteorder="big") % len(TEAM_NAME_CATALOG)
    for offset in range(len(TEAM_NAME_CATALOG)):
        candidate = TEAM_NAME_CATALOG[(start_index + offset) % len(TEAM_NAME_CATALOG)]
        if candidate.casefold() not in unavailable_names:
            return candidate
    raise ValueError("No free generated team name remains.")
