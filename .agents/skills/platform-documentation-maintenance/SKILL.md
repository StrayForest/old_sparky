---
name: platform-documentation-maintenance
description: Audit and update OldSparky documentation, AGENTS.md files, ADRs, runbooks, and project skills when code, workflows, production state, or agent guidance changes.
---

# Platform Documentation Maintenance

Use this workflow for a documentation or agent-guidance change package.

## Workflow

1. Read `platform/docs/CURRENT.md`, then `platform/docs/README.md`. Open only
   the owner documents needed for the change.
2. Inventory the instruction hierarchy (`AGENTS.md` files), active docs,
   ADRs, archive entries and `.agents/skills/`. Preserve unrelated dirty work.
3. Verify every changing fact against its source of truth: code, migrations,
   tests, workflow YAML, deployment state or an explicitly linked external
   report. Search for the old term, command, path, status and release ID across
   the repository before editing.
4. Assign each fact one owner. Keep current state in `CURRENT.md`, navigation
   in `docs/README.md`, procedures in runbooks, stable contracts in reference
   docs, decisions in ADRs and completed evidence in `archive/`. Link to the
   owner instead of copying policy into multiple files.
5. Keep persistent instructions short and imperative. Put path-specific or
   multi-step guidance in the nearest nested `AGENTS.md`, a skill, or a
   reference document. Do not add a second instruction system merely to mirror
   `AGENTS.md`.
6. For an ADR, record one decision, its status, context, consequences and
   supersession link when applicable. Do not rewrite an accepted decision to
   describe a new decision; add a superseding ADR when the decision changes.
7. For a skill, keep trigger conditions in frontmatter, keep `SKILL.md` under
   500 lines, use progressive disclosure, link only to necessary one-level
   references, and keep `agents/openai.yaml` aligned with the skill name.
8. Remove stale copies, dated checkpoint claims, obsolete commands and broken
   links. Do not retain a “future” placeholder when the owner or next action is
   known. Never write secrets, tokens, cookies, private reports or live
   personal data into documentation.

## Verification

Run the repository-owned checks from `platform/`:

```bash
.venv_platform/bin/python tools/platform_verify.py docs
.venv_platform/bin/python tools/platform_verify.py verification-contract
git diff --check
```

The docs gate covers local links, document shape and project skill metadata.
Use the system skill creator's `quick_validate.py` for a newly created skill
when that tool is available, then run the docs gate again. Search once more for
the superseded term/path and report any external or operator-only check that
cannot be proven from the repository.

## Output

Report the source-of-truth changes, stale artifacts removed, links/skills
validated, checks run, external evidence used and any remaining owner action.
