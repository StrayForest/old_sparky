---
name: deadlock-workflow-guardrails
description: Use before Deadlock ready-check, captain, dream-slot, assignment, roster, bracket, or workflow-state changes.
---

# Deadlock Workflow Guardrails

Use for Deadlock tournament workflow changes.

## Workflow

- Trace API route, domain function, persistence model, and UI caller.
- Inventory every writer for the transition: manual API, service, scheduled
  automation, worker and admin path. Do not assume a Redis job lock protects a
  concurrent HTTP or automation writer.
- Lock the tournament row first with `SELECT ... FOR UPDATE`, then lock
  secondary rows in one documented order and re-read staging/terminal state.
  Keep check, mutation, audit and commit in that transaction.
- Use database partial unique constraints/indexes as the final guard for active
  ready checks and canonical captain/assignment/published-or-locked roster
  state. Reconcile existing rows before adding a constraint.
- Make ready votes conditional on both an active round and an eligible active
  participant. A route preflight or upsert conflict target alone is not a
  lifecycle guard.
- For dream-slot replace-all writes, lock the owning user/profile before
  delete-and-reinsert and enforce the supported slot range; do not allow a
  mixed concurrent payload to become source-of-truth input.
- Check affected state transition, permissions, visibility, stale-run behavior, and locked-roster behavior.
- Read `references/invariants.md` when the change touches assignment, roster lock, or bracket handoff.
- Add focused independent-session tests for competing starts, vote versus
  close/exclusion, publish/lock versus terminal transition, worker versus API,
  and concurrent dream-slot replacement. Assert persisted final state, not
  only response codes.

## Output

State affected invariant, writers/lock order, expected transition change,
database guard, required concurrency tests, and launch risk.
