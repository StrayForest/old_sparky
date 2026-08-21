---
name: deadlock-workflow-guardrails
description: Use before Deadlock ready-check, captain, dream-slot, assignment, roster, bracket, or workflow-state changes.
---

# Deadlock Workflow Guardrails

Use for Deadlock tournament workflow changes.

## Workflow

- Trace API route, domain function, persistence model, and UI caller.
- Check affected state transition, permissions, visibility, stale-run behavior, and locked-roster behavior.
- Read `references/invariants.md` when the change touches assignment, roster lock, or bracket handoff.
- Add focused route/domain tests for changed transitions.

## Output

State affected invariant, expected transition change, required tests, and launch risk.
