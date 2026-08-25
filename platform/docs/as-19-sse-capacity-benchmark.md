# AS-19 — SSE capacity and combined-load benchmark

- Status: Active measurement plan and controlled limit experiment
- Owner: Platform maintainers
- Started: 2026-08-25

## Objective

Measure the deployed public bracket SSE behavior separately from the existing
10,000-user browser-polling gate, then measure both paths together. The test
must distinguish a deliberate admission response (`429`) from an application
failure (`5xx`, unexpected disconnect or client error) and must leave no
synthetic data behind.

The previous deployed contour was application global `128`, Nginx
`8/source + 160/global`, and Nginx `worker_connections=768`. That contour
rejected the 1,000- and 5,000-attempt probes before they tested application
capacity. The reviewed capacity candidate raises the global ceilings to
application `10,240` global / `32/source`, Nginx `10,240/source + 10,240/global`, and
`worker_connections=32,768`, while retaining a per-user cap of `4` and
fail-closed Redis admission. The production load runner uses a signed,
secret-derived QA header to bypass only the application per-source bucket;
global, per-user, Redis fail-closed and Nginx limits remain active. Ordinary
clients never receive this bypass. These are capacity ceilings, not a claim
that a single VPS can sustain 10,000 streams; the claim is made only after the
staircase, combined run and resource evidence pass.

## Ordered protocol

1. Deploy the reviewed runner and limit candidate through the normal `dev`
   security/build and automatic production deployment chain. If the core
   matrix passes, run a separate reviewed overload extension above 10,000 to
   measure headroom and deliberate admission, without treating a deliberate
   `429` as a successful 10,000-user result.
2. Run the ten hypotheses below one at a time. Clean each run before starting
   the next one; a failed or canceled run is still cleaned.
3. Select the best hypothesis by zero unexpected errors/503s first, then
   admission success, event delivery, p95/p99 latency, CPU/RAM/FD headroom,
   PostgreSQL pool/lock wait and Redis backlog.
4. Run the five follow-up hypotheses around the selected winner.
5. Select the final profile using the same ordering and record the evidence.
6. Run the combined profile with 10,000 polling users, 10,000 virtual tabs and
   the configured SSE target. Polling uses the selected active/passive profile;
   SSE uses the same retained fixture and a bounded event probe.
7. Clean every retained run with its own exact cleanup workflow run ID. A
   canceled or failed measurement is still cleaned and is never reported as a
   successful capacity result.
8. Record the staircase and combined conclusions here and in `CURRENT.md`.

## Metrics and interpretation

The runner records connection attempts, connected streams, `429`/`503`/other
responses, connection latency, keepalives, reconnects, bytes, event delivery
latency, polling status/304 counters, HTTP p50/p95/p99, request-performance
logs, CPU/RAM/load, PostgreSQL connections and lock waits, and worker/backlog
signals where available.

The Redis messages are explicitly transport/fan-out probes with type
`qa_sse_probe`; they do not mutate authoritative tournament state. A successful
probe demonstrates delivery through the bracket event channel, not correctness
of a tournament workflow writer. Workflow mutation load remains owned by the
existing write-burst and browser-polling profiles.

Expected admission behavior for the new target is no unexpected rejection up
to the selected capacity ceiling. Any unexpected `5xx`, `503`, client error,
lease-release failure or sustained resource saturation is a failed measurement
requiring diagnosis. A `429` is only acceptable when the tested variant is
deliberately above a configured ceiling; it is not a successful 10,000-user
result.

## Ten hypotheses

These are executed against the same retained fixture and public route. The
first three establish the staircase; the remaining variants isolate opening
burst, hold time, fan-out and mixed polling effects.

| ID | Variant | SSE target | Hold | Open concurrency | Probe |
| --- | --- | ---: | ---: | ---: | --- |
| H1 | 1k staircase baseline | 1,000 | 60s | 256 | 3 events / 1s |
| H2 | 5k staircase baseline | 5,000 | 60s | 512 | 3 events / 1s |
| H3 | 10k gradual baseline | 10,000 | 60s | 512 | 3 events / 1s |
| H4 | 10k faster opening | 10,000 | 60s | 1,024 | 3 events / 1s |
| H5 | 10k bounded opening | 10,000 | 60s | 256 | 3 events / 1s |
| H6 | 10k long hold | 10,000 | 180s | 512 | 3 events / 1s |
| H7 | 10k fan-out burst | 10,000 | 60s | 512 | 20 events / 0.25s |
| H8 | 10k high fan-out opening | 10,000 | 60s | 1,024 | 20 events / 0.25s |
| H9 | 10k reconnect pressure | 10,000 | 60s | 512 | 1 reconnect, 3 events / 1s |
| H10 | 10k combined workload | 10,000 | 60s | 512 | 3 events / 1s + polling |

H10 uses `profile=combined`, a 30-second polling window and the established
300-second opening stagger. The other rows use `profile=sse`. H1/H2 are
diagnostic staircase points, not evidence that the server is limited to those
sizes.

## Five follow-up hypotheses

After selecting `Hn`, run five nearby variants rather than changing several
dimensions at once:

| ID | Follow-up change around `Hn` |
| --- | --- |
| F1 | halve the SSE opening concurrency |
| F2 | double the SSE opening concurrency, capped at 2,048 |
| F3 | double the hold duration |
| F4 | double probe event rate while keeping the opening shape |
| F5 | repeat the winner with one forced reconnect cycle |

The final winner is the highest-capacity variant with zero unexpected errors
and measurable headroom. If two variants tie, prefer the one with lower p95,
lower CPU and fewer Redis/DB waits. No value is accepted only because it
allows the client generator to create more tasks.

## Operator commands

Run from the reviewed `dev` branch in a low-traffic window:

```bash
gh workflow run platform-production-retained-load-matrix.yml \
  --repo StrayForest/old_sparky --ref dev \
  -f confirmation=RUN-PRODUCTION-RETAINED-LOAD-MATRIX \
  -f control_email=aleksei.lisitsin1@gmail.com \
  -f concurrency=80 -f profile=sse \
  -f sse_connections=1000 -f sse_users_per_tournament=50
gh run watch <load-run-id> --repo StrayForest/old_sparky --exit-status
```

Use `sse_connections=5000` with `sse_users_per_tournament=250`, then
`sse_connections=10000` with `sse_users_per_tournament=500`. Set
`sse_open_concurrency`, `sse_duration`, `sse_event_count`,
`sse_event_interval` and `sse_reconnect_cycles` per the hypothesis table. For
the combined run use `profile=combined`, `sse_users_per_tournament=500`, and
the reviewed polling/SSE durations. Always run:

```bash
gh workflow run platform-production-retained-load-cleanup.yml \
  --repo StrayForest/old_sparky --ref dev \
  -f confirmation=DELETE-PRODUCTION-RETAINED-LOAD \
  -f load_run_id=<load-run-id> \
  -f control_email=aleksei.lisitsin1@gmail.com
gh run watch <cleanup-run-id> --repo StrayForest/old_sparky --exit-status
```

If the load workflow is canceled while the SSH step is active, use the exact
abort workflow first. The cleanup/recovery path accepts `sse` and `combined`
manifests and still validates marker, report path, synthetic ownership,
tournament graph and control-account preservation before deletion.

## Results

Results are added after the CI/deploy gate and each production run. Detailed
JSON and bounded logs remain in the GitHub artifacts/VPS run root; this document
stores only the measured conclusion and run identifiers.

- Limit candidate CI/deploy: `32819954714` / production `32820833942` passed for
  `da1435c`; the signed QA-bypass and failed-run summary fix are pending their
  own CI/deploy gate.
- Ten-hypothesis matrix: pending.
- Follow-up matrix: pending.
- Combined 10,000-user SSE + polling gate: pending.
- Diagnostic H1 attempt `32823477661` did not reach a valid SSE result: the
  setup completed, but the application source bucket held at 32 (`connected=171`,
  `max_active=32`, `rejected_other=829`, `errors=149`, no events). It was
  cleaned by `32823765006`: 1,000 users and 20 tournaments deleted, zero fixture
  users/tournaments/sessions/audit rows remained, and the control account was
  preserved. This run is excluded from hypothesis ranking.
