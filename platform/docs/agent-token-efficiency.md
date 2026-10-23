# Agent token-efficient work protocol

- Status: Active working reference
- Owner: Platform maintainers
- Last reviewed: 2026-10-23

Use this protocol to reduce repeated context and tool output while preserving
required evidence, review, safety checks and release gates.

## Keep context small and current

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
- Assign one owner/poller to each CI run, production operation and live QA.
  Other agents can review saved evidence; they do not independently poll the
  same run or repeat the same production check.
- The orchestrator resumes from the latest proven state. Reopen hypotheses
  only when a new log, source diff, run result or live observation provides a
  signal.

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
