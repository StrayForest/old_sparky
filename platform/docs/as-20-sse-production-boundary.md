# AS-20 SSE production boundary

Status: closed at the current production contour on 2026-08-27.

## Deployed protection

The verified package is commit
`0c6f0ae369b5dad935e2cc4b8123b5480aabf326`. Its security/build run was
`33042821250`, automatic deploy `33043138900` and production deploy/live smoke
`33043143157`.

The package retains:

- signed short-lived SSE admission tickets;
- anonymous tickets only for public bracket data;
- session-bound private tickets and periodic fail-closed session,
  participant, organizer and admin revalidation;
- Redis global/source/user leases at `3,000/32/4`, with Nginx physical
  source/global ceilings at `10,240`;
- one shared Redis relay and bounded sequence buffer per worker/tournament;
- no artificial 600-second stream rotation; keepalive and renewable lease
  checkpoints keep healthy streams open;
- immediate conditional polling fallback and full-jitter SSE recovery at
  `60–180s`.

Making public tickets anonymous does not add access: public bracket data is
already available to anonymous viewers. It removes unnecessary user lease and
session SQL work. The global/source limits, signed ticket, tournament
visibility revalidation and all private authorization checks remain active.

## Evidence

| Contour | Evidence | Result |
| --- | --- | --- |
| Public hold >600s | load `33039877619`, cleanup `33040449804` | 32/32 HTTP 200, 1,280 keepalives, 96/96 events, 0 errors; connect/event p95 144/207ms |
| Origin-local cap | load `33040760791`, cleanup `33040911848` | 3,000/3,000 HTTP 200; N+10 probe 10 expected 429; 0 503/timeouts/errors; 9,000/9,000 events |
| Public gradual fill 25/s | load `33040985301`, cleanup `33041234047` | 3,000/3,000; N+10 429; 0 errors; connect/event p95 534ms/1.63s |
| Public gradual fill 40/s | load `33041292794`, cleanup `33041492597` | 3,000/3,000; N+10 429; 0 errors; connect/event p95 2.99/1.79s |
| Post-deploy public ticket validation | load `33043458814`, cleanup `33043714003` | 3,000/3,000; N+10 429; 0 errors; connect/event p95 448ms/2.39s |
| Public gradual fill 50/s | load `33040501257`, cleanup `33040712758` | 2,154/3,000; 846 edge/open timeouts; 0 503/429/application errors |
| Mixed 10k at 50/s | load `33041564210`, cleanup `33041917423` | 10,000/10,000 polling, but 2,209/3,000 SSE and 791 edge/open timeouts |
| Mixed 10k at 25/s | load `33041962825`, cleanup `33042299928` | 10,000/10,000 polling, 3,000/3,000 SSE, 9,000/9,000 events, 0 errors |

The exact cleanup runs removed every synthetic user/tournament/session/audit
row and preserved the control account. No fixture data remains from these
runs.

## Boundary and next action

The highest verified origin-only point remains 20,000 SSE, near the API
service's roughly 1GB memory limit. The customer-facing protected point is
3,000 established public SSE. Public opening is safe at 25/s; 40/s is a
high-latency diagnostic point and 50/s is rejected as an opening profile even
though the application itself does not emit unexpected errors.

The 10k-user mixed profile is functionally safe at 3,000 SSE plus 10,000
polling users when openings are gradual. Its remaining pressure is API CPU,
event latency and PostgreSQL connection peak, not Redis admission failure or
lock contention. The application cap must not be raised until an operator-owned
Cloudflare/transport or VPS resource change is made and the same exact
load/cleanup matrix is repeated. Ten-thousand public persistent SSE and the
exact 180% two-core target are not claimed by this evidence.
