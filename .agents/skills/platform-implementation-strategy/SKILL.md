---
name: platform-implementation-strategy
description: Use before platform API, schema, permission, workflow, runtime, release, or cross-module changes.
---

# Platform Implementation Strategy

Use before editing platform behavior or data flow.

## Workflow

- Identify owner layer: API/schema, domain, infra/model/migration, web, or release tooling.
- Check the current code path, `platform/docs/CURRENT.md` and `platform/docs/platform-roadmap.md`.
- For a mutable workflow, inventory every writer: route, service, automation,
  worker and administrative path. Make all durable writers share one
  PostgreSQL transaction boundary; Redis may coalesce work but is not the
  correctness authority.
- Lock the stable aggregate row first (`Tournament` for tournament workflow;
  `User`/profile for profile replacement), define a canonical secondary-row
  lock order, and re-read terminal/lifecycle state after locks are held.
- Make a database constraint/index the final guard for every cardinality or
  exclusivity invariant. Use application checks for messages, not as the sole
  protection against concurrent writers.
- For lifecycle-sensitive child writes, use a conditional mutation or lock and
  revalidate the parent/child state in the same transaction; a preflight alone
  cannot prevent a post-close/post-exclusion commit.
- For replace-all collections such as dream slots, serialize the owning row;
  add optimistic revision semantics when clients need conflict detection.
- Define behavior, data/schema impact, permissions, tests, rollback risk, and legacy-bot boundary.
- Design concurrent-index migrations for populated databases: preflight or
  repair duplicate data, handle invalid interrupted indexes on retry, and test
  upgrade/retry compatibility as well as the fresh schema.
- Keep the strategy short; implementation details belong in code and tests.

## Output

State intended behavior, writers and lock order, data/constraint impact,
concurrent-request schedule and expected final state, tests, and rollback risk.
