from __future__ import annotations

from collections.abc import Callable


_runtime_cache_invalidator: Callable[[str], None] | None = None


def register_tournament_runtime_cache_invalidator(
    callback: Callable[[str], None],
) -> None:
    global _runtime_cache_invalidator
    _runtime_cache_invalidator = callback


def invalidate_tournament_runtime_caches(tournament_id: str) -> None:
    if _runtime_cache_invalidator is not None:
        _runtime_cache_invalidator(tournament_id)
