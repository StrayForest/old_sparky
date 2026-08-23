from __future__ import annotations

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
PATH = "platform/apps/platform_api/app/api/routes/tournaments.py"


def baseline() -> str:
    return subprocess.check_output(
        ["git", "show", f"origin/dev:{PATH}"],
        cwd=ROOT,
        text=True,
    )


def route_block(text: str, function_name: str, next_function_name: str) -> tuple[int, int, str]:
    function_marker = f"async def {function_name}("
    function_index = text.index(function_marker)
    decorator_index = text.rfind("\n@router.", 0, function_index)
    if decorator_index < 0:
        raise RuntimeError(f"Decorator for {function_name} not found")
    start = decorator_index + 1

    next_marker = f"async def {next_function_name}("
    next_function_index = text.index(next_marker, function_index + len(function_marker))
    next_decorator_index = text.rfind("\n@router.", function_index, next_function_index)
    if next_decorator_index < 0:
        raise RuntimeError(f"Next decorator after {function_name} not found")
    end = next_decorator_index + 1
    return start, end, text[start:end]


def hide_from_schema(block: str, route: str) -> str:
    old = f'@router.post("{route}", response_model=TournamentDeadlockCaptainRoundResponse)'
    new = (
        "@router.post(\n"
        f'    "{route}",\n'
        "    response_model=TournamentDeadlockCaptainRoundResponse,\n"
        "    include_in_schema=False,\n"
        ")"
    )
    if old not in block:
        raise RuntimeError(f"Expected baseline decorator for {route} not found")
    return block.replace(old, new, 1)


base = baseline()
current_path = ROOT / PATH
current = current_path.read_text(encoding="utf-8")

specs = (
    (
        "respond_deadlock_captain_round",
        "close_deadlock_captain_round",
        "/{slug}/deadlock/captain-round/respond",
    ),
    (
        "close_deadlock_captain_round",
        "finalize_deadlock_captain_round",
        "/{slug}/deadlock/captain-round/close",
    ),
    (
        "finalize_deadlock_captain_round",
        "get_deadlock_auto_assignment_state",
        "/{slug}/deadlock/captain-round/finalize",
    ),
)

for function_name, next_function_name, route in specs:
    _, _, base_block = route_block(base, function_name, next_function_name)
    replacement = hide_from_schema(base_block, route)
    start, end, _ = route_block(current, function_name, next_function_name)
    current = current[:start] + replacement + current[end:]

current_path.write_text(current, encoding="utf-8")
