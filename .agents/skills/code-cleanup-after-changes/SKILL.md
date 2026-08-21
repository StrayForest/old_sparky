---
name: code-cleanup-after-changes
description: Use after OldSparky code, UI, API, test, docs, skill, or tooling changes to remove unused components, helpers, imports, styles, i18n keys, tests, scripts, routes, and legacy code that no longer has an owner.
---

# Code Cleanup After Changes

Use this skill after any substantive change package before verification and
handoff.

## Workflow

1. Identify the removed or replaced behavior and its owning layer: web, API,
   domain, infra, migration, release tooling, docs, or skills.
2. Search from the symbol/file outward with `rg`:
   - component/function/type/class names;
   - route paths and test ids;
   - CSS class names;
   - i18n keys;
   - script names and docs references.
3. Delete code only when no active owner remains. Do not delete API routes,
   schema fields, migrations, data repair tools, or compatibility behavior just
   because the current frontend no longer calls them; first verify there is no
   admin, QA, deploy, rollback, or external contract owner.
4. Remove related artifacts together:
   - imports/exports and barrel entries;
   - tests that only validate removed behavior;
   - mocks and fixtures for removed behavior;
   - CSS and design tokens used only by removed UI;
   - i18n keys used only by removed UI;
   - docs that instruct operators to use removed flows.
5. Prefer direct deletion over TODOs, dead wrappers, or "future" placeholders.
6. Preserve unrelated dirty work and never revert user changes.

## Verification

- Run `rg` again for deleted symbols and removed routes.
- Run the smallest relevant type/build/test check:
  - web, from `platform/`: `tools/platform_run_quiet.sh "web build" -- tools/platform_web_npm.sh --prefix apps/platform_web run build`;
  - backend, from `platform/`: `tools/platform_run_quiet.sh "platform tests" -- .venv_platform/bin/python -m unittest discover -s tests`;
  - tools/skills/docs: syntax or validator checks where available.
- Treat TypeScript unused imports, failed imports, stale tests, and stale docs
  as cleanup failures, not as separate future work.

## Output

Report what was removed, what was intentionally kept and why, `rg` cleanup
evidence, verification commands, and any remaining legacy risk.
