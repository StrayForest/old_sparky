# AS-19 — SSE capacity and combined-load benchmark

- Status: Active measurement plan
- Owner: Platform maintainers
- Started: 2026-08-25

## Objective

Measure the deployed public bracket SSE behavior separately from the existing
10,000-user browser-polling gate, then measure both paths together. The test
must distinguish a deliberate admission response (`429`) from an application
failure (`5xx`, unexpected disconnect or client error) and must leave no
synthetic data behind.

The current application global SSE admission limit is 128 connections. Nginx
has a separate global/source contour. The benchmark therefore does not claim
that 10,000 persistent SSE streams are supported when the target is rejected;
it measures the actual admission boundary and resource behavior.

## Ordered protocol

1. Deploy the reviewed runner through the normal `dev` security/build and
   automatic production deployment chain.
2. Run SSE-only with 1,000 attempts and 50 synthetic users per tournament.
3. Run SSE-only with 5,000 attempts and 250 synthetic users per tournament.
4. Run SSE-only with 10,000 attempts and 500 synthetic users per tournament.
5. After each run, inspect the compact report and server bottleneck evidence.
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

Expected admission behavior for an attempt target above 128 is connected
streams at or below the application cap plus explicit `429` responses. Any
unexpected `5xx`, `503`, client error, lease-release failure or sustained
resource saturation is a failed measurement requiring diagnosis. The best
profile is selected by correctness and cleanup first, then latency, resource
headroom, pool wait, lock wait, backlog and fan-out delivery—not by raw attempt
count.

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
`sse_connections=10000` with `sse_users_per_tournament=500`. For the combined
run use `profile=combined`, `sse_users_per_tournament=500`, and the reviewed
polling/SSE durations. Always run:

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
