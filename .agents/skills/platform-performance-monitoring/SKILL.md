---
name: platform-performance-monitoring
description: Use for Deadlock Platform performance monitoring, load-test instrumentation, request_perf analysis, production/preprod bottleneck reports, Prometheus/Grafana decisions, and step-by-step site workflow observability.
---

# Platform Performance Monitoring

Use this skill when adding or interpreting platform monitoring around user
journeys, production QA, load tests, endpoint latency, SQL/request counts,
server saturation, PostgreSQL pressure, worker backlog, or frontend request
waterfalls.

## Workflow

1. Define the user step, not only the endpoint: public browse, tournament
   detail, registration, ready-check, team formation, bracket view, admin
   dashboard, or cleanup.
2. Capture the full request path for that step:
   - frontend route and server component/client component behavior;
   - API endpoints called by the step;
   - domain/service functions used by those endpoints;
   - SQL count, DB time, compute time, max SQL time, payload size when
     available.
3. Compare client and server evidence:
   - QA `performance.http_client.by_phase` for user-step latency;
   - QA `performance.server_request_perf_logs.by_route` for backend route
     time and SQL shape;
   - QA `performance.system` for CPU, RAM, swap, load average, nginx
     connections, gunicorn workers, PostgreSQL CPU/connections;
   - `pg_stat_statements` and bounded `EXPLAIN` for DB-heavy paths.
4. Treat repeated UI polling, duplicated SSR/API reads, JSONB deserialization,
   serialization, and large list payloads as first-class bottlenecks even when
   SQL count is already low.
5. For tournament pages, keep detail, bracket shell, compact bracket, and full
   roster bracket profiles separate. Current frontend bracket SSR should use
   `workspace_view=bracket_summary&participants_limit=0`, then compact
   `/bracket?teams_view=summary`; full roster reads are diagnostic/admin paths.
6. Check write amplification on high-volume mutations. Do not add per-user
   audit-log writes when an authoritative domain table already records the
   same workflow fact.
7. For counter-backed hot paths such as Deadlock ready votes, check whether
   aggregate scans were replaced by row-level lock contention: compare route
   DB time, max SQL time, `postgres_waits.max_lock_waiters`,
   `max_ungranted_locks`, `max_lock_waiting_query_ms`, generic backend waits,
   and p95 under concurrency.
8. For join, ready-vote, or organizer mutation work, use the retained
   `platform_production_qa.py --mode write-burst` shape. Compare target phases
   through `write_burst.profiles[].server`; do not use setup-inclusive route
   aggregates as mutation p95 evidence.
9. Distinguish resource inventory from bottlenecks:
   - a short CPU peak is not sustained saturation;
   - a connection peak is not pool pressure without meaningful active waits;
   - an observed short lock wait is not lock contention unless an ungranted
     lock or a long wait confirms it.
10. Optimize one bottleneck class per code package unless the user explicitly
   asks for a combined final package. Preserve tournament visibility,
   permissions, workflow invariants, and platformdb isolation.
11. After changes, update docs with before/after metrics and the remaining
   ceiling: SQL, CPU, DB pool, frontend waterfall, payload, or worker queue.
12. For retained JSON reports, extract compact tables with a parser instead of
    reading or pasting whole reports. Include report paths for drill-down.

## Instrumentation Rules

- Prefer existing lightweight instrumentation first:
  `PLATFORM_PERF_*` request logs, `platform/tools/platform_production_qa.py`,
  `platform/tools/platform_performance_audit.py`, `pg_stat_statements`,
  systemd/journalctl, nginx/socket counts, and PostgreSQL catalog views.
- Add new temporary logging only behind env flags or bounded QA tooling.
  Do not leave per-request verbose logging always-on in production.
- Record both realistic and worst-case profiles when a previous QA scenario no
  longer matches the frontend flow.
- Keep generated test data auditable and cleanup-capable. Do not mutate
  `sparkydb`.

## Prometheus And Grafana

Use Prometheus/Grafana when the platform needs continuous trend visibility,
alerting, or post-incident correlation across deploys. For the current small
2 CPU VPS, start with a lightweight setup:

- node_exporter for CPU, RAM, swap, load, disk, and network;
- postgres_exporter with limited credentials for connection, lock, cache, and
  slow-query indicators;
- nginx exporter or nginx stub_status for connections/request rate;
- app metrics only after deciding stable low-cardinality labels:
  route template, method, status class, user-step phase, worker queue, cache
  hit/miss.

Do not add high-cardinality labels such as user id, tournament id, slug,
email, raw path with UUIDs, or search text.

## Output

Return monitored user steps, route/SQL/system evidence, accepted
optimizations, verification commands, deploy/rollback notes, and remaining
limits.
