# Platform documentation governance

- Status: Active reference and maintenance how-to
- Owner: Platform maintainers
- Last reviewed: 2026-11-23

This document defines where platform facts belong and how to keep the
repository's documentation, agent instructions and project skills coherent.

## Source-of-truth map

| Information | Owner |
| --- | --- |
| Current production state, verified release evidence and next priority | [`CURRENT.md`](CURRENT.md) |
| Document routing and reading contract | [`README.md`](README.md) |
| Product behavior, user roles, permissions and workflow contracts | [`product-reference.md`](product-reference.md) |
| Components, data ownership and flow, trust boundaries and deployment topology | [`production-architecture.md`](production-architecture.md) |
| Repeatable operator actions and recovery procedures | The task-specific runbook in [`docs/`](.) |
| Test placement, verification gates and CI route contracts | [`test-suite-governance.md`](test-suite-governance.md) |
| Performance profiles and matrix | [`platform/performance/README.md`](../performance/README.md) |
| External-load runner safety and evidence boundaries | [`performance-external-load-safety.md`](performance-external-load-safety.md) |
| One accepted architectural decision | The relevant record in [`adr/`](adr/) |
| Prior implementation evidence and superseded checkpoints | [`archive/`](archive/) |
| Prioritized future work | [`platform-roadmap.md`](platform-roadmap.md) |
| Repository-wide or path-specific agent behavior | The nearest [`AGENTS.md`](../../AGENTS.md) |
| Reusable task workflow and trigger conditions | The relevant project skill in [`../../.agents/skills/`](../../.agents/skills/) |

One fact has one owner. Other documents may summarize it only when they link
back to the owner. A review date means that the document was checked; it is
not evidence that a production or operator-only condition is currently true.

## Instruction hierarchy

- Root `AGENTS.md` contains repository-wide rules.
- A nested `AGENTS.md` adds rules for its subtree; it must not silently
  contradict a broader rule.
- A project skill is selected by its frontmatter description and contains the
  reusable workflow for that task. Keep path-specific detail in the skill or
  the nearest nested instruction file.
- Do not add `CLAUDE.md`, `GEMINI.md` or Copilot instruction mirrors merely to
  duplicate `AGENTS.md`. Add a tool-specific file only when its behavior or
  format is genuinely different, and link the ownership decision here.

## Documentation shape

Use the smallest useful form from the Diátaxis model:

- tutorials teach a newcomer a complete path;
- how-to guides solve a known operational task;
- reference documents define stable contracts and interfaces;
- explanation documents capture context, rationale and trade-offs.

ADRs are short records of one decision: status, context, decision and
consequences. When a decision changes, add a superseding ADR and preserve the
accepted record as historical context.

## Maintenance workflow

For a code, workflow, deployment, instruction or skill change:

1. Read `CURRENT.md` and `README.md`, then the relevant owner documents.
2. Trace changed facts to code, migrations, tests, workflow YAML, deployment
   evidence or a dated external/operator report. Identify whether the change
   alters product behavior, permissions/workflow, data ownership/flow, a trust
   boundary, runtime/deployment/recovery, a verification gate or an operator
   procedure; use the source-of-truth map above to select each affected owner.
3. Update each affected owner and its navigation or directly affected links in
   the same package. Architecture and contract changes must leave their owner
   documents accurate before the change is complete. A refactor, formatting or
   test-only change that changes no owned behavior does not need a documentation
   edit; state briefly in the handoff why the relevant owner documents remain
   accurate. This is a semantic review, not a rule that every code diff needs a
   documentation diff.
4. Move completed plans and dated checkpoints to `archive/`; label them as
   historical and remove “latest/current” language from active navigation.
5. Remove obsolete commands, paths, statuses, duplicate copies and personal
   or secret data. Use configuration names and placeholders instead.
6. Read the diff as a new operator or contributor: every command should be
   copyable, every mutation should be marked, and every unresolved action
   should have an owner.

When work exposes a stale, incorrect or ambiguous instruction, or a repeated
failure shows that an instruction is incomplete, trace it to its owner document
and repair that document or its owning script in the same task before declaring
the affected stage complete. First distinguish a documentation defect from an
execution error against a correct procedure: in the latter case, follow the
existing procedure and do not add duplicate rules or checks. Replace obsolete
steps instead of appending an incident narrative. Make operational instructions
reproducible: state exact commands, required inputs and source bindings,
validation and success criteria, and abort and cleanup steps. Keep unrelated
work moving when the documentation repair does not block it.

## Verification

From `platform/`, run:

```bash
tools/platform_run_quiet.sh "Platform docs gate" -- .venv_platform/bin/python tools/platform_verify.py docs
tools/platform_run_quiet.sh "Platform verification contract gate" -- .venv_platform/bin/python tools/platform_verify.py verification-contract
git diff --check
```

The docs gate checks document shape, repository-local links and project skill
metadata. It does not infer semantic consistency between changed code and its
owner documents; reviewers verify that relationship using the source-of-truth
map and the change handoff. For a newly created skill, also run the system skill creator's
`quick_validate.py` when available. A local gate blocked by a missing safe
dependency is not a pass; report it as `LOCAL GATE BLOCKED`.

## External orientation

Keep always-applied agent instructions focused and route deeper context to
task-specific documents, following [OpenAI's Codex guidance on skills and
prompts](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra).
Use [skill metadata to select reusable workflows](https://developers.openai.com/plugins/concepts/skills),
and keep repository-wide and path-specific guidance scoped to the files it
governs, as described by [GitHub's custom-instructions model](https://docs.github.com/en/copilot/concepts/prompting-and-context/custom-instructions).
Use [Diátaxis](https://diataxis.fr/) as a lightweight way to distinguish
procedures, reference, learning and explanatory context, not as a reason to
create extra document layers.
