# Platform simplification and performance audit — 2026-11-13

- Status: Candidate reviewed; full exact-SHA CI and production release pending
- Owner: Platform maintainers
- Scope: Production platform, frontend SSR/client boundary, public tournament catalog, and CI/release/backup controls

## Verified production and rollback baseline

The bounded read-only readiness collector returned `ready` for current app
`cb1a75114aac0f4fb0ee6cc407011f90c6e76f31`, release
`gha-43367073524-1-cb1a75114aac`, and previous app
`79b3b95ede586da84c3d49138bf88888f866c6b3`, release
`gha-43176891484-1-79b3b95ede58`. Host-tools C3
`625bbaa11023ffaf059dfa03a34a4850b02d4f93` passed all 15 pinned-member and
mode checks. API/web readiness, service state, locks, operation marker,
maintenance timer, disk availability and final pointer consistency passed.
Database Alembic head is `20261112_0055`; the source-bound backup and
maintenance receipts confirm restore/Alembic verification across 37 tables.
The read-only readiness collector itself does not query Alembic state.

The candidate is based on source `6bd19b64d8b4711e49da8c98ef3ce2c9727d74c6`.
It changes no schema, runtime dependencies or host-tools pin. For the single
authorized app deployment, preserve current `cb1a751...` as the initial
rollback release; require the usual exact-source restore-verified backup.
Rollback switches application releases and does not downgrade Alembic.

## Audit decisions

| Audited surface | Result |
| --- | --- |
| Backend tournament API, auth/session checks, rate limits, public/private DTOs, media projection, translation, workers, database access and cache | Kept existing authorization, projection, query/cache and worker boundaries. Catalog list serialization was the only supported narrow optimization; no auth, data-loading, cache-envelope or worker change was justified. |
| Web tournament client/forms, profile/admin flows, auth/session transport and SSR | Accepted only the server-only scheduler boundary below. No duplicate eager detail/bracket read or safe dependency removal was found; shared client transport and profile/admin behavior stay unchanged. |
| Draft | Kept the static HTML/CSS/JS and Wrangler boundary, fixed R2/CDN assets, room rules and origin/CSP/size/captain-secret guardrails. No Next dependency or theme/layout work was justified. |
| CI, deployment, backup, rollback, host-tools and retention | Kept provenance and recovery gates. Existing independent security gates already run in parallel, while measured critical time is test execution; setup-python/npm caches are already enabled. No safe CI speedup or gate removal was selected. |

The review of authentication, session, rate-limit, media, translation, worker,
database/cache, profile/admin, tournament form/client, and Draft surfaces was
for finding behavior-preserving opportunities; it does not mean those controls
were candidates for removal.

| Candidate | Decision | Evidence and safeguards |
| --- | --- | --- |
| Serialize the public tournament catalog DTO list directly with one module-level Pydantic adapter | Accepted | Keeps the declared `TournamentCardResponse` schema, public DTO fields, pagination/cache headers and cold/warm body. The ASGI regression checks a fixed nested-media payload, key order, nulls, UTC timestamp, status, headers, cache parity, DB call count, OpenAPI schema and omission of private worker error detail. |
| Move the bounded internal-read scheduler behind a server-only tournament workspace wrapper | Accepted | Detail and bracket SSR callers retain the 24-active/128-pending/5-second FIFO guard, cancellation, and typed 503 behavior. Browser callers keep the shared API function without importing the scheduler. Web-hermetic asserts scheduler markers are absent from built browser JavaScript and fails on missing build output. |
| Route project change/cleanup skills through the canonical verification registry | Accepted | Replaces raw `unittest discover`/npm/Alembic substitution instructions with the impact planner and complete owner gates. Exact-SHA CI, production backup, AUTO/child and recovery rules remain mandatory. |
| Remove the optional Node auth transport and simplify its pinned host-tools/deploy boundary | Rejected for this release | The latest SSR C32 A/B evidence reports no pairwise performance gain. The selector also crosses the release-independent host-tools pin, service environment rendering, workflows, tests and operations guidance; removing only the app side would break a coherent rollback. See the [performance matrix](../../performance/README.md) and [host-tools pin](../../tools/platform_host_tools_pin.py). |
| Consolidate or further parallelize CI contours, or remove/relax backup, host-tools, release-provenance, recovery or retention gates | Rejected | Independent owner gates already run concurrently. The longest measured spans are test execution, and preserving serial ownership inside each contour avoids changing test isolation and evidence boundaries. Exact-source no-op routing already skips app artifact build/deploy for non-runtime changes. No measured evidence supports additional restructuring or weakening gates. |
| Migrate production to Docker | Deferred | The accepted [systemd versus Docker ADR](../adr/systemd-vs-docker.md) identifies no measured launch blocker and requires a separate migration/rollback decision. |

The frontend audit also found no justified dependency removal, no duplicate
eager detail/bracket fetch, and no need to change nonce CSP, authentication,
CSRF, session, cache, component, Draft, or production routing boundaries.

## Measured results and limits

The catalog serializer microbenchmark used one synthetic validated card DTO
with nested CDN media, then lists of 9 and 50 cards. On the approved immutable
production interpreter (Python 3.12.3, FastAPI 0.137.2, Pydantic 2.13.3), the
old `model_dump_json` list builder plus modeled FastAPI validation/serialization
was compared with the candidate adapter. After 80 warmups, 9 batches of 400
calls were timed with `perf_counter_ns`; outputs were byte-identical. Median
serializer time was 135.72 µs versus 57.96 µs for 9 cards (2.34×), and
789.20 µs versus 313.41 µs for 50 cards (2.52×). This is synthetic DTO-only
serialization, with no database, Redis or network; it does not predict endpoint
or production latency.

The frontend change removes scheduler code from the browser build when the
web-hermetic guard passes. The old 30,036-byte chunk contained unrelated
modules, so it does not support an attributable bundle-size delta. No user
experience or production latency gain is claimed.

One successful full Security run on baseline `cb1a751...` completed in 11m33s;
its privileged contour ran 10m51s (10m18s in the serial contour), Web hermetic
8m21s (7m13s in the gate and 52s dependency install), and DB-free backend
contours 4m48s (4m23s in the serial run step). Production backup completed in
1m16s and the deploy run in 8m45s. These single-run wall-clock measurements
are not duration guarantees. `setup-python` pip and `setup-node` npm caches
were already enabled, and the longest spans were test execution rather than
dependency installation; no safe CI speedup was selected.

## Validation status

- Focused backend API/cache suites: 14 passed under the immutable production
  interpreter with isolated loopback-only dummy service URLs; no live database
  or Redis access.
- Candidate `git diff --check`: passed.
- Full exact-SHA Security and the web-hermetic/browser guard: pending.
- Production combined deploy, post-deploy checks and final `CURRENT.md` update:
  pending; no candidate production mutation has occurred.

Official primary references used for the implementation and boundary review:
[FastAPI response-model filtering](https://fastapi.tiangolo.com/tutorial/response-model/),
[FastAPI direct/custom responses](https://fastapi.tiangolo.com/advanced/custom-response/),
[Pydantic serialization](https://docs.pydantic.dev/latest/concepts/serialization/),
[SQLAlchemy 2.0 asyncio and implicit-I/O guidance](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html),
[Next.js
CSP/nonces](https://nextjs.org/docs/app/guides/content-security-policy),
[Next.js `connection()`](https://nextjs.org/docs/app/api-reference/functions/connection),
[React request-scoped `cache()`](https://react.dev/reference/react/cache),
[Next.js data fetching](https://nextjs.org/docs/app/getting-started/fetching-data),
[Next.js authentication guidance](https://nextjs.org/docs/app/guides/authentication),
and [Node.js v26.3.1 HTTP agent behavior](https://nodejs.org/download/release/v26.3.1/docs/api/http.html).
