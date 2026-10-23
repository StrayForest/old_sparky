# Platform test-suite governance

- Status: Active reference
- Owner: Platform maintainers
- Last reviewed: 2026-10-21

The executable registry at `platform/tools/platform_verify.py` is the single source of truth for verification ownership, commands, environment
requirements and CI membership. Use `list --json` when tooling needs the
machine-readable registry. This document explains the architecture and
placement rules; it does not repeat tool arguments. This checkout shares production storage: inventory exact task-temp paths, sizes and open users; preserve small sanitized evidence.
Coordinate exact-path cleanup with the release lead; never use broad globs. Avoid duplicate local environments/heavy suites; hosted CI is authoritative, and selectors require disk/resource checks.
For a local `web-hermetic` run on the shared production filesystem, budget its isolated browser/build copy alongside the worktree's `node_modules`, `.next` and npm download cache before starting. After the gate exits, prove its task-owned paths have no process references and remove those reproducible artifacts before calculating backup and deploy capacity. Do not treat space used by a local web gate as available for a concurrent release.

For a local change-impact plan, run from `platform/`:

```sh
python3 tools/platform_verify.py impact --changed-path platform/tools/platform_verify.py
```

The planner maps changed sources to complete owners; unknown or malformed paths select the full canonical gate set. It is a test-selection aid only: it cannot skip owner gates or replace exact-SHA CI. Changes to test modules select their complete owner contour.

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
| `verification-contract` | platform tooling owners | repository checkout; attested actionlint 1.7.12 | local feedback + CI |
| `release-runtime` | release owners | root test user; disposable staged checkout and local ZIP fixtures | conditional runtime-sensitive/fallback fixture route |
| `server-smoke` | release owners | exact deployed SHA | deployment workflow |
| `live-public` | production operators | canonical public origin and dedicated QA identity | explicit/release workflow |
| `live-user-destructive` | production operators | marked production fixtures and mandatory cleanup | explicit operator workflow |
| `external-load` | performance operators | external generator to production origin | explicit operator workflow |

The `web-quality` gate runs shutdown, SSR stream/heap and Next root-directory
contracts from `shutdown-guard-contract.cjs`, `ssr-stream-diagnostics-contract.cjs`,
`ssr-heap-metrics-contract.mjs` and `next-rootdir-glob-contract.mjs` through the locked
`platform_node.sh` helper after CI provisions Node 26.3.1. The shutdown
contract self-signals only after the guard preload, has a bounded in-child
watchdog, and cleans up its detached process group on failure. The SSR
contract bounds both enabled and disabled fixtures and cleans up their
detached process groups on failure. The backend tool contour keeps
source/ownership assertions for these contracts but does not execute their
Node processes; this prevents an unpinned backend runner from duplicating the
web runtime checks. The heap contract injects a fake clock, process memory and
GC entries, verifying numeric-only output, 5,760-sample/eight-hour limits and
timer disposal without a server or browser. The Next contract configures a patterned `next.rootDir`
with a Pages route and verifies `@next/next/no-html-link-for-pages` still
reports an internal navigation link. This guards the scoped glob compatibility
override for `@next/eslint-plugin-next`; remove that override after upstream
releases a patched `braces` dependency. Run `tools/platform_verify.py
web-quality` from `platform/`; the helper requires Node 26.3.1.

The first eight gates are deterministic and always part of the normal CI aggregate. The conditional `release-runtime` gate is
deterministic as well, but is intentionally excluded from `platform_verify.py ci`: the classifier enables it for an exact
runtime-sensitive path change, a fail-closed fallback, an exact baseline-runtime proof, or every deployable full push to `dev`.
This ensures that a later test-only push cannot deploy atop undelivered runtime changes without producing exact-target runtime
proof. In the local `ci` aggregate, migration cleanup drops the disposable schema, so a validated test URL/schema and migration
lock guard an Alembic-head re-upgrade before backend preflight; unsafe targets stop the aggregate, and standalone migration
behavior is unchanged. On pull requests and `merge_group`, `release-runtime` is fixture-only: it builds staged runtime from pinned
ZIP fixtures under `python -I`, then checks file-map/mode parity, ordered paths, archive validation and install behavior.
Compact-v2 fixtures verify the source-suite/provider digests, provider retention, validator agreement and fixed read-only unit
aliases; collision, identity-bound cleanup and UID paths are covered without a live browser. The gate has no production network,
credentials or deployment authority. A separate `release-runtime-real` job runs for classifier-sensitive or fallback pushes to
canonical `dev`, every deployable full push to `dev`, same-repository PRs targeting `dev` when that route requires the real
builder, or `workflow_dispatch` whose ref is exactly `dev`. The PR route binds the tested merge SHA, ordered base/head parents and
exact same-repository refs before building. It starts on a fresh runner, checks out the exact tested SHA, creates the
production-style clean root venv and invokes the full `platform_build_release.sh` builder. It validates the archive checksum and
`RELEASE.json` provenance read-only, emits only bounded status/digest data, and removes its identified temporary output/cache in
an exit trap. The same-repository PR route also uploads one short-lived JSON size projection bound to the tested source, run and
archive; the release archive, logs and build tree are never uploaded. The job receives no production secrets or SSH material and
does not deploy; `status-final` independently requires the fixture job and real job on routes where each is required. Non-`dev`
manual runs and ordinary/docs routes keep the real job skipped as selected by the classifier; there is no schedule trigger. The
canonical builder keeps its ordinary build output in a private root-owned `0600` raw log and emits only allowlisted
`RELEASE_BUILD_PHASE` markers to a separate root-owned `0600` marker stream. The real job reads only that bounded marker stream
through `platform_release_build_diagnostics.py`'s explicit `--marker-log` interface before identity-checked cleanup and prints
only the normalized phase, reason, cleanup state and builder/parser return codes. Missing, malformed, oversized, mutable or
untrusted marker streams fail closed; the raw builder log is never parsed, printed or uploaded, and both files are removed with
the identified temporary root after successful cleanup. Cleanup-identity failures remain fail-closed for operator investigation.
The workflow contract verifies the exact identity-checked cleanup path; deletion-failure simulation is intentionally not mocked
because it would replace the root-owned filesystem boundary with a test double. The builder's cleanup trap preserves the original
failure status and emits a failed `complete` marker, so diagnostic success cannot turn a failed build into a green gate. A parser
rejection is reported only with the bounded reason enum `oversized`, `metadata`, `control`, `encoding`, `marker`, `sequence` or
`missing`. `platform_verify.py ci` can execute only the always-on deterministic gates and never connects to production, creates
production fixtures, opens a production browser or starts a load generator. The latter four remain discoverable governance groups
but are workflow-only.

The security workflow runs credential-free `host-tools-pin-preflight` before applicable test jobs. It resolves the exact-SHA pin against full history; downstream jobs require its success, and status finalization fails closed on a skipped prerequisite. The resolver result is not an input to the backup-verifier artifact.

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
| `backend-tool-contract` | 600s | not serial-resource constrained | hermetic repository tools, backup restore diagnostics (including paired scratch PostgreSQL startup settings), privacy-safe projections (`test_platform_evidence_privacy`) and the bounded loopback content-read diagnostic without root-owned host metadata |
| `backend-integration` | 1200s | serial | PostgreSQL/Redis integration tests and real workflow races |
| `backend-privileged` | 1200s | serial | root/service-identity, media, Redis DB 15 purge, release/install/systemd, root-owned artifact metadata and privileged wrapper tests |
| `performance-contract` | 900s | not serial-resource constrained | deterministic load/observer/acceptance contracts, including request sampling (`test_platform_request_performance`), production-QA summary (`test_platform_production_qa_write_burst_profile`), and bounded tmpfs diagnostic contracts (`test_platform_performance_contract`) |
| `backend` (aggregate) | 3600s | serial orchestration | disjoint union of the five contours |

`test_platform_external_load` (performance-contract) covers bounded HTTP body failures, phase accounting, client CPU sampler lifecycle, cgroup-capacity reason parsing and mount-root mapping, and inline retry queue attribution; missing or ambiguous visible quota evidence remains UNKNOWN. `test_platform_evidence_privacy` (backend-tool-contract) owns whole-series run-window, production observer-row mapping, observer-timeline and identity-stripping projections. `test_platform_live_qa_runtime_install` (backend-privileged) exercises the copied dispatcher against its installed release validator, including missing/tampered refusal. The existing `test_platform_live_qa_guard` lock cases own the shared machine-lock boundary, while `test_platform_storage_maintenance` checks backup-only lazy imports and maintenance-to-guard lock identity. `test_platform_public_content_diagnostic` is also in `backend-tool-contract`; its tests cover the current four home-feed rows, bounded loopback reads, cache headers, deadlines, metadata readiness and privacy-safe summaries. Optional dispatch manifest inputs scope exact IDs to one release; the automatic sample remains diagnostic only, not a capacity benchmark.

The existing `test_platform_live_qa_guard` and `test_platform_live_qa_wrappers` cases also own the exact immutable-payload stale-bundle recovery admission and retained-state-on-cleanup-failure behavior; their synthetic fixtures never run production database cleanup.

The root-only isolated-restore signal test and disk-image fixture directory identity/mode test run once in the required `backend-privileged` contour. Their aggregate summary must include successful timing; missing or skipped execution fails. The directory test verifies the actual root-owned fixture preconditions and must not be moved to a non-root contour or mocked as root. This current-run evidence does not replace the separately source- and parent-bound `baseline-runtime` proof, and does not authorize reuse across runs.

The synthetic PG16 restore diagnostic is hosted Security-only in `backend-privileged`. It creates a fixed 32 MiB custom dump (4,096 rows × 8,192-byte payloads) with a `pg_trgm` GIN index and Alembic marker, then runs the three fixed serial restore arms using the explicitly selected scratch backend. `tmpfs_v1` remains the default; the separate `disk_ext4_image_v1` diagnostic uses the verified bounded ext4 image, formatted with discard disabled and eager inode-table initialization. After formatting, the exact open image inode is reallocated before the unchanged full-allocation predicate is checked, covering formatter zero-range fallback that may punch holes in regular files. The full-allocation predicate is also checked during mount verification. Both backends retain the verified 640 MiB, zero-swap unit and the same restore, isolation and cleanup checks. Each arm checks exact row digests and payload bytes before/after both schema restores, plus schema, index plan/result and Alembic equivalence. The sanitized source-bound artifact records separate cluster-wide WAL counter deltas; each after-snapshot waits for all sessions in the exact restore database to exit, flushing backend-local WAL statistics before observation. It also records `pg_restore` command time, precheck-through-postcheck phase time, reset cgroup peak and the selected backend's sampled filesystem high-water for `restore_platform` and `restore_public`. This synthetic diagnostic does not prove production capacity or change production defaults.
The SSR admission-shed parser remains in `test_platform_ssr_observability` (performance-contract), public projection privacy in `test_platform_evidence_privacy` (backend-tool-contract), and the bounded web scheduler in the existing `frontend-audit-regressions.spec.ts` smoke owner. The inline retry queue regression adds one ID to the existing performance-contract module; it adds no module or gate-ownership entry.

Every aggregate and backend sub-contour is guarded before test discovery by one pure, fail-closed resource validator. It requires
the exact values `PLATFORM_ENVIRONMENT=test`, `PLATFORM_DB_SCHEMA=platform`, database `platformdb_test`, a literal IP loopback
database host, and a literal IP loopback Redis host with path `/15`. Missing values, DNS names, host lists, malformed URLs,
userinfo ambiguity, query strings and fragments are rejected; the validator performs no connection, DNS lookup or client
operation. The shell wrapper and runner both invoke this same validator, including the aggregate-only artifact-verification path,
so a skipped wrapper check cannot open a production target. A production environment or `platformdb` target is a `LOCAL GATE
BLOCKED` refusal, not a test run. The aggregate and `backend-integration` runner additionally perform a read-only resource
preflight: PostgreSQL must expose exactly the current Alembic head and the complete initial role seed, and Redis DB 15 must answer
a ping. A missing migration or seed is a `LOCAL GATE BLOCKED` refusal with an instruction to run the migration gate, so an
already-cleaned disposable database cannot produce a misleading cascade of HTTP 503 test failures. The migration scenario
cleans the disposable schema when it exits; direct local sequencing must then apply `.venv_platform/bin/python -m alembic
upgrade head` to `platformdb_test` before `backend-integration`, matching the CI job's separate schema-upgrade step.
`backend-privileged` requires
root and disposable Redis DB 15, but not PostgreSQL. The verifier resolves the safe dotenv once and passes the validated mapping
to the child and timeout cleanup. CI provisions Redis 7 for both lanes; teardown flushes only DB 15. On timeout, the parent reaps
its direct child before reacquiring the privileged lock; cleanup errors do not replace timeout 124. `backend-integration` and
`backend-privileged` are serial because they share database, Redis or host-identity resources; the aggregate preserves that
boundary. Release/install and systemd modules remain privileged even with temporary-directory fixtures because production paths
enforce root ownership, fixed release paths or systemd state. Tests mock subprocess contracts instead of requiring a real mount
namespace or `CAP_SYS_ADMIN` on the CI runner. `test_profile_access_cache_purge_is_fixed_bounded_and_verified` is
`backend-integration`; it verifies fixed rollback/restore namespaces, bounded scans and fail-closed handling.
`test_restore_profile_access_cache_purge_requires_stopped_services_and_skips_retention` is `backend-privileged`; it verifies restore purge requires stopped API/worker and skips backup/retention.

The four root-only supervisor fixture IDs in the catalog (`test_supervisor_cleanup_covers_upload_failures_and_closes_locks`, `test_host_tools_contract_failure_emits_marker_before_locks`, `test_deploy_only_artifact_preparation_errors_emit_markers_without_app_writes`, `test_retained_lock_supervisor_preserves_callback_failure_marker`) require the hosted Linux root environment. Their helper checks exact ID, runner environment and UID before `/opt` or `/run/lock` writes; local execution fails with `LOCAL GATE BLOCKED`, and positive fixtures remain in the hosted privileged contour.

The 11 catalogued `test_platform_load_runtime` cases that create real PID namespaces require a non-root Linux process with PID-namespace capability. The canonical verifier and direct runner check this exact ID set before execution; a local miss is `LOCAL GATE BLOCKED`, not an exclusion or skip. The explicit namespace-probe refusal test remains runnable on root; the complete contour runs on the hosted Security runner.
`test_candidate_capture_runner_is_private_bounded_and_composes_with_dispatcher` is a root-only `backend-privileged` and
`release-runtime` fixture exercising the production runner, marker, dispatcher and isolated fake candidates for bounded capture,
status, no-follow setup, cleanup and descendant termination. Successful captures are removed after reap and durable metadata;
failed ones stay root-only for scoped owner cleanup, and raw output never enters logs.
`test_cloudflare_failed_oneshot_is_quiescent_only_with_empty_cgroup_contract` is a root-only
`backend-privileged`/`release-runtime` fixture accepting only failed process-free oneshots with empty `ControlGroup`; all else
fails closed. Privileged release-lock tests hold a serial test guard and use unique root-owned files directly beneath `/run/lock`;
cleanup removes only the exact test-prefixed regular file and never the production canonical lock.

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
| exact production host-tools provisioning ADR and recovery-bootstrap-only family, excluding the active LiveQA guard and runtime builder | all first eight gates | `false` | non-deployable baseline reconcile; active payload/runtime-builder fixes use the runtime-sensitive ordinary full route so a reviewed `dev` push builds and installs them |
| known `out-of-scope` | `verification-contract` | `false` | never deployable; auto-deploy is a successful no-op |
| known full candidate-packaging-only path set (candidate workflow/helper, contract test/catalog/verification updates and platform docs) | all first eight gates | `false` | never deployable; trusted pushes are successful CI-only no-ops |
| exact storage-operations family (diagnostics/maintenance workflow or exact storage-retention skill, plus only its closed classifier/AUTO/recovery-workflow/tests/catalog set and docs) | all first eight gates plus runtime-sensitive gates | `false` | full verification, non-deployable; AUTO takes no-deploy; producer uploads an attested skip receipt; publisher checks exact producer/parent CI evidence and performs no host-bundle or marker action |
| known `full` application/runtime/migration/release path (including `platform/tools/platform_workflow_remote_dispatch.py`, invoked by external-load from the active app) | all first eight gates | `false` | deployable only for a non-fallback push to current `dev`; dispatcher changes are runtime-sensitive |
| unknown or malformed full fallback | all first eight gates | `true` | never deployable; fail-closed verification only |

Host-tools lifecycle files use full-route verification. The exact host-tools
provisioning ADR is part of the recovery-bootstrap-only family so its enforced
pin contract receives the full gate set; it remains non-deployable by itself.
The active payload helper `platform/tools/platform_live_qa_guard.py` and the
release artifact builder `platform/tools/platform_build_live_qa_runtime.py`
are excluded from that family because their changes must reach a new immutable
app release; their runtime-sensitive full routes require the exact runtime
gates.
Candidate-packaging-only changes remain non-deployable; a mixed application
change follows its app path and may deploy on a trusted push. Host-only
pin/bundle/bootstrap remain no-op.
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

Operation-bound legacy LiveQA and maintenance-failure evidence fixtures in `test_platform_recovery_bootstrap` stay in `backend-privileged`; they cover receipt/pointer/venv identity, service/timer snapshot, bounded readiness retries, immutable evidence binding, fixed reset-failed diagnostics and receipt-last cleanup. The recovery workflow contract also executes the capture staging shell with fake SSH/SCP and a real OpenSSL CMS round trip, including certificate pin rejection, ciphertext tampering, wrong-key failure and plaintext exclusion. Its recovery-binding regression resolves the workflow by exact path and workflow ID while retaining failed-run, source, repository, ref and attempt checks; it does not accept a display-name prefix or add a host operation.
`test_platform_release_recovery_boundaries` covers clamped reconcile timeout, retained receipts, retry after partial publish, and refusal to resume rollback without durable cache-purge proof. Its transaction-bound purge test uses the current release/pre-rollback venv for rollback and the candidate release/venv for install-abort. The copied executable rollback entry point owns selector-call coverage for the current installer and exact receipt arguments; quiesce fault-injection cases stub the earlier bootstrap preflight so they reach the intended post-quiesce receipt boundary. The protected activation recovery cases bind the exact prior provider and durable migration authorization, restore only the recorded core services while the health timer remains quiesced, and require a fresh successful health-monitor invocation before ordinary receipt-bound restoration. Their write-barrier fixtures verify refusal before authorization and stop before the first pointer mutation in the authorized path. `test_platform_live_qa_runtime_install` covers fixed retention-failure diagnostics, provider reuse without relaxing generation checks, M8 rollback reconciliation, and the bounded provider-only recovery boundary. Recovery tests verify the complete v1 runtime validator, source-manifest binding, all three logical pointer roles with deduplicated owner manifests, refusal to overwrite invalid targets, safe failure diagnostics, and private-stage cleanup on reference drift or publication failure. Its new candidate-owned provider rebind and receipt-derived rollback cases also cover the changed app-lock digest and transaction-bound install-failure recovery. These are `backend-privileged` because they exercise release state and privileged payload ownership.

`LiveQaRuntimeInstallTests.test_typed_provider_payload_digest_binds_aliases_and_cleanup_is_closed` exercises legacy, self-owned and reused provider generations as installer-published payloads: standalone dispatcher `verify` and safe-env `validate-runtime` accept each generation, while guard whole-payload/runtime-root and mount-alias helpers validate the same identities directly (not through a guard CLI). Source-owned aliases materialize only from exact typed `engine_links`; an external provider's validated link map remains metadata and creates no local `provider-runtime` links. `LiveQaWrapperContractTests.test_live_launch_workflow_delegates_to_server_supervisor` checks that the workflow parser and dispatcher agree on the closed Live Launch `stage`/`check` vocabulary.
`test_platform_live_qa_wrappers` also owns the source-bound fixed-stage status contract for public launch failures before the browser supervisor starts; its failure tests remain in `backend-privileged` and invoke no browser or database work.
The same wrapper owns the optional closed browser-count line, bound to source/app/marker hashes and logical-case/attempt partitions before terminal launch status. Its one-case, no-retry live-user journey succeeds only with one executed/passed case and attempt and no skips or failures. Workflow `test_count` counts executed logical outcomes; total and skipped remain separate. Missing/invalid counts stay unavailable and sanitization never invents QA results. QA-only child-capture tests cover concurrent pipe draining, bounded output, closed diagnostics, timeout cleanup, secret privacy, and fixed systemd/Playwright commands; generic release subprocesses remain outside this adapter.
`test_liveqa_reconcile_stderr_is_available_only_to_private_candidate_capture` checks private reconcile stderr. Its companion `test_candidate_capture_runner_is_private_bounded_and_composes_with_dispatcher` verifies private capture and closed stdout/public markers; the executable catalog snapshot tracks both IDs.
Privileged release tests prove promoted dependencies stay readable, relocation removes only wheel/RECORD-bound generated caches, cleanup is receipt/readiness/cgroup-bound, and runtime summaries stay out of deployment markers.
Venv reuse requires an exact active quiesce receipt and wheel/`RECORD` integrity; tampering is refused. The origin proof follows a bounded, cycle-checked unchanged-release chain until the installer writes its root-private candidate-bound receipt. Owner tests cover two unchanged hops, pruning intermediate releases, receipt tampering and chain gaps; proof misses fail before fresh-venv allocation when required. The required-reuse policy integration test invokes the real proof CLI and installer receipt path; its fixture models runtime/integrity/space inputs and accepts only the four exact `pip check`/`pip freeze --all` command vectors, while the transaction-status CLI, proof/policy checks and receipt binding remain real. Synthetic archive and transaction-directory modes are explicitly normalized so this fixture is independent of process umask; production metadata rejection remains exercised. Separate tests cover the production runtime and wheel/`RECORD` integrity guards. These root-owned cases remain in `backend-privileged`; bootstrap extraction and release-size projection tests in `test_platform_validate_release_artifact` belong here because they validate the privileged release boundary.
`test_platform_cpu_profile` also owns the fixed-schema diagnostic-plan, process CPU-window, and bounded CPU-profile contracts in `performance-contract`; they cover exact service/process/phase binding, one-use request selection, released-runtime identity, and fixed-category summaries without exporting raw frames.
`TmpfsAllocationAuditTests` in `test_platform_performance_contract` owns the metadata-only tmpfs diagnostic contracts in `performance-contract`. Synthetic private trees cover no-follow symlinks, regular-file hardlink deduplication, two-pass identity drift, entry caps, bounded `MemAvailable`/`Shmem` parsing, private outputs, the final execution-receipt rule and a complete synthetic `main` run. These tests do not scan the host `/dev/shm`, read service/process state, or permit cleanup.
`test_platform_load_runtime` and `test_platform_external_load` own source-bound timeout checkpoints in `performance-contract`; diagnostic counters include completed HTTP attempts only and never grant acceptance. For acceptance, `schedule_delay` and `late_start` each require one applicable sample per raw request's first attempt, derived as that summary's request count minus its exact retry count; phase and aggregate retry reconciliation remains mandatory. Queue, service, observed-latency and lifecycle evidence still covers every HTTP attempt, so missing first-attempt samples remain structural failures. `PerformanceProfileContractTests.test_observer_progress_receipt_is_private_bound_monotonic_and_non_authoritative` verifies its private source/run/profile-bound monotonic receipt remains non-authoritative.
`test_platform_profile_read_models` owns the profile cache-generation CAS service regressions in `backend-integration`; the real trigger/Redis ordering test in `test_platform_tournament_concurrency_integration` verifies join generation advancement and rejection of a stale leave delta.
The canonical AST snapshot is 2,162 tests (2,085 backend, including 704 privileged) across 171 modules, with every ID assigned exactly once. The 77 verification-contract tests are separately owned; the five backend contours are unit 314, tool-contract 565, integration 262, privileged 704, and performance-contract 240. `DraftReleaseSelectorTests` in `test_platform_secret_artifact_boundary` executes the actual selector with synthetic Git history and invalid provenance, checks manual force and stale-source refusal, and verifies that bundle generation returns to the repository root. The Wrangler runtime cap case executes both builder and publisher validators with a sparse oversized pinned-path fixture, then rejects an oversized sibling. The source-bound backup proof module contributes hermetic tool-contract cases, including the named-pair removal proof and tamper checks. Its helper-bundle workflow contract remains in the existing release-build contract module, whose full ownership is backend-privileged; it therefore runs in the isolated privileged contour. The maintenance-failure evidence/reset helper cases in `test_platform_recovery_bootstrap` are also backend-privileged and use synthetic receipt, unit and journal fixtures. The strict venv-reuse contract exercises the real proof CLI, policy/floor checks, transaction and pointer behavior, and receipt flow with test-copy runtime, integrity and space inputs. Separate tests cover the real runtime and wheel/RECORD integrity predicates. The machine-readable catalog in `platform_test_catalog.py` is authoritative for the exact test IDs and digests. The Ready Vote transport-pair profile is a separate diagnostic contract; it adds no production workflow input beyond the fixed profile choice and does not count toward the 11-profile load matrix. `test_platform_service_recovery_workflow` belongs to `backend-privileged` because its controller fixtures validate root-owned release metadata and systemd unit identity. It checks exact synchronization between the canonical controller and no-checkout workflow heredoc, active-release SHA/slug binding, narrow inactive-success prestate, API and node-bound Celery readiness, ordered API/worker/nginx startup, nginx config identity and loopback proxy health, timer-state preservation using only timer-applicable systemd properties, reverse-order rollback, relative and absolute release pointer acceptance with traversal/alias refusal, early refusal receipt projection through the exact workflow wrapper under nonroot, and YAML-parsed Bash syntax for each actual workflow script. Its worker-bootstrap tests execute the pinned runtime helper and safe environment loader as a nonroot identity with a test-only broker sentinel, validate Celery 5.6's nested worker ping envelope, distinguish closed probe failure classes under a fixed deadline, and refuse missing or malformed worker environments without projecting values. Its disk boundary case executes both controller copies and proves the exact `max(5 GiB, ceil(10% of total))` available-free floor, including reserved-block accounting. The production recovery path remains explicit workflow-only and requires post-release QA; these fixtures do not start host services.
`test_platform_release_capture_probe` belongs to `backend-tool-contract`. Its workflow-contract case keeps the existing diagnostics mode as the default and verifies isolation of the explicit read-only capture probe; helper cases use temporary release metadata, symlink fixtures and mocked unit-state reads to cover closed check results without touching host state. The actual pinned snapshot path also checks finite state/return-code observations for the failing owned-unit query without exposing command output or issuing another query. The default-off `--diagnostic-systemd-state` route separately validates the exact pending target and canonical systemd receipt, emits bounded private expected/observed unit facts, and does not run payload, nginx, health, restore or recovery actions; collection, validation, current-state comparison and historical-cause status are distinct. `test_platform_release_active_artifact_proof` also belongs to `backend-tool-contract`; it executes a read-only proof with a tiny synthetic archive, archive-derived payload comparison, generated bytecode validation, closed receipt/current-pointer refusals, traversal-error handling and no-write assertions. The existing `test_platform_recovery_bootstrap` owner verifies default-off reconcile, proof-success skip and proof-refusal stop behavior. Release retention tests own locked candidate-set rechecks and belong to `backend-privileged`.

`test_platform_backup_restore_drill` owns newest-pair restore, preserve-existing inventory, and named-pair proof behavior in `backend-tool-contract`; it verifies complete dump/sidecar pairing and content hashes, preserves every prior file identity, validates ordered pre-delete removal evidence, and rejects changed, incomplete or path-escaping pairs. The post-publication member-change regression additionally proves that the final inventory check rejects a changed old member and rolls back only the newly owned pair. Its schema-1/schema-2 private lifecycle diagnostic validator, closed host-memory guard-site frame, binary `/proc/<pid>/stat` start-time parser, closed first-fault projection, and optional private phase-boundary projection are also covered here, including a real self-process read, malformed byte fixtures, and preservation of the original controller failure when secondary diagnostic collection fails. Phase events are ordered boundary history with separate worker/controller clocks; tests keep malformed or partial observations secondary to the original guard. Its root-only isolated-restore signal, two-phase worker-authorization barrier, resource-guard lifecycle and scratch-directory mode-before-ownership capability regression tests are assigned to `backend-privileged`, where they exercise the real root-owned `/run` scratch boundary without skipping the coverage. These owner tests also keep the tmpfs default, propagate the selected backend through every restore resource guard, check the exact postgres owner after the disk-root transition, and exercise the child CLI failure JSON/frame pair through parent polling and cleanup without sending DONE. `test_platform_storage_maintenance` owns lock-held build-cache eviction/fallback-compaction integration and the canonical lock-aware backup workflow contract in `backend-privileged`, including schema-1 compatibility, closed schema-2 scratch-unit and host-memory diagnostic projections, the optional private phase-attribution handoff, dual-channel validation of the private host-guard receipt, rejection of malformed summaries, the full-maintenance preserve-all and protected-proof CLI/wiring contracts, and the exclusive no-archive authorization-probe CLI contract. Its backup workflow regression also executes the extracted remote normalizer call with runner-only uppercase flag variables absent, proving the validated lowercase positional values reach the real normalizer under Bash `nounset`. `test_platform_backup_probe_workflow_contract` owns source-bound authorization-probe workflow validation in `backend-tool-contract`; it checks exact Security/source/helper gating, the no-database/no-backup path, closed success/failure envelopes, durable private receipt binding/no-overwrite/no-upload, and first-fault/phase-attribution privacy. `test_platform_maintenance_qa_evidence` owns source-bound live-QA artifact validation in `backend-tool-contract`; its positive reports are emitted by the checked-in live-launch and live-user sanitizers with synthetic inputs, while fake API metadata and ZIP archives cover exact workflow/run/attempt binding, expiry, digest, malformed archive, closed report behavior and binding of the private protected-retention proof to the manifest and new archive. `test_platform_release_build_contract` covers pinned cache integrity/regeneration. New aggregate scratch-allocation schema/privacy, bounded metadata-walk, mount-identity, parent-root-view, and first-fault-precedence regressions also belong to `test_platform_backup_restore_drill` under `backend-tool-contract`; they use synthetic proc/mount records and disposable temporary trees and never read production paths or change a resource guard. Named-backup cases remain in these existing owners; the catalog remains authoritative and all tests use synthetic paths only.

The `test_synthetic_pg16_restore_diagnostic_under_root` case is assigned to `backend-privileged`. On the ephemeral Ubuntu 24.04 runner it builds a tiny deterministic PostgreSQL 16 custom archive and performs three serial restores inside the verified resource boundary. The default `tmpfs_v1` backend retains the existing 640 MiB, zero-swap, 512 MiB tmpfs systemd configuration. The separately selected `disk_ext4_image_v1` backend uses the reviewed fixed-size ext4 image and retains the same host and unit memory guards, isolation, durability, fixture and restore checks. Its output reports filesystem capacity and peak only as disk metrics; it does not emit tmpfs metrics or claim RAM savings. The report checks exact fixture rows, the Alembic marker and `pg_trgm`, and includes per-arm logical hashes, cgroup peak memory, WAL bytes and elapsed time. The sanitized `synthetic-restore-diagnostic.json` binds the selected backend and measurements to the tested source SHA. A standard Security `workflow_dispatch` on a non-`dev` ref may choose exactly one backend, defaulting to tmpfs. Push, pull-request, `dev`, and non-standard dispatches are forced to tmpfs regardless of dispatch input; the choice is not a production backup or deployment input. Run the tmpfs and disk choices as separate feature dispatches, and review the first artifact plus cleanup, health, storage and fresh resource admission before dispatching the second. Do not chain into disk after a failed tmpfs run. Compare the same fixture identity, PostgreSQL version, arm definitions and per-arm logical hashes, not archive hashes. Failure skips the full contour but still reaches the one existing always-upload step; success removes only the two fixed synthetic reports before the unchanged full contour. The early dispatch is diagnostic-only, while the full contour remains required for `dev` publication. Failure evidence retains the pre-teardown fixed unit snapshot and a bounded exact-unit journal query normalized to closed reason, namespace, errno and target enums; raw journal messages are never exported. Capture first requests bounded-time `journalctl --sync`; separate sync and query statuses distinguish synchronization from reading the query to EOF under caps, which does not claim a complete causal trace. Failed synthetic units are stopped, reset if still loaded and required to become `not-found` before mount cleanup. Before this synthetic unit starts, its root GitHub-hosted test provisions only missing directories from the complete fixed `InaccessiblePaths=` target set as root-owned mode `0700`, records inode identities, and removes only those still-empty created directories after unit unload; pre-existing targets remain untouched and unsafe layout/cleanup fails closed. This ephemeral fixture does not alter production host paths. The report is diagnostic-only: it proves real archive I/O for this small fixture, not a full application restore or production capacity. Workflow preflight fails if PostgreSQL 16 binaries or `pg_trgm` are unavailable, and does not start the runner's global PostgreSQL service.

`test_platform_backup_success_guard` is `backend-tool-contract`: fake GitHub API records and synthetic ZIPs exercise the closed exact-source backup receipt, complete backup-run and attempt-scoped job pagination, the conservative last-job-completion reservation bound, artifact digest, redirect credential boundary, and fail-closed polling behavior without network or production access. Executed jobs require ordered start/completion timestamps. On the validated `push` route, the conditional `Dispatch exact baseline proof finalizer` and `Authenticate internal baseline runtime proof` jobs may have inverted timestamps only in their exact terminal `skipped` API shapes. Both require a present `steps` field of `null` or `[]`; the auth job additionally requires all four runner fields to be present and explicitly null, matching the captured API row and its workflow-dispatch/baseline-runtime condition. Timestamps must still parse and fall within the exact run, and both completions remain in the conservative maximum. Tests reject other names, events, statuses, step shapes, runner shapes, out-of-run times and inverted executed jobs. The auto-deploy workflow contract in `test_platform_release_build_contract` proves that this guard must pass before its sole child-dispatch request, does not use the workflow event's nullable completion timestamp, and keeps full exact-source history available before resolving the pinned host-tools commit while disabling checkout credential persistence.
`test_platform_live_qa_guard` owns the reversible Chromium subtree transaction, partial-delete recovery, sandboxed probes and signed-provider verification, including bounded hashing of large runtime members, in the same contour.

The `platform-host-tools-candidate.yml` workflow is a separate trusted `workflow_run` contract, not
a production gate. Its validator and bundle tests belong to `backend-tool-contract` through
`test_platform_host_tools_bundle`, except the active-baseline reader test, which exercises
root-owned release metadata and belongs to `backend-privileged`; catalog snapshots track both. Its pin-resolver contract compares full history with a depth-one clone and requires a closed shallow-history diagnostic while preserving full-ancestry resolution.
The existing SSH-key contract generates an ephemeral OpenSSH key and validates its multiline private-key format with `/usr/bin/ssh-keygen`; that executable is a required test prerequisite. Negative configuration cases assert only closed reason codes and the existing exit contract, never secret values, digests or public-key output. If the parser executable is unavailable, report `LOCAL GATE BLOCKED` rather than skipping the case or substituting a mock parser.
The `verification-contract` gate runs pinned actionlint 1.7.12 on every workflow YAML file with shellcheck and pyflakes disabled; owner tests exercise positive and negative `runner.environment` context fixtures. Security CI downloads the official archive into private `RUNNER_TEMP`, checks its fixed SHA-256, and verifies its GitHub attestation against the exact release tag and [upstream release workflow](https://github.com/rhysd/actionlint/blob/v1.7.12/.github/workflows/release.yaml) before extraction or execution. The [installation guide](https://github.com/rhysd/actionlint/blob/v1.7.12/docs/install.md) and [GitHub CLI attestation command](https://cli.github.com/manual/gh_attestation_verify) document this verification; a missing exact local binary is `LOCAL GATE BLOCKED`, never a skipped workflow check.
The same gate invokes `host_tools_candidate_workflow_issues()` so default-branch guards, narrow permissions, pinned actions, data-only candidate handling, isolated-Python use, TOCTOU rechecks and production artifact isolation fail closed. A base-reachable pin is a successful no-op; only a novel eligible pin reaches build, attestation and upload. `test_platform_noop_source_binding` belongs to `backend-tool-contract` and covers the closed no-op receipt, distinct runner/application SHA binding, authenticated artifact metadata, bounded archive extraction and redirect handling without production access. Its output is review evidence only, not production authority.
Production workflows must not consume the candidate workflow's artifact prefixes.
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

Unknown paths/events or malformed provenance fail into full verification with
`fallback=true`, `deployable=false` and `release-runtime`; sensitive/fallback or
deployable `dev` pushes also require `release-runtime-real`. Recognized paths
use `fallback=false`; full verification alone grants no production authority.
Each consumer applies its policy to the authenticated, digest-bound manifest:

| Consumer | Docs-only / out-of-scope | Full routes |
| --- | --- | --- |
| Trusted PR candidate workflow | Authenticated no-candidate completion; no candidate artifacts | Strict full verification and existing candidate eligibility checks |
| Push auto-deploy | Docs-only uses authenticated baseline reconciliation with no app writes; out-of-scope completes no-deploy/no-backup | Preserve route policy: candidate uses source-bound baseline reconciliation and backup but grants no app-release authority; recovery uses baseline reconciliation, storage skips deployment/backup, ordinary eligible full push may deploy |
| Recovery bootstrap producer/publisher | Docs, out-of-scope, storage and candidate-packaging each produce their distinct authenticated skip receipt; no recovery bundle | Ordinary eligible full and recovery-bootstrap routes retain bundle validation; candidate-packaging is CI-only and grants no recovery authority |

Consumers bind route family, source, run/attempt and digest; malformed,
failed or mismatched evidence fails closed without retry, `continue-on-error`
or reduced-pass fallback. Candidate handling belongs to
`test_platform_host_tools_bundle`; classifier and recovery skip routes to
`test_platform_ci_classifier`; auto-deploy/provenance to
`test_platform_release_build_contract` and `test_platform_workflow_provenance`;
bundle validation to `test_platform_recovery_bootstrap`. The
[classifier](../tools/platform_ci_classifier.py) and release workflows own
implementation.

The event and conditional-job behavior follows the primary GitHub Actions
contracts for [workflow events](https://docs.github.com/en/actions/reference/events-that-trigger-workflows),
[job conditions](https://docs.github.com/en/actions/using-jobs/using-conditions-to-control-job-execution),
[workflow artifacts](https://docs.github.com/en/actions/using-workflows/storing-workflow-data-as-artifacts),
and [dependency caching](https://docs.github.com/en/actions/using-workflows/caching-dependencies-to-speed-up-workflows).

## Ownership rules

Use the lowest suitable layer in the test pyramid:

The host disk policy is a shared release/operations contract owned by
`tools/platform_disk_policy.py`. The health-monitor unit test and storage-
maintenance privileged test must cover the independent exact 5 GiB floor and
the exact 10% free / 90% used boundaries, fail-closed invalid usage, and the
conservative `total - available` formula with systemd/CLI wiring. A threshold
or formula change requires updating the policy helper, both consumers, the runbook owner in
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

Raw SSR metadata in `apps/platform_web/tests/smoke/platform-routes.spec.ts` comes from full-document `page.goto()` responses.
SPA flows check client URLs and hydrated DOM, preserving project/auth/fixture coverage without a second raw request.

## Local and GitHub verification

Local canonical gates provide fast developer feedback. They must use the
registry, for example:

```bash
cd platform
.venv_platform/bin/python tools/platform_verify.py list
.venv_platform/bin/python tools/platform_verify.py backend-unit --focused tests.test_platform_domain
.venv_platform/bin/python tools/platform_verify.py python-quality
.venv_platform/bin/python tools/platform_verify.py verification-contract
```

Impact planning is advisory; run focused feedback and every affected full gate.
`backend-unit --focused` is DB-free; `backend --focused` needs DB/Redis/privileged
resources. Local `ci` runs docs, python-quality, security and verification-contract
before migration, backend DB prep, backend, web-quality and web-hermetic; no gate
is skipped. Migration cleanup requires prepared resources; exact-SHA Actions gate publication.

For full local Python gate dependencies, see [the development guide](development-guide.md#python-dependency-ownership).

`LOCAL GATE BLOCKED` means required safe dependencies are unavailable; a smaller substitute is not a complete-gate pass.

The privileged storage-maintenance host-contract test is catalogued with its
GitHub-hosted Linux environment markers. The canonical verifier and runner
check those markers before launching the contour or preparing shared test
resources; a local run reports `LOCAL GATE BLOCKED` while the exact full
contour remains required on the hosted Security runner. Do not spoof runner
markers or skip the test's own host-safety assertions.

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
The dependency audit covers runtime, quality and security tool dependencies in the complete CI lock; setup-python caches follow their lock, and local bootstrap stays separate from hash-locked CI.
`backend-static`, `backend-integration`, `release-runtime` and `release-runtime-real` use `ubuntu-latest`; `backend-privileged` and `verification-contract` remain on
`ubuntu-24.04`. Keep jobs current and adapt fixtures/tooling to their contract. The privileged builder validates
setup-python 3.12 source/ABI, copies to private root-owned storage, then restores prefix mode 0700 after `cp -a`.
Check symlink containment, strip group/other write from copied non-symlinks and assert the predicate. Restrict
`LD_LIBRARY_PATH` to the copy; both venv bases resolve inside it. Full smoke checks each command, ABI/layout,
clean-env imports and both venvs; early-exit output is not a pass. Git archive excludes the copy/venv. Privileged
tests set `PLATFORM_RELEASE_PYTHON` to their actual interpreter; fake venv probes answer metadata queries.

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

The tracked browser journeys `apps/platform_web/tests/smoke/live-launch.spec.ts` and `apps/platform_web/tests/smoke/live-user-journey.spec.ts` belong to their respective production launch and Live User QA workflows; run only against the exact deployed `dev` SHA through those workflows, whose supervisors own fixture cleanup. The objective-patch case checks the current index/detail and two durable PNG URLs; its exact two-source UI-icon checks are hermetic, and the public case IDs/schema remain unchanged. Dispatch and recovery procedures are in [the CSP live-QA runbook](csp-live-qa-runbook.md).
The `test_platform_live_qa_wrappers` module belongs to `backend-privileged`. It owns fixed-enum early-stage diagnostics and the `engine_preflight` ordering/mode contract; `test_platform_live_qa_guard` owns the exact installed wrapper and locked child command. Preflight runs after `identity_report` and before account installation/provisioning under the existing QA UID, locks and read-only mounts; it has no account or database fixture. Only enumerated stages/checks are exposed. Failures before the trusted Python dispatcher starts remain unclassified, and markers do not change workflow failure or cleanup behavior.
The same module owns public browser failure-report validation, private root-store publication/retention, exact gate/source/marker binding, cleanup survival and closed `failure_report_unavailable` propagation; workflow tests prove it cannot make passing or missing counts successful, while `web-hermetic` covers the CommonJS reporter's closed projection, safe locations and byte/mode limits. These tests use no browser, server, database or production QA fixture.
The same module owns the nested Live User QA timeout route: the fixed desktop journey gets its browser-child budget, other captured browser actions retain the default, and dispatcher timeout tests verify process-group termination. `test_platform_release_audit_hardening` also owns the fixed dispatcher-budget and unchanged-operation-limit assertions.
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

The canonical load-profile registry is `platform/performance/`. Profiles record fixture shape, logical actions, HTTP attempts,
concurrency, spread/ramp, retry semantics, expected statuses, correctness, latency budgets, resource evidence and cleanup. The
external HTTP generator runs on the GitHub-hosted runner. The production host performs only bounded fixture, observer and
exact-cleanup work; it never generates the measured client load. Production SSH secrets are scoped only to trusted fixture,
finalization and cleanup steps: checkout persists no credentials, and each checked-out client or evaluator runs with an explicit
allowlist while private keys, SSH auth sockets and control paths are absent from the runner. The external-load workflow contract
executes cleanup-evidence handoff from finalizer copy through origin artifact and evaluator sanitization; missing transcripts fail
projection and final acceptance. The same contract applies to every production workflow: SSH secret expressions are step-local
only, and checkout, artifact upload/download and checked-out client steps must not inherit them. Read-only web-runtime diagnostics
upload only fixed-schema aggregate summaries; producer/helper failures fail closed and successful empty input is the only
permitted empty result. Candidate checkout/build/load jobs and environment-approved SSH jobs are separate fresh jobs. A
secret-bearing job never checks out the candidate or executes a candidate-produced program; it consumes only a fixed-path,
closed-schema handoff or immutable release archive after inline validation of the exact target SHA, artifact digest and
provenance. Long-running origin supervisors detach at the trusted remote dispatcher boundary, so a local background child cannot
cross into a later candidate job or retain runner credentials. The production deploy DAG keeps its dispatch validator always
scheduled, builds and publishes the candidate artifact only in the separate non-secret build-release job, and requires both
validator and build success before the production job can consume that exact artifact. The environment-approved production job has
no checkout or candidate build executable; verification-contract owns this DAG assertion. The standardized production SSH
workflows that materialize credentials use a literal, per-workflow `$RUNNER_TEMP/...-ssh` directory; setup and cleanup reject a
directory symlink. An `always()` cleanup removes only the named key, `known_hosts`, config and control-socket paths, verifies each
path and the directory are absent, clears `SSH_DIR` through `GITHUB_ENV`, and gates any later artifact action on cleanup success.
Cleanup is never `continue-on-error`, so a cleanup failure keeps the job failed without replacing the primary operation result.
Their host-key setup uses two fresh mode-600 scans, each bounded by `timeout --foreground 4s ssh-keyscan -T 3`, with a
0.5-second backoff. It ignores blank/comment lines but requires exactly one exact-host Ed25519 record matching the pinned
fingerprint; SSH host checking stays strict. Failure diagnostics expose only status, counts and validation booleans. Dynamic
contract tests cover all workflow sites, OpenSSH banners, retries, malformed/multiple/mismatched keys and cleanup.

Every retained result identifies its source SHA, profile ID/version/digest,
runner, fixture shape, offered logical actions, HTTP attempts and acceptance
outcome. A performance result is incomplete unless correctness and exact
cleanup pass. Canceled runs use the matching abort/cleanup workflow keyed by
the exact run ID.

CPU-pair contracts belong to `test_platform_cpu_profile` and `test_platform_external_load_workflow_contract`: bind runner `SOURCE_GIT_SHA` independently from application `APP_TARGET_SHA`, validate CSRF as the issuer's 43 URL-safe characters, period, and 64 lowercase hexadecimal HMAC, and exercise the fixed two-key `/proc` environment projection without copying unrelated values. The CPU profile owner also covers the root-plan refusal token across helper, dispatcher and runner, and verifies that cleanup errors retain a distinct code without replacing the first run failure. CPU-only abort evidence remains separate from canonical measurement results and cannot set namespace closure or award diagnostic credit. Keep session-token validation separate and preserve authentication, profiling, pairing and threshold guards. The web-quality gate checks the Node emitter against `tests/fixtures/cpu-diagnostic-memory-complete-v1.protocol`; the Python CPU-profile owner test consumes that same golden event and evaluates the complete cgroup projection, while rejecting legacy field names. This keeps the Python contour independent of an unpinned Node executable. Missing canonical interpreter means `LOCAL GATE BLOCKED`; supplemental tests are reported separately.

External-generator capacity attribution is owned by `test_platform_external_load` and `test_platform_evidence_privacy`. Behavioral tests exercise the actual closed-loop and paced producers: unavoidable positive start jitter does not become a permanent client-limit verdict, while sustained p99 queue/schedule lag beyond the 2 ms diagnostic floor does. The floor affects only capacity attribution; raw timing and SLO acceptance remain unchanged. Runner quota claims require bound outer cgroup evidence and sanitizer cross-checks against the paired client timing/resource samples; missing or inconsistent evidence remains UNKNOWN.

## Contract self-test

The privileged contour owns immutable recovery-bootstrap tests (`tests.test_platform_recovery_bootstrap`); backend-tool-contract owns cumulative-baseline provenance tests (`tests.test_platform_workflow_provenance`).
Verification-contract owns failed-report caller tests and the synthetic `unittest.subTest` accounting regression: multiple failures/errors count as one executed parent ID while native failure/error rows remain in the summary. Backend-tool-contract also owns the deferred-origin budget-failure pipeline (`tests.test_external_load_pending_origin_pipeline`).
Contract tests cover closed bundles, exact manifest/provenance schemas, content-addressed installation, receipt identity, migration-uncertainty guards, and secret/SSH ordering. `tests.test_platform_release_systemd_state` covers operation-ID/path/inode mismatch, helper-manifest tampering and rollback-target retry binding; `tests.test_platform_release_recovery_boundaries` covers rollback/recovery subprocess faults.
`test_platform_release_recovery_boundaries` remains `backend-privileged` and owns populated-quiesce quarantine, exact-receipt resume, fail-closed identity/collision cases, and the separate signed `prune_quarantined_candidate` transaction. Pruning fixtures prove original receipt bytes stay unchanged, only the exact consumed candidate is removed, tombstone replay does not touch paths, stale F/receipt/sidecar/pointer bindings refuse, mutation before the first unlink stops, interrupted deletion resumes only the same stage inode, and symlink/mount/cross-device trees are not followed or removed. Recovery-bootstrap tests own signed capability, workflow source-bound U/F argument and success-marker contracts, as well as attestation variants and the legacy-v2 no-systemd bridge; live recovery remains workflow-only.
Migration diagnostics use a bounded mode-600 local temp report retained on failure; CI does not upload it, and ephemeral runner files disappear after the job.

The `docs` gate checks document shape, repository-local links and project skill
frontmatter/interface metadata. `verification-contract` checks registry/CI
membership, workflow gate names, classifier route ownership and artifact
guards, direct command duplication, exclusion bypasses, backend discovery,
hermetic suite registration, production reachability from `ci`, documentation
gate IDs, load-profile schema/deduplication, workflow-owned load budgets and
the external-generator topology. Keep it deterministic and small enough to run on every CI change.
