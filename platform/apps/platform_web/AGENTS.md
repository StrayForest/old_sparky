# Platform Web Agent Guide

## Rules

- Build dense operational Deadlock tournament UI, not marketing pages.
- Use `lib/platform-api.ts` for API calls and `lib/platform-types.ts` for shared types.
- Keep sample data in `tests/`; production routes must not fall back to fabricated tournaments or profiles.
- Do not restore static HTML reference adapters under `public/` or `components/`.
- Put user-facing strings in the Russian-only `lib/i18n.ts` catalog.
- Keep hero/rank assets under `public/assets/` and follow existing placeholder conventions.
- Preserve targeted client-state updates; avoid full-page refreshes for workflow actions when practical.

## UI Quality

- Reuse existing CSS variables and component patterns.
- Check desktop and mobile for overflow, overlap, and unstable fixed-format controls.
- Keep cards for repeated items or framed tools only; avoid nested cards and decorative-only redesigns.
- Do not add broad visual systems or dependencies without a direct request.

## Verification

- UI changes: `../../tools/platform_web_npm.sh run build`.
- UI plus API contract changes: also run `.venv_platform/bin/python -m unittest discover -s tests` from `platform/`.
