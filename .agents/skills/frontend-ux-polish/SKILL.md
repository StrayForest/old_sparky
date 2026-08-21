---
name: frontend-ux-polish
description: Use for platform web UI, i18n, responsive layout, interaction, hero/rank picker, or visual polish changes.
---

# Frontend UX Polish

Use when changing `platform/apps/platform_web`.

## Workflow

- Keep operational screens dense, restrained, and consistent with existing CSS variables.
- Put user-facing text in the Russian-only `lib/i18n.ts` catalog. Do not add a
  locale selector, locale persistence, or English fallback unless the product
  explicitly restores multilingual support.
- Use `lib/platform-api.ts` and `lib/platform-types.ts`.
- Check mobile/desktop overflow, overlap, loading/error/empty/disabled states, and stable control sizing.
- Run from `platform/`:
  `tools/platform_run_quiet.sh "web build" -- tools/platform_web_npm.sh --prefix apps/platform_web run build`;
  add platform tests if API contracts changed.

## Output

Summarize UI behavior, i18n keys, responsive checks, and build/test status.
