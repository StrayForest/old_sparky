from __future__ import annotations

from pathlib import Path

path = Path("platform/apps/platform_api/app/api/routes/tournaments.py")
text = path.read_text(encoding="utf-8")
old = "from apps.platform_api.app.services.tournament_runtime_cache import (\n    invalidate_tournament_runtime_caches,\n    register_tournament_runtime_cache_invalidator,\n)"
new = "from apps.platform_api.app.services.tournament_runtime_cache import (\n    register_tournament_runtime_cache_invalidator,\n)"
if old in text:
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
