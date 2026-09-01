# Platform documentation governance

- Status: Active reference and maintenance how-to
- Owner: Platform maintainers
- Last reviewed: 2026-09-01

This document defines where platform facts belong and how to keep the
repository's documentation, agent instructions and project skills coherent.

## Source-of-truth map

| Information | Owner |
| --- | --- |
| Current production state, verified release evidence and next priority | [`CURRENT.md`](CURRENT.md) |
| Document routing and reading contract | [`README.md`](README.md) |
| Product, role and workflow contracts | [`product-reference.md`](product-reference.md) |
| Components, data flow, trust boundaries and deployment topology | [`production-architecture.md`](production-architecture.md) |
| Repeatable operational procedures | The relevant runbook in [`docs/`](.) |
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
   evidence or a dated external/operator report.
3. Update the owner, navigation and directly affected cross-links together.
4. Move completed plans and dated checkpoints to `archive/`; label them as
   historical and remove “latest/current” language from active navigation.
5. Remove obsolete commands, paths, statuses, duplicate copies and personal
   or secret data. Use configuration names and placeholders instead.
6. Read the diff as a new operator or contributor: every command should be
   copyable, every mutation should be marked, and every unresolved action
   should have an owner.

## Verification

From `platform/`, run:

```bash
.venv_platform/bin/python tools/platform_verify.py docs
.venv_platform/bin/python tools/platform_verify.py verification-contract
git diff --check
```

The docs gate checks document shape, repository-local links and project skill
metadata. For a newly created skill, also run the system skill creator's
`quick_validate.py` when available. A local gate blocked by a missing safe
dependency is not a pass; report it as `LOCAL GATE BLOCKED`.

## External orientation

The maintenance rules are aligned with current guidance from [OpenAI's Codex
engineering guide](https://openai.com/business/guides-and-resources/how-openai-uses-codex/),
[Anthropic's memory guidance](https://code.claude.com/docs/en/memory),
[GitHub's custom-instructions model](https://docs.github.com/en/copilot/concepts/prompting/response-customization),
[Google's code-review practices](https://google.github.io/eng-practices/review/reviewer/looking-for.html),
and the [Diátaxis documentation framework](https://diataxis.fr/).
