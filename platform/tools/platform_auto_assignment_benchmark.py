#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
import tracemalloc
from dataclasses import dataclass
from typing import Any

from python_packages.platform_domain.deadlock import AutoAssignmentEngine
from python_packages.platform_domain.deadlock.captain_round import TEAM_COUNT_CHOICES
from python_packages.platform_domain.deadlock.constants import (
    PLAYTIME_OPTIONS,
    POOL_LIST,
    RANKS,
    ROLE_OPTIONS,
)

READY_PLAYERS_PER_TEAM = 6
DEFAULT_TEAM_COUNTS = (2, 4, 8, 16)
EXPECTED_SNAPSHOT_DIGESTS = {
    2: "aac8e48cc451a749362191d6d846f9e97a8fedc149312dfa236b200d53ea5005",
    4: "f3ea4c8428eee173683a8b84f985219507d17d1e9d711c1654f2927578b1273a",
    8: "5cb1e40f840a893685c80c89c53788dd2964f97cfe8f2d620b0d36af96e3ab6f",
    16: "f0c81d3a8b0a64a5ec3b40207f23fc4b2c4b24f88c7feb64fdc2d789cb2e080d",
    32: "bfd5f52d0e821fa50fc31935cd96ccac85589147b0b154627517f29ff1c7d81b",
    64: "aa38e401450e5c227a910758020ee668a66e9c726d87087e0051ec6f4eec765a",
    128: "96ee5576388758b44f586413ffef4a211b73f406577d899d9f0581824a3c6067",
}


@dataclass(frozen=True, slots=True)
class BenchmarkFixture:
    captain_rows: tuple[dict[str, Any], ...]
    ready_player_rows: tuple[dict[str, Any], ...]
    dream_slot_rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    teams: int
    ready_players: int
    iterations: int
    wall_min_ms: float
    wall_median_ms: float
    wall_max_ms: float
    cpu_median_ms: float
    traced_peak_mib: float | None
    snapshot_digest: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark deterministic Deadlock auto-assignment with generated "
            "fixtures. The command does not connect to PostgreSQL or Redis."
        )
    )
    parser.add_argument(
        "--teams",
        type=int,
        nargs="+",
        default=list(DEFAULT_TEAM_COUNTS),
        help="Supported team counts to benchmark (default: 2 4 8 16).",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Measured solver runs per team count (default: 3).",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="Unmeasured warmup runs per team count (default: 1).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a table.",
    )
    parser.add_argument(
        "--skip-memory",
        action="store_true",
        help="Skip the extra tracemalloc solver run for expensive scenarios.",
    )
    args = parser.parse_args()
    unsupported = sorted(set(args.teams).difference(TEAM_COUNT_CHOICES))
    if unsupported:
        parser.error(
            "--teams contains unsupported values: "
            + ", ".join(str(value) for value in unsupported)
        )
    if args.iterations < 1 or args.iterations > 100:
        parser.error("--iterations must be between 1 and 100")
    if args.warmup < 0 or args.warmup > 20:
        parser.error("--warmup must be between 0 and 20")
    args.teams = sorted(set(args.teams))
    return args


def build_fixture(teams_count: int) -> BenchmarkFixture:
    if teams_count not in TEAM_COUNT_CHOICES:
        raise ValueError(f"Unsupported team count: {teams_count}")

    captain_rows: list[dict[str, Any]] = []
    ready_player_rows: list[dict[str, Any]] = []
    dream_slot_rows: list[dict[str, Any]] = []

    for team_index in range(teams_count):
        team_id = str(team_index + 1)
        captain_rows.append(
            _player_row(
                user_id=f"captain-{team_id}",
                player_index=team_index * READY_PLAYERS_PER_TEAM,
                team_id=team_id,
            )
        )
        for slot_number in range(1, READY_PLAYERS_PER_TEAM + 1):
            role_index = (team_index + slot_number - 1) % len(ROLE_OPTIONS)
            hero_index = (team_index * READY_PLAYERS_PER_TEAM + slot_number) % len(POOL_LIST)
            dream_slot_rows.append(
                {
                    "team_id": team_id,
                    "slot_number": slot_number,
                    "allowed_roles": [ROLE_OPTIONS[role_index]],
                    "desired_heroes": [
                        POOL_LIST[hero_index],
                        POOL_LIST[(hero_index + teams_count + 1) % len(POOL_LIST)],
                    ],
                }
            )

    ready_count = teams_count * READY_PLAYERS_PER_TEAM
    for player_index in range(ready_count):
        ready_player_rows.append(
            _player_row(
                user_id=f"player-{player_index + 1}",
                player_index=player_index,
            )
        )

    return BenchmarkFixture(
        captain_rows=tuple(captain_rows),
        ready_player_rows=tuple(ready_player_rows),
        dream_slot_rows=tuple(dream_slot_rows),
    )


def _player_row(
    *,
    user_id: str,
    player_index: int,
    team_id: str | None = None,
) -> dict[str, Any]:
    role_index = player_index % len(ROLE_OPTIONS)
    hero_index = (player_index * 3) % len(POOL_LIST)
    row: dict[str, Any] = {
        "user_id": user_id,
        "username": user_id,
        "rank": RANKS[player_index % len(RANKS)],
        "subrank": (player_index % 6) + 1,
        "playtime": PLAYTIME_OPTIONS[player_index % len(PLAYTIME_OPTIONS)],
        "pool": [
            POOL_LIST[hero_index],
            POOL_LIST[(hero_index + 7) % len(POOL_LIST)],
            POOL_LIST[(hero_index + 17) % len(POOL_LIST)],
        ],
        "roles": [
            ROLE_OPTIONS[role_index],
            ROLE_OPTIONS[(role_index + 1) % len(ROLE_OPTIONS)],
        ],
    }
    if team_id is not None:
        row["team_id"] = team_id
    return row


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(
        snapshot,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def solve_fixture(fixture: BenchmarkFixture) -> tuple[str, dict[str, Any]]:
    run = AutoAssignmentEngine().solve(
        fixture.captain_rows,
        fixture.ready_player_rows,
        fixture.dream_slot_rows,
    )
    return snapshot_digest(run.result_snapshot), run.result_snapshot


def benchmark_fixture(
    teams_count: int,
    *,
    iterations: int,
    warmup: int,
    measure_memory: bool = True,
) -> BenchmarkResult:
    fixture = build_fixture(teams_count)
    expected_digest: str | None = EXPECTED_SNAPSHOT_DIGESTS.get(teams_count)

    for _ in range(warmup):
        digest, _ = solve_fixture(fixture)
        expected_digest = _assert_digest(expected_digest, digest, teams_count)

    wall_times_ms: list[float] = []
    cpu_times_ms: list[float] = []
    for _ in range(iterations):
        wall_start = time.perf_counter_ns()
        cpu_start = time.process_time_ns()
        digest, _ = solve_fixture(fixture)
        cpu_times_ms.append((time.process_time_ns() - cpu_start) / 1_000_000)
        wall_times_ms.append((time.perf_counter_ns() - wall_start) / 1_000_000)
        expected_digest = _assert_digest(expected_digest, digest, teams_count)

    traced_peak_mib: float | None = None
    if measure_memory:
        tracemalloc.start()
        digest, _ = solve_fixture(fixture)
        _, traced_peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        expected_digest = _assert_digest(expected_digest, digest, teams_count)
        traced_peak_mib = traced_peak_bytes / 1024 / 1024

    return BenchmarkResult(
        teams=teams_count,
        ready_players=len(fixture.ready_player_rows),
        iterations=iterations,
        wall_min_ms=min(wall_times_ms),
        wall_median_ms=statistics.median(wall_times_ms),
        wall_max_ms=max(wall_times_ms),
        cpu_median_ms=statistics.median(cpu_times_ms),
        traced_peak_mib=traced_peak_mib,
        snapshot_digest=expected_digest or "",
    )


def _assert_digest(
    expected_digest: str | None,
    actual_digest: str,
    teams_count: int,
) -> str:
    if expected_digest is not None and actual_digest != expected_digest:
        raise RuntimeError(
            f"Non-deterministic result for {teams_count} teams: "
            f"{expected_digest} != {actual_digest}"
        )
    return actual_digest


def print_results(results: list[BenchmarkResult], *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "teams": result.teams,
                        "ready_players": result.ready_players,
                        "iterations": result.iterations,
                        "wall_min_ms": round(result.wall_min_ms, 3),
                        "wall_median_ms": round(result.wall_median_ms, 3),
                        "wall_max_ms": round(result.wall_max_ms, 3),
                        "cpu_median_ms": round(result.cpu_median_ms, 3),
                        "traced_peak_mib": (
                            round(result.traced_peak_mib, 3)
                            if result.traced_peak_mib is not None
                            else None
                        ),
                        "snapshot_digest": result.snapshot_digest,
                    }
                    for result in results
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return

    headers = (
        "teams",
        "ready",
        "runs",
        "wall min",
        "wall median",
        "wall max",
        "cpu median",
        "peak MiB",
        "digest",
    )
    rows = [
        (
            str(result.teams),
            str(result.ready_players),
            str(result.iterations),
            f"{result.wall_min_ms:.1f} ms",
            f"{result.wall_median_ms:.1f} ms",
            f"{result.wall_max_ms:.1f} ms",
            f"{result.cpu_median_ms:.1f} ms",
            (
                f"{result.traced_peak_mib:.1f}"
                if result.traced_peak_mib is not None
                else "-"
            ),
            result.snapshot_digest[:12],
        )
        for result in results
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    print("  ".join(value.ljust(widths[index]) for index, value in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def main() -> None:
    args = parse_args()
    results = [
        benchmark_fixture(
            teams_count,
            iterations=args.iterations,
            warmup=args.warmup,
            measure_memory=not args.skip_memory,
        )
        for teams_count in args.teams
    ]
    print_results(results, as_json=args.json)


if __name__ == "__main__":
    main()
