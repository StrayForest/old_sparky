from __future__ import annotations

from python_packages.platform_infra.models import Tournament


def tournament_state_version(
    tournament: Tournament,
    *,
    participant_count: int = 0,
) -> int:
    """Return the shared optimistic version used by tournament mutations/reads."""

    updated_at = tournament.updated_at or tournament.created_at
    updated_ms = int(updated_at.timestamp() * 1000) if updated_at is not None else 0
    return (
        updated_ms
        + int(tournament.bracket_revision or 0) * 1_000_000
        + max(0, int(participant_count))
    )
