# AS-04 inactive private-workspace authorization — closed

- Status: Archived / resolved
- Closed: 2026-08-21
- Production verification: successful
- Implementation commit: `1405fdbaeaa7c66b974b782a26eb3cc81c5ebf6b`
- Regression-test commit: `492c3cefe65fbc7b1f9995612a6f3572699dfdc8`
- CI verification run: `32455353984`
- Production deployment run: `32455553444`
- Production release: `gha-32455553444-1-492c3cefe65f-20260821T064427Z`

## Original finding

Private invite-only tournament reads treated the existence of any `TournamentParticipant` row as current membership. Because withdrawn and disqualified rows are intentionally retained for history/audit, those inactive rows could continue satisfying private-workspace authorization for workspace, roster, matches, bracket and bracket SSE reads after active participation ended.

## Remediation delivered

- Added a fail-closed private-workspace authorization guard for the invite-only read contour before the existing route handlers run.
- The guard resolves the current participant status for the authenticated user and rejects `withdrawn`/`disqualified` historical records with `403`.
- Protected reads are limited to the private workspace surfaces: workspace, participant roster, matches, bracket and bracket SSE admission.
- Organizer and platform admin/superadmin authorization remain explicit and independent from participant membership.
- Active participants retain their existing access, public-tournament behavior is unchanged, and unrelated tournament routes are not converted into private-workspace gates.

## Verification

The implementation passed:

- focused unit coverage for path scoping, every runtime inactive participant status, active/non-member/public cases and organizer/admin exceptions;
- an API-level integration regression that performs invite claim + join, transitions users to `withdrawn` and `disqualified`, then proves `403` across workspace, roster, matches, bracket and bracket SSE while organizer access remains available;
- the full backend unit/integration suite and static/dependency security gates;
- frontend audit/typecheck/lint/production build and Playwright smoke;
- production preflight, immutable release build/checksum/install, Alembic-head validation and service restart checks;
- origin and public deploy smoke with enforced CSP and the normal Cloudflare/Nginx contour.

GitHub Actions production deployment run `32455553444` installed CI-verified commit `492c3cefe65fbc7b1f9995612a6f3572699dfdc8` as release `gha-32455553444-1-492c3cefe65f-20260821T064427Z`. `deadlock-api`, `deadlock-worker`, `deadlock-web` and Nginx were active after restart, and both deploy-smoke passes completed successfully.

No Cloudflare or Turnstile control was weakened, and no broad/manual live-test bypass was restored to make the release pass.

## Remaining scope

AS-06 separately owns SSE connection-pressure limits and disconnect/timeout cleanup. AS-04 closes authorization at SSE admission; it does not replace the AS-06 resource-lifecycle work.
