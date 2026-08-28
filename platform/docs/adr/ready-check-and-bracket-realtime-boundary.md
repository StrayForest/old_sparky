# Ready Check and bracket realtime boundary

- Status: Accepted
- Owner: Platform maintainers
- Date: 2026-08-28

## Decision

The public tournament catalog is request-driven. `/tournaments` does not open
realtime connections or run a background poller: navigation, filter changes,
pagination and explicit user actions issue ordinary requests.

Ready Check no longer uses a realtime transport. A Ready Check has a
server-known `starts_at` and `ends_at`, so keeping a connection open only to
announce a deterministic timestamp is unnecessary. The tournament workspace
response carries the schedule, the eligible/current-user state and a UTC
`server_time` anchor in the same initial HTTP payload.

The browser derives an estimated server clock from that anchor and elapsed
monotonic time (`performance.now()`). It enables the Ready action locally at
`starts_at`, disables it locally at `ends_at`, recomputes after timer wake-up,
visibility restoration and `pageshow`, and makes no Ready Check request for
any of those transitions. There are no Ready Check EventSource clients,
polling loops, preconnects, leases, admission slots or capacity overrides.

The browser clock is a presentation aid only. `POST
/tournaments/{slug}/deadlock/ready-check/vote` validates the authenticated
participant, schedule boundaries, eligibility, tournament/workflow state,
duplicate/concurrent writes and the current server time. A delayed automation
worker cannot make a valid vote at or after `starts_at` fail: the vote path
locks the tournament row and can materialize the active round itself. The
worker remains responsible for persistence, no-show/timeout processing and
later workflow side effects.

## Client and read contracts

The initial workspace payload is authoritative for the client timeline:

```text
server_time + starts_at + ends_at
                  |
                  v
      monotonic local timer, no request
                  |
                  v
             one authoritative vote POST
```

Loading before the window shows a disabled waiting action. Loading during the
window shows an active action immediately. Loading after the window shows an
expired action. Reloads and browser restarts simply receive a new server-time
anchor; no timer state is persisted.

`GET /tournaments/{slug}/deadlock/ready-check` remains a legitimate explicit
state read for the tournament workspace, organizer/admin views and recovery
flows. The web client does not poll it to discover `starts_at`, and no Ready
Check endpoint is used as an SSE fallback. The vote response is authoritative
for the post-vote UI, so the client does not issue a follow-up GET.

## Bracket boundary

The bracket grid is request-driven. The tournament workspace carries the full
bracket in its initial HTTP payload, and the browser does not open an
EventSource, run a revision probe or poll while the page is open. A user action
that changes a bracket may refetch the authoritative response needed to render
the result; passive changes become visible after the user manually reloads the
page. There are no bracket connection limits, leases, tickets, admission
windows or relay infrastructure in the active product path. Redis remains
available to unrelated platform services.

## Verification and load model

Ready Check SSE capacity staircases are retired and are not release gates.
The replacement load test is a short-lived Ready vote burst: eligible users
load the tournament workspace, wait on the local timeline, then submit votes
with a human-shaped distribution and a separate aggressive burst. It records
vote POST p50/p95/p99, accepted/rejected reasons, duplicate/idempotency
behavior, database pool wait and locks, API/PostgreSQL CPU and connections,
and cleanup completeness.

Historical AS-19/AS-20 SSE benchmark results remain evidence of the former
transport and generic/bracket origin behavior. They do not describe the
current Ready Check production architecture and do not authorize a Ready
Check SSE capacity claim or cap change.
