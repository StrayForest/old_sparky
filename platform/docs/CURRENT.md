# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-22

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production runs application source
  `f500d04ddbdee97e3fa0843a71a60573ebd27d70` in release
  `gha-44962373348-1-f500d04ddbde`; `previous` is source
  `91e59493901c186c0316eef912f5d9de7f7d11cb` in release
  `gha-44919878014-1-91e59493901c`. The current `dev` source is
  `cc78e2f5c962a235074ff49c6e6bab1749b6b731`. Host-tools pin
  `d0f60dc1d93a463a529592ff8c7dd59de398cead` selects installed C23
  `23e76706728064defeff1024c48e939cd1a8f6bc` under manifest
  `production-host-tools-v4`; the prior AA4 generation
  `aa4f9e075156bd9aadb699eb3a13253e80daf79a` remains preserved. The active
  runtime manifest is v3 with Node 26.3.1. Alembic current and head are both
  `20261115_0056` (admin tournament test fixtures).
- The active app release S passed exact-SHA Security run `44960656347/1`,
  source-bound restore-verified backup `44962121912/1`, AUTO
  `44961874268/1`, and production deployment `44962373348/1`. The deployment
  completed successfully; use the deployment and backup runbooks for its
  source-bound receipts and recovery procedure.
- Current `dev` source cc78 passed Security `44965031579/1`, source-bound
  backup `44966253447/1`, AUTO `44965973383/1`, and production child
  `44966535234/1`. The child was a successful authenticated `verified-noop`
  for the full f500-to-cc78 range (`runtime_required=false`, artifact
  `14732135289`); it did not replace the active app release S. It fixes future
  ordinary-upgrade activation of the operator storage-retention timer.
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
- Production backup, deployment and source-binding rules remain owned by the
  [deployment runbook](deployment-runbook.md) and
  [backup and restore runbook](backup-restore-runbook.md).

## QA and cleanup gates

- Public S run `44963028353/1` passed with 47 cases passed, 10 skipped, and
  zero failed, flaky or interrupted cases. UserQA S run `44963545588/1` passed
  its complete 1-case contour, and automatic parent `44963009243/1` succeeded.
  The exact QA credential files were retired through the documented owner
  procedure. This QA evidence applies to f500; the cc78 verified-noop does
  not receive new application QA credit.
- The operator storage-retention timer is installed, enabled and active. Its
  service completed a successful apply; the timer uses the reviewed resource
  limits of 25% CPU, 512 MiB memory and 30 minutes. Exact one-time cleanup
  removed 69 obsolete registered worktrees; the canonical checkout is the
  sole registered worktree. Maintenance run `44967525771` succeeded against
  active release f500 using the authenticated cc78 verified-noop binding. It
  removed five reviewed unpinned obsolete releases while protecting current
  and previous. The fresh canonical preview is healthy with current and
  previous protected, six releases retained, no release deletions pending,
  one runtime cache retained and no transient candidates. Exactly one complete
  restore-verified backup pair is retained. The filesystem is 47% used with
  20,530,745,344 bytes available; the current/previous releases are f500/91e594,
  services are healthy, and no release transaction is present. The retention
  timer is active and enabled; its post-apply preview found zero candidates.
  Human Chrome and real Cloudflare challenge/scoring remain optional
  diagnostics; the CI Siteverify fixture does not establish production
  Cloudflare scoring.
- The completed external HTTP matrix is not whole-site functional coverage.
  Cell 9's attribution failure and cell 7's unresolved client/origin join are
  not closed by the separate SSR diagnostics. No cause or server capacity
  ceiling is established. Do not rerun completed cells; use the owner
  [performance matrix](../performance/README.md#current-11-profile-production-matrix)
  and [SSR diagnostic runbook](ssr-capacity-diagnostic-runbook.md) for any
  separately authorized follow-up.

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
