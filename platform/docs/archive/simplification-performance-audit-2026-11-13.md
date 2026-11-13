# Platform simplification and performance audit — 2026-11-13

- Status: One cumulative application deployment completed; public browser QA and roster cleanup remain open
- Owner: Platform maintainers
- Scope: Production platform, frontend SSR/client boundary, public tournament catalog, and CI/release/backup controls

## Verified production and rollback baseline

Production now runs application and installed LiveQA payload source
`126d4114542369af9d999e219563fa1d53b09568`; its previous app is the initial
rollback source `cb1a75114aac0f4fb0ee6cc407011f90c6e76f31`. Host-tools pin C3
is `625bbaa11023ffaf059dfa03a34a4850b02d4f93`. The single app deployment
followed exact-SHA Security `43401840198`, source-bound restore-verified backup
`43404066694`, AUTO `43403887168`, and automatic production child `43404454746`.
Post-deploy readiness and health passed with an operations receipt/checkpoint;
this is not a public browser QA pass.
Deployment preflight compared Alembic `current` with `heads` at
`20261112_0055`; no downgrade occurred. The application change did not alter
schema, dependencies, or the host-tools pin. The standard application rollback
pointer remains the initial `cb1a...` release.

The initial bounded readiness result preceded activation and is not the final
deployment evidence. The post-deploy operations receipt/checkpoint is the
source for the successful health result. A null source-binding digest is
expected where the runner and app source SHAs are equal; it is not a failed
binding.

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
| Recognize the two reviewed project change/cleanup skill files as companions to application source in the CI classifier | Accepted | The exception removes only those exact paths from classification when the remaining route is already full, non-fallback and contains `platform/apps/` source. It retains all full gates and event-based deployment authority; skill-only and docs-only plus skills remain non-deployable, and other `.agents/` paths retain fail-closed fallback. Focused regression cases cover these boundaries. |
| Require a source-bound backup for every full baseline-reconcile route | Accepted | AUTO now skips backup only for strict docs-only reconciliation. Recovery-bootstrap and candidate-packaging routes retain exact-source backup and the child authenticates the complete baseline-to-target range before choosing a no-op or app release. The tradeoff is one extra verified backup for recovery-only no-op changes; the latest observed baseline backup took 1m16s, not a duration guarantee. Storage remains a separate no-deploy route. |
| Finalize the internal baseline proof against its expected workflow outcomes (PR #359) | Accepted | The finalizer requires the eight expected successful proof jobs and eleven expected general-skip jobs. This validates the internal proof summary; ordinary full-source Security gates remain unchanged. |
| Remove the optional Node auth transport and simplify its pinned host-tools/deploy boundary | Rejected for this release | The latest SSR C32 A/B evidence reports no pairwise performance gain. The selector also crosses the release-independent host-tools pin, service environment rendering, workflows, tests and operations guidance; removing only the app side would break a coherent rollback. See the [performance matrix](../../performance/README.md) and [host-tools pin](../../tools/platform_host_tools_pin.py). |
| Consolidate or further parallelize CI contours, or remove/relax backup, host-tools, release-provenance, recovery or retention gates | Rejected | Independent owner gates already run concurrently. The longest measured spans are test execution, and preserving serial ownership inside each contour avoids changing test isolation and evidence boundaries. Exact-source no-op routing already skips app artifact build/deploy for non-runtime changes. No measured evidence supports additional restructuring or weakening gates. |
| Migrate the single-VPS application services from systemd to Docker Compose | Deferred | The accepted [systemd versus Docker ADR](../adr/systemd-vs-docker.md) identifies no measured launch blocker and requires a separate migration/rollback decision. A rough 10–20 engineer-day estimate covers only application services on the existing VPS; it excludes PostgreSQL, Redis, and Kubernetes migration and is not measured effort or AI wall-clock time. No account-specific usage metrics were available, so this audit makes no AI quota or percentage claim; see the public [pricing documentation](https://learn.chatgpt.com/docs/pricing). |

### Delivery of the reviewed application changes

The cumulative application source `126d4114542369af9d999e219563fa1d53b09568`
was activated in the one production app deployment recorded above. The final
classifier route carried the application source through normal exact-SHA
Security, source-bound backup, AUTO and child deployment. The previous app
remains the initial `cb1a...` rollback release. No schema, dependency, or
host-tools pin change was included.

The frontend audit found no justified dependency removal, duplicate eager
detail/bracket fetch, or reason to change nonce CSP, authentication, CSRF,
session, cache, component, Draft, or production routing boundaries. The DTO
serializer is the only measured application performance improvement here;
SSR and end-to-end production latency were not byte- or time-quantified by this
audit.

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

A successful full Security run on the prior baseline `cb1a751...` completed in 11m33s;
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
- Exact-SHA Security `43401840198`, restore-verified source-bound backup
  `43404066694`, AUTO `43403887168`, and production child `43404454746`
  completed; post-deploy readiness and health passed per the operations
  receipt/checkpoint. This receipt does not certify public browser QA.
- Public launch runs `43405943415` and `43408704800` timed out with exit 124
  and produced no case counts. Both exact orphan gates were recovered through
  the trusted path; the recovery preserved the bundle and 13 generated roster
  entries. A supported direct public run against the same deployed source
  lasted about 300.2 seconds at
  the systemd-unit level; its terminal output was lost, so it has no pass or
  failure credit. The unit, gate, and runtime mount aliases are gone. Automated
  user QA has not started; human Chrome authentication and Turnstile QA remain
  for the user.
- The 13 generated roster entries await canonical recovery-bundle cleanup. The
  recorded bundle `created_at` is `2026-11-13T10:02:41Z`, so eligibility is
  after `2026-11-13T14:02:41Z`. Fresh-bundle cleanup refused; no replacement
  inventory was fabricated. The main manual account was not pre-created and
  the control identity is excluded.
- A separate public-QA timeout change is not part of the deployed source and
  is not installed; the active host-tools pin remains C3. Future validation of
  that change does not count as public QA for this deployed source. No
  deployment result for the timeout change is claimed here.

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
