# Agent token-efficient work protocol

- Status: Active working reference
- Owner: Platform maintainers
- Last reviewed: 2026-10-25

Use this protocol to reduce repeated context and tool output while preserving
required evidence, review, safety checks and release gates.

## Orchestrator and delegation

- The primary agent coordinates only. Delegate repository inspection, edits,
  checks and operations to `gpt-6-luna`; do not switch models. The primary
  assigns scope, routes findings and reports status, but does not inspect or
  edit implementation files or run repository checks.
- Set reasoning effort to `low` for bounded research or mechanical work,
  `medium` for related multi-file changes or behavioral reasoning, and `high`
  for security, data integrity, release/recovery or broad cross-module work.
  Use a higher effort only when the risk or dependencies need it.
- Give each agent one clear owner scope and only the task-relevant context.
  Parallelize independent work within available capacity. Prefer assigning
  related follow-up work to an existing idle Luna agent with useful context.
- When an agent fails or is interrupted, resume the same objective with a Luna
  agent from the latest checkpoint. Pass the error, current worktree/branch,
  owned paths and concise `git diff` summary. The retry checks the current diff,
  preserves valid work and completes only the remaining work; do not repeat
  completed actions or successful gates without a new change or failure signal.
  Do not loop on an unchanged external blocker.
- Keep checks narrow but complete: remove redundant exploration/output, never
  required gates or evidence. The delegated owner runs checks and returns their
  exact command, status, and report location.
- The primary agent gives short, meaningful progress updates when a decision,
  blocker or milestone changes. Do not repeat unchanged status or repeatedly
  poll agents for reports; use the worker's checkpoint and request an update
  when a dependency or handoff needs it. Keep the final handoff concise.
- Token efficiency never justifies dropping security checks, required tests,
  independent review or release/production gates.

## Keep context small and current

### Reuse source worktrees and release artifacts

- Continue in the existing clean worktree supplied for the task. Do not create
  another repository copy just to build or inspect a release.
- The release builder defaults to the primary checkout's
  `platform/dist/releases`, including when invoked from a linked worktree.
  That is the source-artifact owner path shared with maintenance retention and
  the release-output flock; production CI's per-run output override stays
  isolated. Use `PLATFORM_RELEASE_OUTPUT_DIR` only for an
  intentionally isolated fixture or a separately owned build.
- Keep release archives and their checksum sidecars together under that owner
  path. Do not copy release bundles into worktree-local `dist/releases`,
  arbitrary `.git` directories or `/tmp` for convenience. Existing same-slug
  output remains immutable and the builder refuses collisions; artifact
  storage identity does not grant release/deployment authority.
- The existing maintenance retention owns old canonical source artifacts.
  Review its exact plan and preserve current/previous releases and all active
  work; do not add a second cleanup timer or broad temporary-file sweep.
- For production Python probes, use the approved wrapper or the exact approved
  interpreter with bytecode disabled (`-B` or `PYTHONDONTWRITEBYTECODE=1`).
  Never run a naked venv CLI against an immutable payload, and do not parallelize
  checks on one payload if any may write bytecode or caches.
- On a production-sharing checkout, an exact known own-site receipt or report
  may be inspected locally with a bounded read under its existing ownership and
  permissions. Check that evidence before proposing a new capture workflow;
  keep raw private output private. This read-only evidence path does not grant
  permission to mutate production: release, cleanup and deployment still use
  their documented workflows and locks.
- If a worktree-local test environment is missing, do not create another copy
  on a production-sharing filesystem. A focused hermetic test may use an
  existing approved environment only when its dependencies are already
  compatible; invoke its exact interpreter with bytecode disabled and do not
  install into or alter that environment. This supplements but never replaces
  required hash-locked CI gates; otherwise report `LOCAL GATE BLOCKED` and let
  CI run the required gate.

### Retained private operator inputs

- Before asking for `control_email` or another operator input the user has
  already provided, check the existing private
  `.local/private/operator-inputs.md` in the primary checkout. From a
  worktree, use `/root/old_sparky/.local/private/operator-inputs.md` or resolve
  the primary checkout through Git's common directory.
- If the requested value is present, use it without asking again. Read values
  without printing them; never copy them into Git, logs, artifacts or reports.
- In documents and handoffs, keep only the private-file pointer; never include
  the operator input value.

- Keep one short active state in the task handoff: verified facts, source SHA,
  relevant run IDs, owners, blocker (if any) and the single next action. Link
  detailed evidence; do not paste the incident history into every handoff.
- Give each agent a compact task packet: objective, owned files/action, exact
  source SHA or current dirty-worktree status, constraints, evidence links,
  and expected report. Refer to prior context by file/section instead of
  repeating it.
- Before resuming work after capacity or interruption, inspect the current
  worktree/branch and active agent/run state. Continue from the latest proven
  checkpoint; do not replay deploy, backup, cleanup or another completed side
  effect. On a capacity error, save the checkpoint, verify live state, then
  send a focused follow-up to the same Luna agent when available. If a new
  agent is needed, pass only a compact task packet and evidence links. Use a
  short backoff before retrying; never run a rapid retry loop. Retry the
  requested model when it recovers; do not switch this task to another model
  or provider unless the user changes that constraint.
- Treat a failed default environment or workflow attempt as a checkpoint, not
  a reason to repeat a completed gate or create duplicate repositories or
  environments. Check for an existing permitted reuse route and continue from
  the failed stage.
- Assign one owner/poller to each CI run, production operation and live QA.
  Other agents can review saved evidence; they do not independently poll the
  same run or repeat the same production check.
- The orchestrator resumes from the latest proven state. Reopen hypotheses
  only when a new log, source diff, run result or live observation provides a
  signal.
- Before an expensive production-bound CI push, cheaply classify the exact
  pending source range with the canonical classifier. Require the expected
  full, deployable, non-fallback runtime route and artifact job; classify the
  complete baseline-to-target range for cumulative unshipped releases. Fix a
  no-op/missing-artifact route before CI; never bypass the guard.

## Read and report narrowly

- Batch independent read-only lookups. Search with `rg`, then read only the
  relevant function, section or bounded line range. For long files, use
  headings and line numbers rather than loading the whole file.
- Separate committed-source claims from dirty-worktree claims: use
  `git show <exact-sha>:<path>` for CI/release attribution and `git diff` (or
  focused file reads) for uncommitted changes. Never attribute a dirty fix to
  a SHA that does not contain it.
- Bound command output to the needed lines/files and suppress routine noise;
  retain full test, CI, deploy and QA results in their evidence artifact.
- Keep reports to a few lines using **Fact / Evidence / Blocker / Next**.
  Label inference and unresolved cause as such; do not restate hypotheses
  without new evidence.

## Preserve quality and measure honestly

- For gates that import runtime dependencies, use the canonical hash-locked environment or let CI run them; do not rebuild a runtime one missing import at a time.
- Do not repeat an expensive local or CI gate without a new source, failed or
  inconclusive result, changed environment, or explicit required rerun. Do
  not omit required CI gates, independent review, rollback checks or production
  QA to save tokens.
- Before a heavy full suite, finish targeted tests and independent review for
  risky changes. A quiet wrapper's silence or a live process is not success:
  wait for its exit and inspect the retained failure log on nonzero status.
  After an interrupted build or browser run, verify owned processes and fixed
  ports are clear before starting another build.
- For gates that import runtime dependencies, use the canonical locked
  environment or defer to CI. Do not rebuild the environment one missing
  import at a time.
- After renaming a helper or entrypoint, search all call sites with `rg` and
  cover the actual shell entry path in regression evidence.
- Split work by real ownership or risk (implementation, independent review,
  operations); use one agent for a simple bounded task. Do not impose an
  arbitrary task-size cap that hides a real blocker.
- Track token use only when session `token_count` usage metadata is available;
  inspect that metadata alone. Otherwise report operational proxies such as
  repeated reads/tests/polls and handoff size. Compare before/after only when
  both were actually recorded; never invent savings percentages, token counts
  or cost. Do not claim a percentage reduction without comparable before/after
  usage measurements; until then, report process changes and counted proxies.

## Compact templates

**Task packet**

```text
Goal: <one outcome>
Scope/owner: <files or operation>
Source: <exact SHA, or dirty worktree>
Constraints/evidence: <short links or paths>
Return: <fact, evidence, blocker, next>
```

**Active state**

```text
State: <source SHA> | CI <run/attempt> | backup/deploy <IDs> | live <pointer/health>
Owners: <ops / core / QA>
Evidence: <paths; details live there>
Blocker: <observed fact or none>
Next: <one action — owner>
```

**Report**

```text
Fact: <what is established>
Evidence: <exact SHA/run/path and result>
Blocker: <specific observed condition, or none>
Next: <one action and owner>
```
