# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-28

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production currently runs application source
  `7895e32da29a6e53a8d8e6cceeb45e2aea93ace1` in automatic release
  `gha-46097693180-1-7895e32da29a`; exact-SHA Security run `46090132339/2`,
  source-bound backup `46097392702/1` with restore verification, deployment
  `46097693180/1` and post-deploy activation passed. The separately installed
  shared host-tools generation is `b655b2969c70e8787805d98ebb61a8a3e6a7bd74`.
  The
  host-tools pin and app release are separate identities. See the
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
- Automatic post-deploy QA for source
  `7895e32da29a6e53a8d8e6cceeb45e2aea93ace1`, deployment parent
  `46097693180/1` and exact Public child `46098379744/1` failed with 55 passed,
  1 failed, 10 skipped, and 0 flaky, timed-out, interrupted or retried
  attempts. Public cleanup is `cleaned`; UserQA was not dispatched. The sole
  failure was `objective_icons` during its final screenshot capture, classified
  as `protocol_capture_error`; the retained evidence does not identify a more
  specific capture cause. The test already passed its route, HTTP/MIME, PNG
  signature and overflow assertions. A pending change makes this artifact
  viewport-sized while preserving those checks; the failure is not yet
  resolved. See the [CSP/live-QA runbook](csp-live-qa-runbook.md).
- The installed-unit-bound storage retention preview after Public child
  `46098379744/1` completed in dry-run mode: 4 user-cache candidates, 3 busy,
  0 removed; 6 unregistered task directories were left untouched. The
  maintenance timer remains active and enabled. No retention deletion was
  performed. The CI Siteverify
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
