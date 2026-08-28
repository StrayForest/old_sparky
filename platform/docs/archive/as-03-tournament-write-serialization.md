# AS-03 tournament write serialization — closed

- Status: Archived / resolved
- Closed: 2026-08-21
- Implementation commit: `5a8604febfe6506e406bbc2fc04031cd3fb34652`
- Concurrency-test follow-up commit: `0064c44cdc13f676ccfa201c02110969cdc1e072`
- Security/build verification run: `32467428286`
- Production deployment run: `32467677414`
- Verified production release: `gha-32467677414-1-0064c44cdc13-20260821T092444Z`
- Alembic head: `20260813_0038` (unchanged)

## Original finding

Invite redemption and tournament participant-capacity decisions used read-check-write sequences without a stable database serialization point. Two concurrent requests could therefore observe the same remaining invite use or participant slot and both proceed.

The original audit named invite claim, self-join and organizer participant-add. Review against the participant lifecycle introduced by AS-04 identified two additional edges that belong to the same invariant:

- invite revocation must serialize with claim so a claim cannot race a concurrent revoke against stale invite state;
- restoring a retained `withdrawn` or `disqualified` participant to an active status is a capacity increase and must not reactivate a player after the last slot has already been taken.

The restoration case was also a sequential correctness issue: a slot can be freed by making a participant inactive, filled by another player, and then overfilled if the original participant is restored without rechecking capacity.

## Decision

PostgreSQL row-level locking is the owner of this invariant because PostgreSQL is already authoritative for tournament, invite and participant state and the checks and writes must share one transaction.

The selected lock model is:

1. invite claim/revoke locks the stable tournament row and then the specific invite row with `FOR UPDATE`;
2. participant-count mutations serialize on the stable tournament row with `FOR UPDATE`;
3. after the tournament lock is held, the normal route/service logic performs its existing eligibility and capacity checks and writes in the same database transaction;
4. reactivation of an inactive retained participant explicitly recounts active participants and rejects restoration with `409` when `max_participants` is already reached.

Tournament-to-invite lock ordering is consistent for invite claim and revoke, reducing deadlock risk when both resources are required.

## Alternatives rejected

- **Redis/distributed lock:** rejected because it would create a second concurrency authority beside the PostgreSQL transaction and would require failure/lease semantics that the durable write itself does not need.
- **Application-process mutex:** rejected because Gunicorn workers/processes do not share an in-process lock and production can scale beyond one process or node.
- **Retry-only/optimistic recheck:** rejected because the aggregate capacity invariant has no conflicting row write that PostgreSQL could reliably detect under the current schema; two requests can both read the same count before inserting different participant rows.
- **Database CHECK constraint for participant count:** not practical because `max_participants` is an aggregate across participant rows, not a same-row constraint. Existing unique constraints remain valuable secondary protection for duplicate participant and invite-access rows.
- **Serializable isolation for every request:** broader and more expensive than necessary; stable row locks provide a narrow serialization point for the exact tournament/invite invariants.

No schema migration was required.

## Remediation delivered

A router-level authenticated tournament write guard now owns the stable serialization points before the affected route handlers execute.

Covered invite mutations:

- `POST /tournaments/invites/claim`;
- `DELETE /tournaments/{slug}/invites/{invite_id}`.

Covered participant-capacity mutations:

- self-service join and leave;
- organizer participant add/remove;
- organizer moderation transitions, including inactive-to-active restoration.

The guard does not grant authorization. Existing route-level organizer/user permissions and the AS-04 participant-lifecycle policy remain authoritative. Organizer mutation requests perform an ownership snapshot before taking the tournament row lock so unrelated authenticated users cannot intentionally lock another organizer's tournament through management endpoints.

Existing database uniqueness constraints remain in place as defense in depth.

## Verification

The PostgreSQL integration coverage is deterministic rather than timing-only:

- **last invite use:** a blocking transaction holds the invite row while two separate authenticated clients attempt to claim the final use; after release, exactly one request returns `201`, the other returns `409`, `use_count` is exactly one and only one invite-access row exists;
- **last participant slot:** a blocking transaction coordinates the tournament row and participant inserts while self-join and organizer-add compete for a tournament with `max_participants=1`; after release, exactly one request returns `201`, the other returns `409`, and exactly one active participant exists;
- **inactive restoration:** an inactive retained participant cannot be restored when another player has filled the freed slot; the restoration returns `409` and active participant count remains at the configured maximum.

Security/build run `32467428286` passed the complete backend suite (`646` tests, `1` skipped), including all three AS-03 concurrency regressions, plus Ruff, Bandit, `pip-audit`, secret scanning, frontend audit/typecheck/lint/build and Playwright smoke.

Production deployment run `32467677414` installed exact commit `0064c44cdc13f676ccfa201c02110969cdc1e072` as release `gha-32467677414-1-0064c44cdc13-20260821T092444Z`. The deploy used a restore-verified backup, retained Alembic head `20260813_0038`, restarted the API/worker/web services successfully and passed origin, public, CSP and deployment smoke.

No Cloudflare, Turnstile, authentication or application-RBAC control was weakened.

## Residual risk and follow-up

The invariant deliberately serializes concurrent writes for one tournament on one small PostgreSQL row. That is the intended correctness tradeoff; future optimization should be driven by measured lock-wait/throughput evidence rather than replacing the durable transaction boundary speculatively.

AS-05 now owns the next application P1 implementation package, while AS-02 remains operator-owned Cloudflare Access/MFA verification.
