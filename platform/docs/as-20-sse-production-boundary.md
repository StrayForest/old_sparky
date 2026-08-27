# AS-20 legacy SSE production boundary

Status: historical legacy contour; superseded by the Ready Check boundary on
2026-08-27.

## Deployed protection

The measured legacy package was commit
`b86ec1e6937184e5de698ad4e9258a9df4a0d792`. Its security/build run was
`33055132124`, automatic deploy `33055567733` and production deploy/live smoke
`33055574062`.

The package retained:

- signed short-lived SSE admission tickets;
- anonymous tickets only for public bracket data;
- session-bound private tickets and periodic fail-closed session,
  participant, organizer and admin revalidation;
- Redis global/source/user leases at `3,000/32/4`, with Nginx physical
  source/global ceilings at `10,240`;
- one shared Redis relay and bounded sequence buffer per worker/tournament;
- no artificial 600-second stream rotation; keepalive and renewable lease
  checkpoints keep healthy streams open;
- immediate conditional polling fallback and full-jitter reconnect recovery.

Making public tickets anonymous does not add access: public bracket data is
already available to anonymous viewers. It removes unnecessary user lease and
session SQL work. The global/source limits, signed ticket, tournament
visibility revalidation and all private authorization checks remain active.

## Evidence

| Contour | Evidence | Result |
| --- | --- | --- |
| Public hold >600s | load `33039877619`, cleanup `33040449804` | 32/32 HTTP 200, 1,280 keepalives, 96/96 events, 0 errors; connect/event p95 144/207ms |
| Origin-local 5k | load `33058453000`, cleanup `33058694392` | 5,000/5,000; 15,000/15,000 events; 0 errors; connect/event p95 163ms/1.02s |
| Origin-local 7.5k | load `33058742383`, cleanup `33059036901` | 7,500/7,500; 22,500/22,500 events; 0 errors; connect/event p95 241ms/1.34s |
| Origin-local 10k | load `33059084508`, cleanup `33059419678` | 10,000/10,000; 30,000/30,000 events; 0 errors; connect/event p95 334ms/4.38s |
| Public gradual fill 25/s | load `33040985301`, cleanup `33041234047` | 3,000/3,000; N+10 429; 0 errors; connect/event p95 534ms/1.63s |
| Public gradual fill 40/s | load `33041292794`, cleanup `33041492597` | 3,000/3,000; N+10 429; 0 errors; connect/event p95 2.99/1.79s |
| Post-deploy public ticket validation | load `33043458814`, cleanup `33043714003` | 3,000/3,000; N+10 429; 0 errors; connect/event p95 448ms/2.39s |
| Public gradual fill 50/s | load `33040501257`, cleanup `33040712758` | 2,154/3,000; 846 edge/open timeouts; 0 503/429/application errors |
| Mixed 10k at 50/s | load `33041564210`, cleanup `33041917423` | 10,000/10,000 polling, but 2,209/3,000 SSE and 791 edge/open timeouts |
| Mixed 10k at 25/s | load `33041962825`, cleanup `33042299928` | 10,000/10,000 polling, 3,000/3,000 SSE, 9,000/9,000 events, 0 errors |
| Mixed 10k + 1,500 SSE | load `33060139677`, cleanup `33060670146` | 10,000/10,000 polling, 1,500/1,500 SSE, 4,500/4,500 events, 0 errors; API p95 3.02s |
| Mixed 10k + 2,000 SSE | load `33057581594`, cleanup `33058336066` | 10,000/10,000 polling, 2,000/2,000 SSE, 6,000/6,000 events, 0 errors; API p95 3.85s |
| Mixed 10k + 2,400 SSE | load `33059477619`, cleanup `33060078050` | 10,000/10,000 polling, 2,400/2,400 SSE, 7,200/7,200 events, 0 errors; API p95 3.73s |

The exact cleanup runs removed every synthetic user/tournament/session/audit
row and preserved the control account. No fixture data remains from these
runs.

## Boundary and next action

The origin-only QA mode supports a bounded ceiling of 30,000 for controlled
loopback experiments, but the highest opened point remains 20,000 SSE because
the cgroup peak was already about 985MB near the API's roughly 1GB memory
boundary. The customer-facing protected point is 3,000 established public SSE.
Public opening is safe at 25/s; 40/s is a high-latency diagnostic point and
50/s is rejected by edge/open timeouts even though the application emits no
unexpected errors.

The 10k-user mixed profiles at 1,500, 2,000 and 2,400 SSE were functionally
successful, but all flagged ordinary API CPU/load and PostgreSQL connection
pressure; none is a clean low-latency production ceiling. The highest current
functional contour is 2,400, not a recommendation to raise the application
cap. A VPS/Cloudflare resource change, followed by the same exact
load/cleanup matrix, is required before claiming 10,000 public persistent SSE
or the exact 180% two-core target.

The failure/recovery matrix on the active SHA covered worker restart, full API
restart, Redis hiccup, Nginx reload and mass disconnect. Polling remained
available and reconnects were jittered. Direct Cloudflare outage injection and
concurrent deploy-under-load were not performed because no safe external-edge
fault injector is present in the repository; the live deployment smoke and
client-side mass-disconnect fallback remain verified.
