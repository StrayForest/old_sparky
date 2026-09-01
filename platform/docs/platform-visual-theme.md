# Platform visual theme

- Status: Active reference
- Owner: Platform web
- Last reviewed: 2026-09-01

## Ownership

- `app/globals.css` owns structure and fallback geometry.
- `app/theme-modern.css` owns semantic tokens and active presentation; it is
  imported after `globals.css`.
- Components own semantics and interaction, not page-specific theme colors.
- Russian UI text belongs in `lib/i18n.ts`.

Use `--ui-*` tokens for canvas, surfaces, actions, state, content, radius and
shadow. The old `--gold`, `--bg-*` and `--text-*` mapping is retained only as a
compatibility layer while structural CSS still consumes it.

## Visual rules

- Deep navy canvas, cold layered surfaces, violet primary actions, teal
  secondary state and restrained semantic status colors.
- Borders separate controls or independent surfaces; decorative frames and
  nested boxes are avoided.
- Non-clickable cards have no hover transform, cursor or state that suggests an
  action.
- Focus remains keyboard-visible; headings are semantic and decorative icons,
  arrows/numbers are hidden from assistive technology.
- Layouts are fluid and bounded by the shared main width. No route may create
  horizontal overflow at desktop, 1300 px, tablet or phone viewports.

## Home page geometry

- The home hero uses one responsive `--home-hero-space` for title top offset,
  subtitle-to-actions distance and actions-to-hero-bottom distance. A geometry
  test allows at most one CSS pixel of rounding difference.
- The tournament path is three text-only panels: `104px` high on desktop/tablet
  and `94px` on phones. Numbers are unframed text on the panel background;
  arrows point right on desktop and down in the one-column layout.
- Patch, video and social groups share the main left/right edges and equal
  inter-group spacing. Source images must decode before visual capture.
- The background image is loaded once on `body`; the hero adds gradients only.

## Shared assets and media

- Brand, background, patch cover and tournament templates use versioned shared
  WebP URLs. Replace bytes only with a new immutable URL/revision.
- Custom media uses prepared descriptors and existing fallbacks; components do
  not invent direct R2/S3 URLs.
- Next image optimization remains disabled for allowlisted editorial/prepared
  external assets until its native dependency boundary is deliberately
  revisited.

## Verification

```bash
cd /root/old_sparky/platform
tools/platform_run_quiet.sh "web typecheck" -- \
  tools/platform_web_npm.sh --prefix apps/platform_web run typecheck
tools/platform_run_quiet.sh "web lint" -- \
  tools/platform_web_npm.sh --prefix apps/platform_web run lint
tools/platform_run_quiet.sh "web build" -- \
  tools/platform_web_npm.sh --prefix apps/platform_web run build
```

Run affected Playwright scenarios at every configured viewport. Inspect desktop
and mobile screenshots after fonts, lazy images and CSS backgrounds load. Check
overflow, clipping, text contrast, focus, loading/error/empty/disabled states
and control sizing separately from decoration.

Fast style rollback removes the `theme-modern.css` import and rebuilds, but a
production rollback normally switches the complete previous release so code
and assets remain paired.

References: [WCAG contrast](https://www.w3.org/WAI/WCAG21/Understanding/contrast-minimum),
[Material color roles](https://developer.android.com/design/ui/wear/guides/styles/color/roles-tokens).
