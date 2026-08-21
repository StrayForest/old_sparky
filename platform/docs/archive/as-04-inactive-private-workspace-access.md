# AS-04 inactive private-workspace authorization — closed

- Status: Archived / resolved
- Closed: 2026-08-21
- Initial implementation commit: `1405fdbaeaa7c66b974b782a26eb3cc81c5ebf6b`
- Initial regression-test commit: `492c3cefe65fbc7b1f9995612a6f3572699dfdc8`
- Initial CI verification run: `32455353984`
- Initial production deployment run: `32455553444`
- Initial production release: `gha-32455553444-1-492c3cefe65f-20260821T064427Z`
- Authorization hardening commit: `7f4cdd681f7698dda568f1117c79c9a57b81589d`
- Hardening CI verification run: `32457165401`
- Hardening production deployment run: `32457381384`
- SSE revocation implementation commit: `81d3b27730cb5ec2d99b0eacb89de9a5a203e597`
- SSE compatibility follow-up commit: `51e638c3606fba362259b3b49a2ac183eae16ce6`
- Final AS-04 CI verification run: `32460086766`
- Final AS-04 production deployment run: `32460277054`
- Participant-lifecycle hardening commit: `cba27c7be7c7ca068499bd30395a1b70940f1a25`
- Participant-lifecycle CI verification run: `32463218557`
- Participant-lifecycle production deployment run: `32463433650`
- Current verified release containing the AS-04 lifecycle hardening: `gha-32463433650-1-cba27c7be7c7-20260821T083045Z`

## Original finding

Private invite-only tournament reads treated the existence of any `TournamentParticipant` row as current membership. Because withdrawn and disqualified rows are intentionally retained for history/audit, those inactive rows could continue satisfying private-workspace authorization after active participation ended.

A follow-up review also found a long-lived variant: a participant who opened private bracket SSE while active could keep that already-open connection after later withdrawal or disqualification because authorization was checked only when the stream was admitted.

## Remediation delivered

- Historical participant rows remain stored for audit/history but no longer act as private-read membership.
- Participant membership is fail-closed: only the explicitly active statuses `registered`, `confirmed` and `checked_in` grant participant membership for invite-only tournament child reads.
- `withdrawn`, `disqualified` and any future/unclassified participant status are rejected with `403` until that status is deliberately classified as active.
- The authorization guard is mounted once on the tournament router and derives scope from FastAPI's matched route metadata and parsed `{slug}` path parameter. It does not maintain a hard-coded list of private endpoint suffixes.
- Any current or future `GET /tournaments/{slug}/...` child route therefore enters the same inactive-participant guard automatically. The tournament summary `GET /tournaments/{slug}` and collection/static tournament routes remain outside this participant-membership guard and retain their existing visibility rules.
- Existing route-specific authorization remains authoritative for non-members and for business-specific permissions. This guard specifically prevents retained or unclassified participant records from being interpreted as active membership.
- Organizer and platform admin/superadmin authority remain explicit and independent from participant membership.
- Private bracket SSE now carries request-local authorization context and revalidates active participant membership against current database state before each next bracket event or keepalive. If an active participant becomes withdrawn, disqualified or otherwise inactive after connecting, the stream terminates before further private data is emitted.
- If request-local stream context is unavailable, an existing invite-only tournament fails closed; public tournament stream behavior remains available after a visibility lookup.
- Public-tournament behavior is otherwise unchanged.

## Participant lifecycle follow-up

A post-fix review found that the legacy organizer remove endpoint physically deleted `TournamentParticipant` while an already-issued `TournamentInviteAccess` row could remain. That destroyed the inactive-participant tombstone used to prevent self-rejoin and could allow a removed participant to join the same invite-only tournament again using retained invite access.

Commit `cba27c7be7c7ca068499bd30395a1b70940f1a25` closes that lifecycle gap:

- organizer participant removal now retains the participant row and changes it to `disqualified` instead of physically deleting it;
- the existing compatibility `DELETE /tournaments/{slug}/participants/{participant_id}` still returns `204`, but the destructive handler is intercepted before deletion and the retained row records moderation actor/time;
- transition to an inactive status still removes the participant from active ready/captain workflow and releases only that tournament's active player commitment;
- a `disqualified` user cannot redeem an invite for the same tournament, and the rejection happens before invite access or `use_count` can be mutated;
- the retained row therefore also continues to block self-join through the existing duplicate-participant check;
- the normal participant roster continues to hide inactive records, while organizer-only `GET /tournaments/{slug}/participants/manage` exposes the full roster so the organizer can deliberately restore the participant through the existing moderation transition;
- exclusion is scoped to one tournament. The regression test proves the same player can still accept an invite and join a different tournament.

This follow-up does not close or weaken AS-03. Invite-use and participant-capacity serialization under concurrent requests remains a separate P1 correctness/security item.

## Verification

The AS-04 authorization hardening passed:

- unit coverage proving route matching is derived from the matched route rather than a suffix allowlist, including a hypothetical future tournament child route;
- fail-closed status coverage proving `withdrawn`, `disqualified` and an unknown example status are denied while `registered`, `confirmed` and `checked_in` remain active;
- summary/collection-route tests proving unrelated routes do not gain extra database work;
- organizer/admin and public/non-member regressions;
- an API-level integration regression that performs invite claim + join, transitions users to `withdrawn` and `disqualified`, then proves `403` across workspace, roster, matches, bracket, bracket SSE, invite management reads, Deadlock ready-check/captain/auto-assignment state reads and tournament-scoped profile reads while organizer access remains available;
- stream-level regression coverage proving a connection admitted while active stops before the next private event after authorization is revoked, denied access does not subscribe to Redis, and an authorized stream still emits events and cleans up normally;
- the complete backend unit/integration suite, including the existing direct Redis bracket-stream coverage, and static/dependency security gates;
- frontend audit/typecheck/lint/production build and Playwright smoke;
- production preflight, immutable release build/checksum/install, Alembic-head validation, service restart checks, origin smoke and public smoke with enforced CSP.

The first SSE follow-up run exposed a compatibility regression in the low-level test helper that streams a synthetic nonexistent tournament ID directly, outside the HTTP authorization path. That regression blocked deployment, was corrected without weakening authorization for any existing private tournament, and the replacement commit `51e638c3606fba362259b3b49a2ac183eae16ce6` passed the full CI contour.

The participant-lifecycle follow-up added an end-to-end integration regression covering organizer removal, retained `disqualified` state, hidden active roster vs organizer management roster, same-tournament invite rejection without extra invite use, same-tournament rejoin rejection, private-workspace denial, successful participation in an unrelated tournament and explicit organizer restoration. Security/build run `32463218557` passed the complete backend suite (`643` tests, `1` skipped), Ruff, Bandit, `pip-audit`, secret scanning, frontend audit/typecheck/lint/build and Playwright smoke.

Production deployment run `32463433650` installed exact commit `cba27c7be7c7ca068499bd30395a1b70940f1a25` as release `gha-32463433650-1-cba27c7be7c7-20260821T083045Z`. Alembic remained at head `20260813_0038`; `deadlock-api`, `deadlock-worker`, `deadlock-web` and Nginx were active; database, origin, public, CSP and SSE deployment smoke passed.

No Cloudflare or Turnstile control was weakened, and no production challenge/security bypass was introduced for testing.

## Remaining scope

AS-03 separately owns concurrency-safe serialization of invite use and participant-capacity check/write operations. AS-06 separately owns SSE connection-pressure limits, bounded long-lived connection counts and general disconnect/timeout resource cleanup. AS-04 owns private-workspace authorization and the retained participant lifecycle required to prevent an organizer exclusion from being bypassed by self-rejoin.
