---
name: platform-implementation-strategy
description: Use before platform API, schema, permission, workflow, runtime, release, or cross-module changes.
---

# Platform Implementation Strategy

Use before editing platform behavior or data flow.

## Workflow

- Identify owner layer: API/schema, domain, infra/model/migration, web, or release tooling.
- Check the current code path and `platform/docs/platform-roadmap.md`.
- Define behavior, data/schema impact, permissions, tests, rollback risk, and legacy-bot boundary.
- Keep the strategy short; implementation details belong in code and tests.

## Output

State intended behavior, likely files/modules, data impact, tests, and risk.
