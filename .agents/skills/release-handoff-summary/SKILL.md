---
name: release-handoff-summary
description: Use at the end of substantive OldSparky code, docs, QA, or release work for a concise handoff.
---

# Release Handoff Summary

Use when substantive work is ready for review or release handoff.

## Workflow

- Gather `git status --short`, changed files, checks run/skipped, deploy impact, rollback impact, and risks.
- Use `references/template.md` when a structured handoff is useful.
- Separate unrelated dirty files from the current change.

## Output

Return branch suggestion, title, summary, verification, deploy notes, rollback notes, and risks.
