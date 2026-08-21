from __future__ import annotations

from collections.abc import Mapping

from python_packages.platform_domain.deadlock.constants import BASE, RANK_POWER

PLAYTIME_BONUS = {
    "0-500": 0.00,
    "501-1000": 0.01,
    "1001-1500": 0.02,
    "1501-2000": 0.03,
    "2001-3000": 0.04,
    "3000+": 0.05,
}


def get_playtime_bonus(playtime: str | None) -> float:
    if playtime is None:
        return 0.0
    return PLAYTIME_BONUS.get(str(playtime).strip(), 0.0)


def calculate_player_strength(
    rank: str,
    subrank: int | None,
    playtime: str | None,
    rank_power: Mapping[str, int] | None = None,
) -> float:
    power_map = rank_power or RANK_POWER
    index = power_map.get(rank, 0)
    normalized_subrank = int(subrank or 0)
    return BASE ** (index + normalized_subrank / 6 + get_playtime_bonus(playtime))
