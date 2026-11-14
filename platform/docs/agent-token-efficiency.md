# Agent work protocol

- Status: Active working reference
- Owner: Platform maintainers
- Last reviewed: 2026-11-14

Use this protocol to keep agent handoffs and repeated context small without
skipping required review, verification, security or release work.

## Coordination

- The primary agent coordinates only. Delegate repository inspection, edits,
  checks and operations to `gpt-6-luna`; choose reasoning effort to match task
  complexity. The primary assigns ownership, routes findings and reports
  status; it does not inspect or edit implementation files or run repository
  checks.
- Give each agent a clear scope, current source or dirty-worktree state,
  relevant constraints and expected handoff. One agent is enough for a bounded
  task; parallelize only independent work with distinct owners.
- Continue in the supplied worktree. On failure or interruption, inspect its
  current state and resume from the latest proven checkpoint. Preserve valid
  changes and completed checks; do not repeat work without a changed input,
  failure signal or required fresh result. Do not retry an unchanged external
  blocker.
- Assign one owner to poll each CI run, production operation or live QA run.
  Other reviewers use saved evidence rather than repeating the operation.

## Read and verify

- Read `platform/docs/CURRENT.md` and `platform/docs/README.md`, then open only
  the relevant owner documents. Search first and read bounded sections of long
  files. Do not load the entire repository or archive for routine work.
- Batch independent read-only lookups. Keep output bounded and retain full
  check or operational output in its designated evidence location.
- Run the checks owned by the change. Keep required gates; avoid repeats only
  when their inputs, environment and required evidence are unchanged. Follow
  `platform/docs/test-suite-governance.md` for selectors and gate ownership.
- Report the exact commands and outcomes. A quiet wrapper, active process or
  missing safe dependency is not a pass; follow the owner procedure and report
  `LOCAL GATE BLOCKED` when a required local check cannot run.
- Separate committed-source claims from dirty-worktree claims. Bind CI,
  deployment and QA statements to the exact source SHA, run/attempt and target
  described by the evidence. Do not attribute an uncommitted fix to a commit.

## Safe continuation

- For an interrupted task, pass forward the error, worktree/branch, owned
  paths, current check/run state and a concise diff summary. Confirm owned
  processes, ports or temporary state are clear before restarting an
  interrupted check. Never repeat production deployment, backup, cleanup or QA
  side effects without checking their current receipt and state.
- Follow the owning procedure for production artifacts and retention in
  [`platform-storage-retention`](../../.agents/skills/platform-storage-retention/SKILL.md),
  release behavior in [`deployment-runbook.md`](deployment-runbook.md), and
  private fixture credentials in
  [`fixture-credential-transport.md`](fixture-credential-transport.md). This
  protocol does not replace those source-bound safety steps.
- Before CI for an intended runtime release, classify the exact candidate
  range; require a `full`, deployable, non-fallback route and its expected
  artifact job. Docs-only and out-of-scope routes remain documented no-ops,
  not guard bypasses. If unshipped changes accumulate, classify the complete
  baseline-to-target range. Follow the [development guide](development-guide.md)
  and [deployment runbook](deployment-runbook.md); never dispatch a no-op or
  missing-artifact route as a release.
- Reuse the existing worktree, compatible approved environment and canonical
  release artifact path. Do not duplicate a repository or copy release bundles
  to unowned storage; the release builder and retention procedure own those
  paths and locks.
- For production Python probes, use the approved wrapper or exact approved
  interpreter with bytecode disabled. Do not run bare virtualenv CLIs against
  immutable release payloads or parallelize checks that can write to the same
  payload.
- Do not install dependencies into a shared or approved environment to unblock
  one check. Reuse only an existing compatible environment; otherwise report
  `LOCAL GATE BLOCKED` and leave required CI gates intact.
- For a gate that can outlive its session, use a private atomic terminal
  receipt with its run ID, exact command, UTC finish time and exit code. Poll
  the receipt, not logs or process listings. Without a final receipt, treat the
  run as incomplete; stop only its owned process group and prove its
  descendants, ports and temporary files are clear before any rerun.
- If the user already supplied a retained operator input, check
  `/root/old_sparky/.local/private/operator-inputs.md` (also from a worktree).
  Read values without printing them. Never copy them into Git, logs, artifacts
  or reports.
- Keep one active-state handoff with verified facts, source SHA, relevant run
  IDs, owners, blocker and next action. Link detailed evidence instead of
  repeating incident history.
- Report token counts only when actual session usage metadata is available.
  Otherwise report observable process measures, such as repeated reads, tests,
  polls or handoff size. Do not invent savings, token counts or percentages.

## Handoff

Use the shortest form that carries the needed evidence:

```text
Fact: <verified result>
Evidence: <source SHA, command, run ID or path>
Blocker: <observed condition or none>
Next: <one action and owner>
```
