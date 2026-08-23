from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "platform/python_packages/platform_infra/models.py",
    '''        CheckConstraint(\n            "status <> 'completed' OR ("\n            "(winner_side = 'home' AND home_score IS NOT NULL AND away_score IS NOT NULL "\n            "AND home_score > away_score AND home_team_id IS NOT NULL "\n            "AND winner_team_id = home_team_id) OR "\n            "(winner_side = 'away' AND home_score IS NOT NULL AND away_score IS NOT NULL "\n            "AND away_score > home_score AND away_team_id IS NOT NULL "\n            "AND winner_team_id = away_team_id))",\n            name="completed_result_consistent",\n        ),\n''',
    '''        CheckConstraint(\n            "status <> 'completed' OR ("\n            "(winner_side = 'home' AND home_score IS NOT NULL AND away_score IS NOT NULL "\n            "AND home_score > away_score) OR "\n            "(winner_side = 'away' AND home_score IS NOT NULL AND away_score IS NOT NULL "\n            "AND away_score > home_score))",\n            name="completed_result_consistent",\n        ),\n''',
)

migration = "platform/alembic/versions/20260823_0041_persistence_concurrency_hardening.py"
replace_once(
    migration,
    '''           OR (status = 'completed' AND NOT (\n                (winner_side = 'home' AND home_score IS NOT NULL AND away_score IS NOT NULL\n                 AND home_score > away_score AND home_team_id IS NOT NULL\n                 AND winner_team_id = home_team_id)\n                OR\n                (winner_side = 'away' AND home_score IS NOT NULL AND away_score IS NOT NULL\n                 AND away_score > home_score AND away_team_id IS NOT NULL\n                 AND winner_team_id = away_team_id)\n           ))\n''',
    '''           OR (status = 'completed' AND NOT (\n                (winner_side = 'home' AND home_score IS NOT NULL AND away_score IS NOT NULL\n                 AND home_score > away_score)\n                OR\n                (winner_side = 'away' AND home_score IS NOT NULL AND away_score IS NOT NULL\n                 AND away_score > home_score)\n           ))\n''',
)
replace_once(
    migration,
    '''            "status <> 'completed' OR ("\n            "(winner_side = 'home' AND home_score IS NOT NULL AND away_score IS NOT NULL "\n            "AND home_score > away_score AND home_team_id IS NOT NULL AND winner_team_id = home_team_id) OR "\n            "(winner_side = 'away' AND home_score IS NOT NULL AND away_score IS NOT NULL "\n            "AND away_score > home_score AND away_team_id IS NOT NULL AND winner_team_id = away_team_id))",\n''',
    '''            "status <> 'completed' OR ("\n            "(winner_side = 'home' AND home_score IS NOT NULL AND away_score IS NOT NULL "\n            "AND home_score > away_score) OR "\n            "(winner_side = 'away' AND home_score IS NOT NULL AND away_score IS NOT NULL "\n            "AND away_score > home_score))",\n''',
)

print("completed-match compatibility fix applied")
