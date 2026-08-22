# Deadlock Workflow Invariants

- Profile-level dream-team slots are the source of truth.
- Dream-slot replace-all writes lock the owning user/profile and leave exactly
  one request payload; slots stay within the supported range.
- Tournament-scoped dream-slot API/model/table must not return.
- Ready-check, captain round, assignment run, roster publish, and roster lock state stay scoped to eligible participants and organizers.
- Every durable workflow writer — route, automation and worker — locks the
  tournament row first, revalidates lifecycle state under that lock and uses a
  documented secondary-row lock order. Redis is advisory coalescing only.
- The database prevents ambiguous cardinal workflow rows: one active
  ready-check and one canonical captain/assignment/published-or-locked roster
  state as defined by the model. Application checks are not the final guard.
- A ready vote may persist only for an active round and eligible active
  participant; closing or excluding cannot race a late vote into persistence.
- Withdrawn or disqualified participants are excluded from ready-check eligibility and downstream captain/assignment inputs.
- Registration, ready confirmation, and captain candidacy do not make a player globally unavailable.
- A locked roster creates one active `player_tournament_commitments` row per roster member. The partial unique index on `user_id` is the final concurrency guard: one player cannot have two active team commitments.
- Roster locking must lock candidate user rows in stable `user_id` order, re-read active commitments, and rebuild the assignment from the remaining ready players before commitments are inserted.
- If the globally available ready pool cannot fill every complete team, roster locking fails. Never publish a partial team to work around an unavailable player.
- A single-elimination loss releases the losing team's commitments. Tournament completion, cancellation, withdrawal, and disqualification release the applicable commitments as part of the same transaction.
- Reporting the valid final score completes the tournament and releases all remaining commitments. Reopening a completed non-final match must reactivate the eliminated team atomically or fail if any player is committed elsewhere.
- `player_tournament_commitments` is the source of truth for current global availability. Historical roster JSON is not a busy-state cache and must not be rewritten merely because a commitment is released.
- The periodic reconciliation job may repair terminal, eliminated, or roster-mismatched commitments, but normal write paths remain responsible for synchronous release.
- Auto-assignment remains bounded and deterministic.
- Starter balance is based on captain plus five starters; reserve does not affect spread, MAD, STD, or seeding.
- Slot roles are hard constraints; hero preferences are soft quality signals.
- Locked roster is the handoff into match creation and bracket progression.
- Terminal tournament states freeze organizer match administration.
