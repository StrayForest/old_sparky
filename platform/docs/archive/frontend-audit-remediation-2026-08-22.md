# Frontend audit remediation — 2026-08-22

- Scope: `platform/apps/platform_web`
- Audit type: read-only full frontend/API-contract review
- Status: resolved; release publication pending
- Owner: Platform web maintainers

## Confirmed findings

| ID | Priority | Finding | Owner layer |
|---|---|---|---|
| FE-01 | P2 | Tournament creation accepts invite codes shorter than the backend `TournamentCreateRequest` contract and maps the resulting validation failure to a schedule error. | Web/API contract |
| FE-02 | P2 | Private tournament creation remains actionable after the monthly allowance and additional private credits are exhausted; the backend rejects the request. | Web/permission UX |
| FE-03 | P2 | Steam unlink is shown to accounts that do not yet have the verified-email/password prerequisite required by the backend. | Web/auth contract |
| FE-04 | P2 | Admin user-search responses are not generation- or abort-guarded, so a slow older response can replace a newer query result. | Web/async state |
| FE-05 | P2 | Server-rendered pages that load initial API data have no application error boundary with a retry path. | Web/error state |
| FE-06 | P3 | A temporary server-side session lookup failure on `/tournaments/new` is presented as an anonymous session. | Web/auth/session |
| FE-07 | P3 | Successful internal tournament navigation uses a full document reload. | Web/navigation |
| FE-08 | P3 | The statistics page bypasses the Russian i18n catalog and exposes English/internal phase labels. | Web/i18n |
| FE-09 | P2 | A bracket refresh could overwrite a locally edited match schedule before the organizer saved it. | Web/workflow async state |
| FE-10 | P2 | The account email draft could be reset by its initial effect after the user had already started typing. | Web/auth async state |

## Intended behavior

- Frontend validation must match the backend schema before a mutation is sent.
- A creation control must be disabled when the selected visibility has no available allowance, while the server remains authoritative for concurrent changes.
- Destructive identity actions must be rendered only when the authoritative current-user DTO says the prerequisite is satisfied.
- Search requests must cancel or ignore stale responses and stale loading/error transitions.
- Initial server API failures must render a safe Russian retry state without exposing exception text.
- Session unavailability must remain distinct from an anonymous session.
- Internal route transitions must use App Router navigation; external OAuth redirects remain full navigations.
- User-facing Russian copy must come from `lib/i18n.ts`.
- A bracket refresh must preserve an unsaved schedule draft and reconcile it only after the server accepts the new schedule.
- Account email initialization must not run a mount reset after user input; server email changes may still reset the draft.

## Implementation

- Added an authoritative `can_unlink_steam` capability to the current-user API DTO and gated the profile action on it.
- Aligned tournament invite-code validation and test fixtures with the backend minimum length of 10; disabled exhausted private creation before submit and kept 409 handling specific.
- Added abort/generation guards to admin user searches, a retry error boundary, and a distinct transient session-unavailable state.
- Replaced internal tournament full reloads with App Router navigation and moved stats copy into the Russian catalog.
- Added draft guards for account email and bracket match scheduling so asynchronous refreshes cannot discard user input.
- Updated the frontend UX skill with the contract/capability, async guard, retry-boundary, and navigation rules used by this remediation.

## Verification and release gates

Passed before release publication:

- `web typecheck`
- `web lint`
- `web build`
- focused account email repeat-each (10/10), bracket schedule repeat-each (10/10), CSP repeat-each (5/5), and affected auth/creation smoke scenarios
- full deterministic web smoke after the final fixes (`[OK] web full smoke final`)
- full platform test suite (`[OK] platform tests`)
- `platform docs: ok`

The release package still requires release preflight, immutable build publication, deploy smoke, and the final commit/merge evidence. No migration is expected.

## Release evidence

Release commit, branch merge, release artifact, deploy target, smoke result, and rollback reference will be recorded here after deployment.
