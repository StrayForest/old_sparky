# Platform test-suite governance

- Status: Active reference
- Owner: Platform maintainers
- Last reviewed: 2026-10-01

The executable registry at `platform/tools/platform_verify.py` is the single
source of truth for verification ownership, commands, environment
requirements and CI membership. Use `list --json` when tooling needs the
machine-readable registry. This document explains the architecture and
placement rules; it does not repeat tool arguments.

## Verification layers

| Gate ID | Layer and owner | Environment | Normal trigger |
| --- | --- | --- | --- |
| `backend` | unit/integration, backend and domain owners | hermetic test PostgreSQL/Redis | local feedback + CI |
| `python-quality` | Python quality, backend/tooling owners | canonical hash-locked CI dependencies | local feedback + CI |
| `security` | dependency and repository security owners | canonical hash-locked CI dependencies | local feedback + CI |
| `migration` | persistence owners | disposable PostgreSQL only | CI |
| `docs` | platform maintainers | repository checkout; Markdown docs and project skill metadata | local feedback + CI |
| `web-quality` | web owners | Node 26.3.1 and locked dependencies | local feedback + CI |
| `web-hermetic` | web owners | local/mocked API and Chromium | local feedback + CI |
| `verification-contract` | platform tooling owners | repository checkout | CI |
| `release-runtime` | release owners | root test user; disposable staged checkout and local ZIP fixtures | conditional runtime-sensitive/fallback fixture route |
| `server-smoke` | release owners | exact deployed SHA | deployment workflow |
| `live-public` | production operators | canonical public origin and dedicated QA identity | explicit/release workflow |
| `live-user-destructive` | production operators | marked production fixtures and mandatory cleanup | explicit operator workflow |
| `external-load` | performance operators | external generator to production origin | explicit operator workflow |

The `web-quality` gate runs the runtime shutdown-signal, SSR stream diagnostics
and Next root-directory glob contracts,
`apps/platform_web/tests/shutdown-guard-contract.cjs`,
`apps/platform_web/tests/ssr-stream-diagnostics-contract.cjs` and
`apps/platform_web/tests/next-rootdir-glob-contract.mjs`, through the locked
`platform_node.sh` helper after CI provisions Node 26.3.1. The shutdown
contract self-signals only after the guard preload, has a bounded in-child
watchdog, and cleans up its detached process group on failure. The SSR
contract bounds both enabled and disabled fixtures and cleans up their
detached process groups on failure. The backend tool contour keeps
source/ownership assertions for these contracts but does not execute their
Node processes; this prevents an unpinned backend runner from duplicating the
web runtime checks. The Next contract configures a patterned `next.rootDir`
with a Pages route and verifies `@next/next/no-html-link-for-pages` still
reports an internal navigation link. This guards the scoped glob compatibility
override for `@next/eslint-plugin-next`; remove that override after upstream
releases a patched `braces` dependency. The focused local commands are
`tools/platform_web_npm.sh --prefix apps/platform_web run test:shutdown-guard`
and
`tools/platform_web_npm.sh --prefix apps/platform_web run test:ssr-stream-diagnostics`
and
`tools/platform_web_npm.sh --prefix apps/platform_web run test:next-rootdir-glob`
from `platform/`; the helper fails closed unless Node 26.3.1 is selected.

The first eight gates are deterministic and always part of the normal CI
aggregate. The conditional `release-runtime` gate is deterministic as well,
but is intentionally excluded from `platform_verify.py ci`: the classifier
enables it for an exact runtime-sensitive path change or for a fail-closed
fallback route. The fallback condition ensures an uncertain route receives
the release-runtime coverage without granting it production authority.
On pull requests and `merge_group`, `release-runtime` is fixture-only: it
builds staged runtime from pinned ZIP fixtures under `python -I`, then checks
file-map/mode parity, ordered paths, archive validation and install behavior.
Compact-v2 fixtures verify the source-suite/provider digests, provider
retention, validator agreement and fixed read-only unit aliases; collision,
identity-bound cleanup and UID paths are covered without a live browser. The
gate has no production
network, credentials or deployment authority. A separate `release-runtime-real`
job runs only for a
classifier-sensitive or fallback `push` to canonical `dev`, or for
`workflow_dispatch` whose ref is exactly `dev`. It starts on a fresh runner,
checks out the exact workflow SHA, creates the production-style clean root venv
and invokes the full `platform_build_release.sh` builder. It validates the
archive checksum and `RELEASE.json` provenance read-only, emits only bounded
status/digest data, and removes its identified temporary output/cache in an
exit trap. It does not receive production secrets or SSH material, publish an
artifact, or deploy; `status-final` independently requires the fixture job and
this real job on the routes where each is required. Non-`dev` manual runs and
all ordinary/docs routes keep the real job skipped as selected by the
classifier; there is no schedule trigger.
The canonical builder keeps its ordinary build output in a private root-owned
`0600` raw log and emits only allowlisted `RELEASE_BUILD_PHASE` markers to a
separate root-owned `0600` marker stream. The real job reads only that bounded
marker stream through `platform_release_build_diagnostics.py`'s explicit
`--marker-log` interface before
identity-checked cleanup and prints only the normalized phase, reason, cleanup
state and builder/parser return codes. Missing, malformed, oversized, mutable
or untrusted marker streams fail closed; the raw builder log is never parsed,
printed or uploaded, and both files are removed with the identified temporary
root after successful cleanup. Cleanup-identity failures remain fail-closed
for operator investigation. The workflow contract verifies the exact
identity-checked cleanup path; deletion-failure simulation is intentionally
not mocked because it would replace the root-owned filesystem boundary with a
test double. The builder's cleanup trap preserves the original
failure status and emits a failed `complete` marker, so diagnostic success
cannot turn a failed build into a green gate. A parser rejection is reported
only with the bounded reason enum `oversized`, `metadata`, `control`,
`encoding`, `marker`, `sequence` or `missing`.
`platform_verify.py ci` can execute only the always-on deterministic gates and
never connects to production, creates production fixtures, opens a production
browser or starts a load generator. The latter four remain discoverable
governance groups but are workflow-only.

## Backend catalog and contours

The executable [backend test catalog](../tools/platform_test_catalog.py)
owns test-method discovery and the one-contour owner for every
`platform/tests/test_*.py` method. The [catalog runner](../tools/platform_test_runner.py)
loads only those catalog-owned IDs; it does not rely on a second filename list
in CI or in this document. The `backend` gate is the aggregate of exactly
these five catalog contours:

| Catalog contour | Timeout | Shared-resource execution | Ownership boundary |
| --- | ---: | --- | --- |
| `backend-unit` | 300s | not serial-resource constrained | unit/domain/backend tests with no external operator contour |
| `backend-tool-contract` | 600s | not serial-resource constrained | repository tool and contract tests that are hermetic and do not require root-owned host metadata; privacy-safe sampling projections (`test_platform_evidence_privacy`) |
| `backend-integration` | 1200s | serial | PostgreSQL/Redis integration tests and real workflow races |
| `backend-privileged` | 1200s | serial | root/service-identity, media, release/install/systemd, root-owned artifact metadata and privileged wrapper tests |
| `performance-contract` | 900s | not serial-resource constrained | deterministic load/observer/acceptance contracts, including request sampling (`test_platform_request_performance`) and production-QA summary (`test_platform_production_qa_write_burst_profile`) |
| `backend` (aggregate) | 3600s | serial orchestration | disjoint union of the five contours |

The timeout values are the catalog's executable contract, not a moving test
count or an estimate derived from the current number of methods. The catalog
rejects unknown, duplicate, overlapping or unowned IDs and detects its
snapshot drift through `verification-contract`; add a new test by updating
that executable catalog and its self-test, never by hard-coding a count or
editing a workflow filename list. The stable gate and command registry remains
the [canonical verifier](../tools/platform_verify.py),
which delegates `backend` and each sub-contour to the guarded runner.

Every aggregate and backend sub-contour is guarded before test discovery by
one pure, fail-closed resource validator. It requires the exact values
`PLATFORM_ENVIRONMENT=test`, `PLATFORM_DB_SCHEMA=platform`, database
`platformdb_test`, a literal IP loopback database host, and a literal IP
loopback Redis host with path `/15`. Missing values, DNS names, host lists,
malformed URLs, userinfo ambiguity, query strings and fragments are rejected;
the validator performs no connection, DNS lookup or client operation. The
shell wrapper and runner both invoke this same validator, including the
aggregate-only artifact-verification path, so a skipped wrapper check cannot
open a production target. A production environment or `platformdb` target is
a `LOCAL GATE BLOCKED` refusal, not a test run.
The aggregate and `backend-integration` runner additionally perform a
read-only resource preflight: PostgreSQL must expose exactly the current
Alembic head and the complete initial role seed, and Redis DB 15 must answer a
ping. A missing migration or seed is a `LOCAL GATE BLOCKED` refusal with an
instruction to run the migration gate, so an already-cleaned disposable
database cannot produce a misleading cascade of HTTP 503 test failures.
`backend-integration` and `backend-privileged` are serial because they use
shared database, Redis or host-identity resources; the aggregate preserves
that serial boundary. Release/install and systemd contract modules remain in
the privileged contour even when their fixtures are temporary directories:
their production paths intentionally enforce root ownership, fixed release
paths or systemd state. Tests must mock subprocess contracts rather than
requiring a real mount namespace or `CAP_SYS_ADMIN` on the CI runner.
`test_candidate_capture_runner_is_private_bounded_and_composes_with_dispatcher`
is a root-only `backend-privileged` and `release-runtime` fixture exercising
the production runner, marker, dispatcher and isolated fake candidates for
bounded capture, status, no-follow setup, cleanup and descendant termination.
Successful captures are removed after reap and durable metadata; failed ones
stay root-only for scoped owner cleanup, and raw output never enters logs.
`test_cloudflare_failed_oneshot_is_quiescent_only_with_empty_cgroup_contract`
is a root-only `backend-privileged`/`release-runtime` fixture accepting only
failed process-free oneshots with empty `ControlGroup`; all else fails closed.
Privileged release-lock tests hold a serial test guard and use unique
root-owned files directly beneath `/run/lock`; cleanup removes only the exact
test-prefixed regular file and never the production canonical lock.

Local/canonical invocations that use the host test services hold one global
cross-UID lock at the fixed
`/run/lock/oldsparky-platform-verification/oldsparky-platformdb-test.lock`
pathname. Root provisions its parent (exact mode `755`) and the root-owned
read-only lock file (exact mode `444`) atomically before CI; missing or
misowned/wrong-mode provisioning is a `LOCAL GATE BLOCKED` refusal. The
acquirer validates every parent component and the lock's device/inode, owner,
link count and mode before and after opening and after taking a non-blocking
`flock`. It never trusts or writes a marker, and therefore a non-root caller
cannot replace, chmod or truncate the lock identity. Normal completion and
handled termination signals release the lock. DB-free contours do not acquire
this resource, so local verification does not become globally serial. GitHub
jobs retain parallelism because each database/Redis service container is
isolated per job; the migration, integration and privileged CI jobs all
provision this same fixed lock before invoking their guarded runners.
Root-required runner contours reject a non-root caller before configuration or
resource validation, lock acquisition, catalog discovery or cleanup; the
aggregate component-manifest verifier is the explicit DB-free exception.

The privileged preflight is fail-closed: normal aggregate execution and
`backend-privileged` require the root test user; aggregate component-manifest
verification is the DB-free exception. Media-processor cases also
require Pillow, `/usr/bin/runuser`, `/usr/bin/test` and the `oldsparky-media`
group. Mode-sensitive fixtures chmod directories to their exact expected
modes after creation so checks remain valid under private umask `0077`,
including each synthetic media import-path directory needed by service users.
Git clones restore indexed modes before pinned checks; checkout keeps umask modes.
Wrapper cases additionally require `/usr/bin/setpriv`. Missing prerequisites
are blocked before tests start and must not be converted into skips. The sole
intentional strict-contour skip is the exact catalog ID
`tests.test_platform_live_qa_mailbox_helper.MailboxHelperTests.test_live_shared_env_metadata_matches_reviewed_contour_when_present`,
with the exact reason `production shared env path is absent`; the catalog
checks that source declaration and any other skip fails the contour.

### Migration ownership and safety

The `migration` gate is the only owner of populated Alembic verification. Its
support helper resolves the source graph with Alembic's `ScriptDirectory`,
checks `alembic current --check-heads`, and compares the official
`MigrationContext` current head with exactly one `public.alembic_version` row.
The source head is discovered at runtime; callers must not copy a `HEAD_REVISION`
constant. Backend integration preflight reuses the same source/database-head
helper before test discovery.

Every Alembic or migration-owned recovery subprocess has a typed 180-second timeout. The migration workflow keeps a five-minute job deadline, retains its loopback `platformdb_test` PostgreSQL service, and has no production URL or database access. The scenario checks a compact critical schema contract from `information_schema` and `pg_catalog`, including `indisvalid`, `indisready` and `indislive` for required indexes.

The verifier starts each gate command in its own POSIX session and observes child exit without reaping until the TERM cleanup window closes. During supervision, timeout or cancellation sends `SIGTERM` to that command's group, waits one second, sends `SIGKILL` if needed, and then reaps the direct child; only ordinary descendants in the group are covered, so children creating a new session must own their cleanup. It never matches or signals unrelated processes. Timeout remains a failed gate with status `124`; cleanup cannot turn it into a pass or skip checks.

The real disposable-database scenario selects only the latest reversible edge
after inspecting its downgrade body, upgrades and downgrades that edge, and
then upgrades again. It separately expects revision `20260829_0046` to refuse
downgrade because its historical invite-access data cannot be restored. It
does not infer that an older range such as `0038` to `base` is safe. Fixture
cleanup revalidates the exact test URL and `platform` schema, removes only that
schema/version table, and preserves the primary migration exception if cleanup
also fails.

## CI route and release authority

`Platform security and build` is intentionally always scheduled for pull
requests, pushes to `dev`, merge queues and explicit dispatches. It does not
use a top-level `paths`/`paths-ignore` filter: the first classifier job reads a
complete, exact-SHA file range and publishes the digest-bound,
`classifier-manifest.json` artifact. The manifest schema/version, target SHA,
event, route class, expected gates, deployability, fallback flag, reason and
digest are validated by the downstream release workflows.

The route class and fallback bit are both part of the exact classifier
manifest. Known, complete repository state and a recognized event can produce
`class=full`, `fallback=false`; deployability is a separate authority bit. The
trusted host-tools candidate packaging paths are deliberately full-coverage
but CI-only, while application/runtime/migration/release paths retain normal
trusted-push authority. The exact candidate allowlist is owned by the
[classifier source](../tools/platform_ci_classifier.py). The routes are:

| Route/provenance | Expected gates | `fallback` | Production authority |
| --- | --- | --- | --- |
| known `docs-only` | `docs`, `verification-contract` | `false` | never deployable; auto-deploy is a successful no-op |
| known `out-of-scope` | `verification-contract` | `false` | never deployable; auto-deploy is a successful no-op |
| known full candidate-packaging-only path set (candidate workflow/helper, contract test/catalog/verification updates and platform docs) | all first eight gates | `false` | never deployable; trusted pushes are successful CI-only no-ops |
| exact storage-operations family (diagnostics/maintenance workflow or exact storage-retention skill, plus only its closed classifier/AUTO/recovery-workflow/tests/catalog set and docs) | all first eight gates plus runtime-sensitive gates | `false` | full verification, non-deployable; AUTO takes no-deploy; producer uploads an attested skip receipt; publisher checks exact producer/parent CI evidence and performs no host-bundle or marker action |
| known `full` application/runtime/migration/release path (including `platform/tools/platform_workflow_remote_dispatch.py`, invoked by external-load from the active app) | all first eight gates | `false` | deployable only for a non-fallback push to current `dev`; dispatcher changes are runtime-sensitive |
| unknown or malformed full fallback | all first eight gates | `true` | never deployable; fail-closed verification only |

Host-tools lifecycle files use full-route verification. Candidate-packaging-only
changes remain non-deployable; a mixed application change follows its app path
and may deploy on a trusted push. Host-only pin/bundle/bootstrap remain no-op.
`platform/contracts/host_tools_pin.json` is the
single bounded pin contract: application-only changes keep the reviewed
`HOST_TOOLS_SHA`, while a host-control closure edit must update the pin and its
closure baseline. The practical bump is commit A (closure change), out-of-band
provision/self-test of A, then commit B (pin-only host-control change) pointing
at A with A's exact baseline; B is never self-pinned. The pin resolver checks the exact lowercase commit,
repository, reachability, ancestry, path set, modes and digests; the
host-tools contract tests therefore fail before a closure edit can merge
without an intentional pin bump. `TARGET_SHA` remains the classifier,
release-artifact, provenance, migration and deployed-receipt identity.

The `verification-contract` CI job owns the real installed-pin resolve: it
checks out the exact target with `fetch-depth: 0` and runs the canonical
resolver against the repository and target SHA. The fast DB-free backend
contour stays shallow and proves the static contract plus the synthetic A/B
pin-bump lifecycle; it does not silently skip historical ancestry proof.

Operation-bound legacy LiveQA fixtures in `test_platform_recovery_bootstrap` stay in `backend-privileged`; they cover receipt/pointer/venv identity, service/timer snapshot, bounded readiness retries, fixed child-stage/status diagnostics and receipt-last cleanup.
`test_platform_release_recovery_boundaries` covers the candidate-deadline-clamped reconcile timeout, retained receipt, and idempotent retry after a simulated partial payload publish. `test_platform_live_qa_runtime_install` covers fixed stage diagnostics for a post-promotion retention failure. These remain in `backend-privileged` because they exercise release-state transitions and privileged payload ownership.
`test_liveqa_reconcile_stderr_is_available_only_to_private_candidate_capture`
checks the helper preserves reconcile stderr. Its companion
`test_candidate_capture_runner_is_private_bounded_and_composes_with_dispatcher` verifies private
capture and closed stdout/public markers; the executable catalog snapshot tracks these IDs.
Privileged release tests prove promoted dependencies stay readable, relocation removes only wheel/RECORD-bound generated caches, cleanup is receipt/readiness/cgroup-bound, and runtime summaries stay out of deployment markers.
Venv reuse requires an exact active quiesce receipt and wheel/`RECORD` integrity; tampering is refused.
A proof miss uses the existing fresh-venv snapshot path. These root-owned cases remain in `backend-privileged`; bootstrap extraction tests in `test_platform_validate_release_artifact` belong here because they validate the privileged release boundary.
The canonical AST snapshot is 1,718 tests (1,646 backend, including 555 privileged) across 162 modules, with every ID assigned exactly once.
Release retention tests own locked candidate-set rechecks and belong to
`backend-privileged`.

The `platform-host-tools-candidate.yml` workflow is a separate trusted `workflow_run` contract, not
a production gate. Its validator and bundle tests belong to `backend-tool-contract` through
`test_platform_host_tools_bundle`, except the active-baseline reader test, which exercises
root-owned release metadata and belongs to `backend-privileged`; catalog snapshots track both.
The verification-contract gate invokes `host_tools_candidate_workflow_issues()` so default-branch
guards, narrow permissions, pinned actions, data-only candidate handling, isolated-Python use,
TOCTOU rechecks and production artifact isolation fail closed. The workflow returns a successful
no-op for a base-reachable pin; only a novel eligible pin reaches build, attestation and upload.
That output is review evidence only: it grants no deploy/provision authority, and production
workflows must not consume its artifact prefixes.

The handoff context keeps the triggering PR source head **E** separate from
the synthetic merge SHA **M** that the security workflow tested.  The trusted
validator binds the exact workflow/run/attempt and its embedded PR snapshot,
then requires the current PR, the singular
`refs/pull/<N>/merge` matching-ref response, and the commit API to agree on
`[base, E]`, exactly two ordered parents and a valid tree.  The final summary
must target **M**; any optional split provenance fields are a closed,
type-checked exact comparison.  The route artifact is checked independently
for `target_sha == M` and for the classifier digest shared with the summary.
Jobs are read from the exact-attempt API and every row identity supplied by
GitHub (`run_id`, `run_attempt`, source-head `head_sha`, source `head_branch`,
`workflow_name`) is bound to the same run.  Artifact metadata uses the same
source-head identity.  The complete immutable context is fetched and
compared again immediately before attestation and upload, so PR synchronize,
base/head/merge-ref/tree changes and reruns fail closed.  The candidate
checkout remains data-only, while all validator code is checked out from
trusted `dev` and invoked with `python -I -B`.

Unknown/global paths, malformed input or provenance, a shallow/unavailable
repository, an unknown event and every `merge_group` event use the full route
with `fallback=true` and `deployable=false`; these routes also run the
conditional `release-runtime` fixture gate, and a sensitive/fallback push to
`dev` runs `release-runtime-real`. Recognized platform paths use
`fallback=false`; full verification alone does not imply production authority.
The candidate-packaging and exact storage-operations families are separately
closed, full-coverage, non-deployable routes. The storage family requires
runtime-sensitive gates. AUTO takes its no-deploy branch before baseline or
production. The producer uploads a provenance-attested skip receipt; the
publisher binds its artifact and payload to exact producer and parent
CI/classifier evidence, without cryptographically verifying the attestation or
publishing a recovery bundle. Tests stay in the existing `verification-contract`
classifier module and `backend-privileged` storage diagnostics module. A known deployable full path with valid exact-SHA provenance
is distinct from both review-only families. A successful `platform-security-build` status therefore remains the exact-SHA CI result, not permission to deploy by itself:
auto-deploy must download and validate the matching classifier artifact, and
production repeats that guard before any artifact build or server-side effect.
This preserves the release authority while preventing a documentation or
uncertain route from producing a false production success. The implementation
is the [classifier source](../tools/platform_ci_classifier.py);
the [security workflow](../../.github/workflows/platform-security.yml)
and release workflows consume its digest-bound manifest.

The event and conditional-job behavior follows the primary GitHub Actions
contracts for [workflow events](https://docs.github.com/en/actions/reference/events-that-trigger-workflows),
[job conditions](https://docs.github.com/en/actions/using-jobs/using-conditions-to-control-job-execution),
[workflow artifacts](https://docs.github.com/en/actions/using-workflows/storing-workflow-data-as-artifacts),
and [dependency caching](https://docs.github.com/en/actions/using-workflows/caching-dependencies-to-speed-up-workflows).

## Ownership rules

Use the lowest suitable layer in the test pyramid:

The host disk policy is a shared release/operations contract owned by
`tools/platform_disk_policy.py`. The health-monitor unit test and storage-
maintenance privileged test must cover exact 5 GiB and 85% boundaries,
fail-closed invalid usage, the conservative `total - available` formula and
their systemd/CLI wiring. A threshold or formula change requires updating the
policy helper, both consumers, the runbook owner in
[`operations-runbook.md`](operations-runbook.md), and the executable catalog
snapshot in the same change; it must not be hidden by a route-specific test
skip.

1. Put domain and API behavior in the auto-discovered `platform/tests/test_*.py`
   tree. A new ordinary backend test requires no workflow or filename-list
   edit.
2. Put important cross-component browser journeys in the existing hermetic
   Playwright suites under `apps/platform_web/tests`. The package’s hermetic
   script owns suite discovery; a new ordinary scenario requires no workflow
   edit.
3. Add a new deterministic contour to the executable registry, its runner,
   the verification-contract self-test and the CI job that invokes the gate.
4. Keep deployment smoke and real-origin QA in their protected production
   workflows. They are not substitutes for hermetic tests and do not expand
   into a production regression suite.
5. Add load, stress, spike, soak or capacity scenarios as versioned profiles
   under `platform/performance/profiles/`. The profile
   owns the complete scenario and acceptance contract. The same external-load
   workflow should orchestrate a new reviewed profile.

Async backend tests inherit `PlatformIsolatedAsyncioTestCase` from
`tests/platform_async_case.py`. Its finalizer closes process-shared Redis
clients and the SQLAlchemy async engine on the event loop that owns them,
before `IsolatedAsyncioTestCase` closes that loop. Direct inheritance from the
stdlib async test case is prohibited by a backend contract test so a new suite
cannot silently reintroduce cross-loop Redis/asyncpg cleanup warnings.
The shared base retains asyncio debug mode but uses a five-second slow-callback
threshold for real PostgreSQL integration steps; dedicated performance gates,
not the generic unittest 100 ms threshold, own latency acceptance.

Never add an individual ordinary test by editing GitHub workflow YAML. Do not
hide deterministic failures with grep exclusions or silent retries. A flaky
test is explicit test debt with an owner, not a reason to weaken a gate.

### Web hermetic ownership

The web package has three explicit deterministic owners. The
`test:source-contract` runner owns source assertions only; its
[`playwright.source-contract.config.ts`](../apps/platform_web/playwright.source-contract.config.ts)
must not define `webServer` or boot an API/browser server. The
[`platform_web_hermetic.sh`](../tools/platform_web_hermetic.sh)
runner builds the standalone Next artifact once in a temporary directory,
then runs source-contract, smoke and participant suites from that same
immutable build. Smoke and participant runs are separate and sequential; each
starts fresh API/web processes with `reuseExistingServer: false`, so no
ambient server, browser or database state is reused.

The responsive smoke project owns `desktop`, `wide-1300`, `tablet-820` and
`mobile-layout` viewports. Responsive specs run in that matrix; the explicit
desktop-only list in the
[`playwright.config.ts`](../apps/platform_web/playwright.config.ts)
is owned only by the `desktop` project. The participant-progressive suite has
its own sequential one-worker contour and explicit desktop/mobile projects in
[`playwright.participant.config.ts`](../apps/platform_web/playwright.participant.config.ts).
Do not infer viewport ownership from a test name or silently add an exclusion;
update the owning config and its contract test when the matrix changes.

## Local and GitHub verification

Local canonical gates provide fast developer feedback. They must use the
registry, for example:

```bash
cd platform
.venv_platform/bin/python tools/platform_verify.py list
.venv_platform/bin/python tools/platform_verify.py backend --focused tests.test_platform_domain
.venv_platform/bin/python tools/platform_verify.py python-quality
```

`LOCAL GATE BLOCKED` means a required safe dependency such as test PostgreSQL,
isolated Redis or Chromium is unavailable. A smaller substitute must not be
reported as a complete-gate pass.

### Python CI dependency contract

`requirements-platform.txt` owns runtime inputs used by the release artifact;
`requirements-quality.txt` owns direct lint/security inputs. The canonical
non-editable Python CI environment is generated from
[`requirements-ci.in`](../requirements-ci.in) into
[`requirements-ci.lock.txt`](../requirements-ci.lock.txt) with
`tools/platform_generate_ci_lock.sh`. The lock contains every transitive
runtime, test/quality and security package, including exact hashed `pip`,
`setuptools` and `wheel` bootstrap pins.

The generator's separate
[`requirements-ci-locker.lock.txt`](../requirements-ci-locker.lock.txt)
pins pip-tools and its own transitive bootstrap set. Default generation is a
byte-stable freshness check constrained by the current CI lock; only explicit
`tools/platform_generate_ci_lock.sh --update` may resolve newer versions.
[`requirements-ci.lock.meta.json`](../requirements-ci.lock.meta.json) records
the nested input closure, target, toolchain versions and SHA-256 digests, so
new `-r` includes cannot bypass the contract.

The lock is currently scoped to Python 3.12 on Linux x86_64 because the
platform and quality pins include native wheels. The generator and installer
fail closed for another interpreter or architecture; a new runner target
requires its own generated lock and an explicit workflow contract. The
`platform-security.yml` Python jobs all call
[`tools/platform_install_ci_python.sh`](../tools/platform_install_ci_python.sh),
which creates a fresh virtualenv, installs only the lock with
`--require-hashes --only-binary=:all:`, and runs `pip check`. Missing or
malformed lock inputs are errors, never a skipped dependency contour.
The installer and generator sanitize ambient pip configuration through
[`tools/platform_ci_pip_env.sh`](../tools/platform_ci_pip_env.sh), use only
the canonical PyPI index, and never forward extra indexes, trusted hosts,
find-links, certificate paths or proxy variables.
The security dependency-audit gate also audits this complete lock, so the
runtime, quality and security tool dependency sets are covered by one report.

Each job's setup-python cache is keyed by the lock path, so lock changes
invalidate dependency artifacts without sharing a mutable virtualenv between
contours. The local bootstrap remains intentionally separate: it installs the
runtime input for developer iteration and does not substitute for the
hash-locked CI gate.

GitHub Actions owns runner images, service containers, dependency bootstrap,
parallelism, caches, artifacts, permissions, environment authorization and
commit statuses. The security workflow invokes stable gate IDs and retains
parallel full-route jobs; reduced routes skip only gates absent from their
manifest. Its aggregate `platform-security-build=success` status for the exact
tested execution SHA remains the release authority; the workflow names that
value `TESTED_SHA=${{ github.sha }}` for pull-request synthetic merges, pushes,
merge queues and manual dispatches. On a pull request, the source-head SHA is
used only for the changed-file diff and never replaces `TESTED_SHA`; the
trusted default-branch SHA used to check out the classifier is separate
implementation provenance. Manual commit statuses are published only for
`push` and `workflow_dispatch`, and target `TESTED_SHA`. A local pass is
neither necessary nor sufficient for deployment.

The auto-deploy and production-deploy provenance gates read the exact
commit's paginated raw status rows through GitHub's [list commit statuses
endpoint](https://docs.github.com/en/rest/commits/statuses#list-commit-statuses-for-a-reference).
The auto-deploy page assembler retains each row, including `creator`, and the shared validator
requires the exact context/state, `github-actions[bot]` creator, attempt URL,
run attempt and target SHA before a status can authorize release behavior.

## Production and performance boundaries

Deployment smoke runs only after immutable deployment activation and checks
that the exact SHA started and critical interfaces are alive. Live public and
destructive user QA are separate operator contours with their own identities,
confirmation and cleanup rules.

The tracked browser journey
`apps/platform_web/tests/smoke/live-user-journey.spec.ts` is owned by the
`platform-live-user-qa.yml` workflow. Run it only against the exact deployed
`dev` SHA through that workflow; its dedicated server supervisor refreshes the
reviewed mailbox helper under the release lock, runs the browser scenario, and
performs exact fixture cleanup. The dispatch and recovery procedure is defined
in [the CSP live-QA runbook](csp-live-qa-runbook.md).

The `workflow_dispatch` `control_email` and live `marker` values cross the
production boundary only after the dependency-free canonical parser applies a
bounded ASCII grammar. The runner writes the accepted fields to a mode-0600
JSON handoff; an invalid value stops before SSH credentials, keyscan or an SSH
command is reached. SSH carries only the fixed
`platform_workflow_remote_dispatch.py` mode on its command line and the JSON on
stdin. Every immutable dispatcher invocation is explicit
`/usr/bin/python3.12 -I -B`; `-I` supplies import isolation and `-B` prevents
bytecode writes into root-owned generations. The remote dispatcher rejects a
host-generation invocation without `-B` before loading sibling code and emits
`python_bytecode_disabled=1` in its capability contract. The host capability
test runs under a bounded file-size limit and compares the complete inventory
before and after the self-test, including the absence of `__pycache__`/`.pyc`.
The remote dispatcher validates the closed schema again before passing values
as data to a fixed-argv helper, and no email, marker or parser detail is
printed into a public report. The adversarial workflow contract covers shell
punctuation, quoting, newlines, command substitutions, option-like prefixes,
Unicode/control/NUL-equivalent values and overlong values.

The same guard owns exact operator confirmations, lower-case 40-character
production SHAs, strict UTC timestamp shapes and bounded numeric dispatch
inputs in the release-recovery, service-recovery, storage-maintenance, origin
proof and runtime-diagnostics workflows. Each of those workflows checks the
reviewed guard before exposing an SSH secret; host-side revalidation fails
closed when the installed helper is absent or returns an error. The canonical
load-profile registry remains the owner of profile IDs and dispatchability.

The canonical load-profile registry is `platform/performance/`.
Profiles record fixture shape, logical actions, HTTP attempts, concurrency,
spread/ramp, retry semantics, expected statuses, correctness, latency budgets,
resource evidence and cleanup. The external HTTP generator runs on the
GitHub-hosted runner. The production host performs only bounded fixture,
observer and exact-cleanup work; it never generates the measured client load.
Production SSH secrets are scoped only to trusted fixture, finalization and
cleanup steps: checkout persists no credentials, and each checked-out client
or evaluator runs with an explicit allowlist while private keys, SSH auth
sockets and control paths are absent from the runner.
The same contract applies to every production workflow: SSH secret expressions
are step-local only, and checkout, artifact upload/download and checked-out
client steps must not inherit them. Read-only web-runtime diagnostics upload
only fixed-schema aggregate summaries; producer/helper failures fail closed and
successful empty input is the only permitted empty result.
Candidate checkout/build/load jobs and environment-approved SSH jobs are
separate fresh jobs. A secret-bearing job never checks out the candidate or
executes a candidate-produced program; it consumes only a fixed-path,
closed-schema handoff or immutable release archive after inline validation of
the exact target SHA, artifact digest and provenance. Long-running origin
supervisors detach at the trusted remote dispatcher boundary, so a local
background child cannot cross into a later candidate job or retain runner
credentials.
The production deploy DAG keeps its dispatch validator always scheduled,
builds and publishes the candidate artifact only in the separate
non-secret build-release job, and requires both validator and build success
before the production job can consume that exact artifact. The
environment-approved production job has no checkout or candidate build
executable; verification-contract owns this DAG assertion.
The standardized production SSH workflows that materialize credentials use a
literal, per-workflow `$RUNNER_TEMP/...-ssh` directory; setup and cleanup
reject a directory symlink. An `always()` cleanup removes only the named key,
`known_hosts`, config and control-socket paths, verifies each path and the
directory are absent, clears `SSH_DIR` through `GITHUB_ENV`, and gates any
later artifact action on cleanup success. Cleanup is never
`continue-on-error`, so a cleanup failure keeps the job failed without
replacing the primary operation result.
Their host-key setup uses the same bounded two-attempt contract: each fresh
mode-600 scan file is limited by `timeout --foreground 4s` around
`ssh-keyscan -T 3`, with a visible 0.5-second backoff and exact pinned
Ed25519/fingerprint validation. The SSH contract test discovers every workflow
site dynamically and exercises first-attempt recovery, persistent failure,
wrong/multiple/malformed output, attempt bounds and candidate cleanup.

Every retained result identifies its source SHA, profile ID/version/digest,
runner, fixture shape, offered logical actions, HTTP attempts and acceptance
outcome. A performance result is incomplete unless correctness and exact
cleanup pass. Canceled runs use the matching abort/cleanup workflow keyed by
the exact run ID.

## Contract self-test

The privileged contour owns immutable recovery-bootstrap tests
(`tests.test_platform_recovery_bootstrap`); backend-tool-contract owns cumulative-baseline
provenance tests in `tests.test_platform_workflow_provenance`. The
verification-contract contour owns failed-report caller tests and the synthetic `unittest.subTest` accounting regression: multiple failures/errors count as one executed parent ID while native failure/error rows remain in the summary.
Together the contract tests cover closed bundles, exact manifest/provenance schemas, content-addressed installation, receipt identity, migration-uncertainty guards, and secret/SSH ordering. `tests.test_platform_release_systemd_state`
adds subprocess coverage for operation-ID/path/inode mismatch, helper-manifest
tampering and rollback-target retry binding; `tests.test_platform_release_recovery_boundaries`
covers rollback/recovery subprocess faults. Recovery-bootstrap tests retain
attestation variants and the legacy-v2 no-systemd cleanup bridge; live execution remains an explicit operator recovery action.

The `docs` gate checks document shape, repository-local links and project skill
frontmatter/interface metadata. `verification-contract` checks registry/CI
membership, workflow gate names, classifier route ownership and artifact
guards, direct command duplication, exclusion bypasses, backend discovery,
hermetic suite registration, production reachability from `ci`, documentation
gate IDs, load-profile schema/deduplication, workflow-owned load budgets and
the external-generator topology. Keep it deterministic and small enough to run on every CI change.
