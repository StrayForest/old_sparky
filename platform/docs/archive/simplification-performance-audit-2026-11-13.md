# Platform simplification and performance audit — 2026-11-13

- Status: Corrected release and automated QA closed; manual auth remains open
- Owner: Platform maintainers
- Scope: Production platform, frontend SSR/client boundary, public tournament catalog, and CI/release/backup controls

## Verified production and rollback baseline

Production runs application source
`9a3d203c8b8d3bb09226e010e559516657d21034` in release
`gha-43426930984-1-9a3d203c8b8d`; the exact previous application source is
the initial rollback target `cb1a75114aac0f4fb0ee6cc407011f90c6e76f31` in
release `gha-43367073524-1-cb1a75114aac`. The earlier candidate
`126d4114542369af9d999e219563fa1d53b09568` was rolled back to that initial
source before the corrected release. The corrected deployment preserves
`cb1a...` as `previous`.

Host-tools C4 source `64cae2a4c8235df1ba5c044446cd2f497f4abdca` is active,
verified installed and self-tested; C3
`625bbaa11023ffaf059dfa03a34a4850b02d4f93` remains retained. C3 and C4
preserve the same 15 helper files and capability set, with 17 total bundle
files. The only C4 host-tools contract changes are dispatcher timeout from 300
to 2,400 seconds and the derived capability hash. The application guard permits
1,500 seconds only for the strict canonical public command.

PR 360 source `9a3d203c8b8d3bb09226e010e559516657d21034` passed exact-SHA
Security `43422580427/1`, post-provision preflight `43426001349/1`,
source-bound restore-verified backup `43426481223/1`, AUTO `43424193707`, and
automatic production child `43426930984/1`; the child succeeded. Required
post-deploy strict preflight, origin/SNI checks, public smokes and fresh
readiness passed. The readiness receipt confirms current source `9a3d...`,
previous `cb1a...`, selected C4 and retained C3. Runtime schema is unchanged.
The root-only source-bound readiness receipt is
`/root/.codex/checkpoints/oldsparky-live-readiness-postdeploy-M-mrVZsWAR/readiness.json`.

After the strict age threshold, the old 13-account roster was recovered through
the active M immutable payload under both canonical locks. Exact cleanup and
absence proof passed; the root-only receipt is
`/root/.oldsparky/reports/liveqa-recover-bundle.2PxPsM/`.

Exact-source public workflow `43439319991` completed all three jobs
successfully. Its sanitized artifact records 57 logical cases and attempts:
47 passed and 10 skipped, with zero failures, timeouts, flaky, interrupted or
expected-failure cases, and zero retries. Process and child exit codes were 0.
The root-only artifact is
`/root/.codex/checkpoints/oldsparky-liveqa-candidate-public-20261113/sanitized/live-launch-sanitized.json`
with SHA-256
`cca1531730ea9ac523d8ca345a9ac54bdf73cdd4afce4d778e8c65d9762aaa0`.

After fresh canonical provisioning, exact-source public workflow `43449584614`
also passed: 57 logical cases and attempts, 47 passed and 10 skipped, with zero
failures, timeouts, flaky, interrupted or expected-failure cases, zero retries,
and process/child exit 0.

The first automated UserQA workflow `43440684487` failed with browser child
exit 1 and remains uncredited; its journey cause is unknown. Report publication
was blocked by an unrelated Nov 11 root-owned diagnostics directory. A
reviewer-approved helper (`0501cdc1c7db6b7a77652ef2936dfb169f3f5f0ebbea6c0aad9f08ff02eaa7cb`)
preserved it outside retention under canonical locks. The preservation receipt
is `/root/.oldsparky/private-preservation/liveqa-diagnostic-store-preserved-20261113T144953Z-e22f5d205332/completion.json`.
It confirms the source slot is absent and the preserved destination inode and
two child metadata identities remain unchanged; no file contents were read or
deleted. This repaired the report-store precondition but does not explain the
browser failure; the app source and pin did not change.

Fresh exact-source UserQA `43450827101` passed complete coverage: one journey,
one attempt, zero skips, failures, flaky, timeouts, interruptions or expected
failures, child exit 0. The sanitized report is
`/root/.codex/checkpoints/oldsparky-liveqa-candidate-public-20261113/userqa-retry-sanitized/live-user-qa-report.json`.
The wrapper completed exact cleanup and absence proof before state removal.
Final guard checks under both locks found zero automated/manual recovery
states, public gates, QA processes or diagnostic entries. The sanitized
absence proof is
`/root/.codex/checkpoints/oldsparky-liveqa-candidate-public-20261113/post-userqa-cleanup/absence-proof.txt`;
the final readiness receipt is
`/root/.codex/checkpoints/oldsparky-liveqa-candidate-public-20261113/post-userqa-cleanup/readiness.json`.
It reports ready with current M, previous `cb1a`, C4 active/C3 retained,
API/web 200, locks free, no pending jobs and consistency passed. Human Chrome
authentication and Turnstile QA remain deferred for the user. Before manual
QA, reconcile the fixed production checkout to the exact active source while
preserving retained local work, then require the owner runbook's
checkout-provenance guard to pass.

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

The frontend audit found no justified dependency removal, duplicate eager
detail/bracket fetch, or reason to change nonce CSP, authentication, CSRF,
session, cache, component, Draft, or production routing boundaries. The DTO
serializer is the only measured application performance improvement here;
SSR and end-to-end production latency were not byte- or time-quantified by this
audit.

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
- Historical public launches `43405943415` and `43408704800` timed out with
  exit 124 and no case counts; both exact orphan gates were recovered through
  the trusted path. A later supported public run lasted about 300.2 seconds at
  the systemd-unit level, but its terminal output was lost. These earlier runs
  receive no pass or failure credit; they do not supersede the exact-source
  public result recorded above.

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
