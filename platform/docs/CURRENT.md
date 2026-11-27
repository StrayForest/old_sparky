# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-27

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production currently runs application source
  `9cc0f9676709552f68b6ad9c15bf5b66f663c71c` in automatic release
  `gha-45999843320-1-9cc0f9676709`; exact-SHA Security run `45997206430/1`,
  source-bound backup `45999314416/1` with restore verification, deployment
  and post-deploy health passed. The separately installed shared host-tools
  generation is `ec2506d0099f788d210b84e3ad3001431bfeeced`, with installed
  receipt digest
  `1c1958ec5799180c88743932f9344145cd1c29d3db07a237380f2b85dc5516f5`.
  The host-tools pin and app release are separate identities. See the
  [host-tools provisioning ADR](adr/production-host-tools-provisioning.md) and
  [deployment runbook](deployment-runbook.md) for their source-bound procedures.
- Installed-origin API acceptance for patch `1848664643342973` passed: the
  Russian patch detail returns 27 changes, with the leaderboard heading at
  index 21, its four descriptions at indexes 22–25, and the Beta Season 2
  announcement separately at index 26. This preserves the group through
  catalog-driven cached-detail reparsing and translation merge.
- The active source includes admin manual tournament fixtures and migration
  `20261115_0056_admin_tournament_test_fixtures.py`. Admins can create and
  resume a marked fixture, prepare synthetic participants, generate teams
  through the normal workflow and explicitly clean up only fixture-owned data.
  The session list refreshes on entry or when the admin selects
  “Обновить сессии”. See the [manual testing guide](admin-console-information-architecture.md#manual-testing)
  and [product contract](product-reference.md).
- Human Chrome and live Cloudflare challenge/scoring remain optional
  diagnostics; the CI Siteverify fixture does not establish production
  Cloudflare scoring.
- Historical timeout and uncredited-run detail is in the
  [closeout audit](archive/simplification-performance-audit-2026-11-13.md).
- Production backup, deployment and source-binding rules remain owned by the
  [deployment runbook](deployment-runbook.md) and
  [backup and restore runbook](backup-restore-runbook.md).

## QA and cleanup gates

- After successful roster provisioning, the deployed release includes
  automatic exact cleanup for a confirmed terminal Public failure and
  preserves the fixed browser-output protocol.
- Standalone Public QA for source
  `9cc0f9676709552f68b6ad9c15bf5b66f663c71c` run `46001343837/1` completed
  with 42 passed, 5 failed, 10 skipped, 0 flaky and 0 timed out. Its cleanup
  receipt is `cleaned`; this records cleanup completion only, not a QA pass or
  proof of historical database absence. UserQA remains pending until Public
  passes. The retained private report shows all 128 patch images loaded and
  zero unmatched CSP violations. In the failed run's immutable 9cc0
  `live-launch.spec.ts:489`, the `image_csp_hosts` afterEach assertion recorded
  a cancelled same-navigation `fetch`; the retained evidence does not identify
  its endpoint. `patch_owner_sections` at 9cc0 `:989` failed on desktop, mobile and WebKit mobile
  because the assertion expected two Solomon `Move Piece` changes, while the
  retained public DTO has one source row and one owner change. The test's
  expected count is stale. Desktop `objective_icons` failed at 9cc0 `:842`; the
  report does not preserve the screenshot exception. A focused local screenshot
  of the latest DTO completed within the test timeout, but the historical
  exception remains unknown. See the [CSP/live-QA runbook](csp-live-qa-runbook.md)
  for the evidence contract and recovery procedure.
- Read-only storage diagnostics `46006095877/1` are bound to active source
  `9cc0f9676709552f68b6ad9c15bf5b66f663c71c`: disk use is 61% with about
  13.96 GiB free, the storage lock is unlocked, API/worker/web services are
  healthy, the maintenance timer is enabled and matches its installed unit,
  and the latest backup is restore-verified. The LiveQA cache preview found
  no candidates or tombstones. The generic release preview is report-only and
  is not an apply set; no retention cleanup was performed. The CI Siteverify
  fixture does not establish production Cloudflare challenge/scoring.
- The 11-cell production matrix is complete, but cell 9 failed its stress
  contract with unknown capacity attribution; no server capacity ceiling is
  certified. Cell 7's client/origin request join remains unresolved. A recent
  SSR composite recovered bounded profile attribution, but its workflow report
  remains incomplete and receives no capacity or SLO credit. Another SSR
  diagnostic has one unexplained ON response disconnect. These findings do not
  establish a production capacity limit or a root cause. Use the
  [performance matrix](../performance/README.md#current-11-profile-production-matrix)
  and [SSR diagnostic runbook](ssr-capacity-diagnostic-runbook.md) for scoped
  evidence and procedures; no matrix rerun is pending.

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
