# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-10

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production app and runner source S are
  `e1299e4818c8b2f684a76af5424109532d0c71a0` (release
  `gha-42806619801-1-e1299e4818c8`); previous app is
  `2835f9b2144c7b1d56e04bfdd0169f7912e06ffd`. Active host-tools C2 is
  `c92dce69ed204d39b16884e62fad2b61addaa996`. Exact-SHA Security
  `42803016028/1`, AUTO `42804838560/1` and automatic production child
  `42806619801/1` completed. Keep S, app and C2 identities explicit. Storage
  maintenance `42811006436/1` completed with one restore-verified backup pair
  retained. SSR diagnostic fixture `42808854934/1` is cleaned; its exact
  cleanup and post-cleanup FULL readiness passed. Obtain fresh canonical FULL
  readiness before any new production work.
- SSR diagnostic `42808854934/1` completed its 120-second capacity phase with
  3,965/3,965 HTTP 200 responses. The separate 20-second profile phase is
  incomplete: 310/310 requests completed, but only 35 of 37 selected ON-phase
  Web roots joined to API request-performance rows, and the Web CPU profile
  missed its start deadline. The composite earns no capacity, SLO or
  performance credit. Do not treat partial profile data as a complete
  comparison or capacity result; use the
  [SSR capacity diagnostic runbook](ssr-capacity-diagnostic-runbook.md) for
  the phase acceptance and cleanup contract.
- Cell 9, `read-mix-concurrency-ramp-v1`, completed as run `41937909055/1` on
  runner S and app A, both `7c6034efe3878c3413090ca1bcdb5be6e93154e8`, with
  host-tools C `0de51aca4b4d04a80a2f0d392003cc258d8f1400`; exact cleanup
  passed. All eight stages observed 40/40 active Ready Checks and passed the
  state guard, with minimum deadline headroom of about 2,424 seconds. All
  170,000 HTTP actions succeeded (160,000 HTTP 200 and 10,000 conditional
  304), with no errors, retries or drops. The stress contract failed, and its
  sole failed evaluator check was `capacity_ramp_evidence`; per-stage logical
  p95/p99 budgets failed at all eight stages. Aggregate logical p95/p99 were
  209.4/331.2 seconds against 12/20-second limits; HTTP p95/p99 were
  886/1,135 ms. Capacity attribution is unknown (`runner_window_incomplete`):
  all offered ramp tasks started late, none were
  dropped, and this run certifies no server capacity ceiling. See the
  [performance matrix](../performance/README.md#current-11-profile-production-matrix).
- Cells 1–8 and 10–11 completed on the previous P4/C baseline. Their contract
  outcomes and cleanup status remain in the matrix table. The corrected SSR
  capacity diagnostic is separate evidence, not a matrix cell; use the
  [SSR capacity diagnostic runbook](ssr-capacity-diagnostic-runbook.md) for
  its acceptance and cleanup contract. Do not switch app or host-tools
  profiles during an active cell.
- Production backup, deployment and source-binding rules remain owned by the
  [deployment runbook](deployment-runbook.md) and
  [backup and restore runbook](backup-restore-runbook.md).

## Open QA and cleanup gates

- All 11 matrix cells have run with authoritative cleanup. Cell 9 passed its
  state guard but failed the stress contract; its capacity attribution is
  unknown and no server ceiling is certified. No matrix rerun is pending. Do
  not repeat completed cells or describe this external HTTP matrix as whole-site
  functional coverage. See the
  [matrix procedure and scope](../performance/README.md#current-11-profile-production-matrix).
- Cell 7's authenticated-page diagnostic still has unresolved client/origin
  reconciliation: the repeat had 19,971/20,000 successes, 28 status-0 and one
  status-502 failure, while Nginx recorded 19,990 HTML requests, all 200.
  There is no client-request join, so the transport cause remains unknown.
- The older SSR composite diagnostic `42081912538/1` on app `9a73f635` stopped
  after 70.385 seconds of its 120-second capacity phase. Four body-receive
  `incomplete_response` failures observed HTTP 200 before the response body
  ended; the bounded Nginx error-log check found no matching entries. Its
  cause and any server capacity ceiling remain unproven. The later corrected
  run `42788095845/1` does not retroactively explain or validate that older
  result. The separate CPU-only diagnostic `42085780534/1` also remains
  unclassified because its parent report has no phase data or failure code.
  Keep these older runs at zero credit and follow the owner runbook before any
  further production profile.

## Hard operational limits

- Preserve at least `max(5 GiB, ceil(10% of total disk))` free space. Include
  backup, restore-image, scratch and temporary allocations when admitting a
  production operation. See the [disk free-space policy](adr/disk-free-space-thresholds.md)
  and [operations runbook](operations-runbook.md).
- Keep local linked-worktree release builds in the shared canonical
  `platform/dist/releases` owner path; production CI keeps its per-run output
  override. Installer retention removes unreferenced runtime extras while
  preserving current, previous and recursive references. Daily maintenance
  owns browser-cache cleanup, not provider-payload pruning.
- GitHub Actions is the release authority. Deploy only through the successful
  exact-SHA Security and automatic production chain on `dev`; do not call
  release installers directly or dispatch the normal deployment manually.
  Rollback switches application releases and does not downgrade Alembic.
  See the [deployment runbook](deployment-runbook.md) and
  [release state machine](release-state-machine.md).
- Platform data uses `platformdb`, schema `platform`; do not touch the retired
  legacy bot or `sparkydb`. Preserve authorization, lock, restore and
  provenance checks. See [production architecture](production-architecture.md)
  and [product reference](product-reference.md).
- Operator inputs remain in
  `/root/old_sparky/.local/private/operator-inputs.md`; never put their values,
  private reports or live personal data in source, logs or artifacts.

## Owner references

- Current product and workflow contracts: [product reference](product-reference.md).
- Release, recovery and retained-load procedures: [deployment runbook](deployment-runbook.md),
  [service recovery](service-recovery-runbook.md), and
  [retained-load cleanup](retained-load-and-web-verification.md).
- Storage and backup policy: [operations](operations-runbook.md),
  [backup and restore](backup-restore-runbook.md), and
  [test-suite governance](test-suite-governance.md).
- Detailed performance, storage and historical measurements:
  [performance evidence](../performance/README.md) and the linked
  [archive](archive/).
- Historical release and storage-recovery decisions:
  [October 2026 checkpoints](archive/storage-release-checkpoints-2026-10.md).
- Prioritized engineering work: [platform roadmap](platform-roadmap.md).
