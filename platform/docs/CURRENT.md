# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-21

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production runs application source
  `59b6af86cc3e3215c47c03f76ff105951b4b8906` in release
  `gha-44908202177-1-59b6af86cc3e`; `previous` is source
  `88e4154661e76e8e4a2aac89a2ac592195d1fcde` in release
  `gha-44895292289-1-88e4154661e7`. Host-tools pin
  `d0f60dc1d93a463a529592ff8c7dd59de398cead` selects installed C23
  `23e76706728064defeff1024c48e939cd1a8f6bc` under manifest
  `production-host-tools-v4`; the prior AA4 generation
  `aa4f9e075156bd9aadb699eb3a13253e80daf79a` remains preserved. The active
  runtime manifest is v3 with Node 26.3.1. Alembic current and head are both
  `20261115_0056` (admin tournament test fixtures).
- Source Q passed exact-SHA Security run `44901850777/1` and the
  restore-verified source-bound backup `44902923665/1`. AUTO
  `44902801760/2` succeeded, followed by production deploy `44908202177/1`.
  The deployment installed Q and C23 and retained the previous app and AA4
  generation. Use the deployment and backup runbooks for the source-bound
  receipts and recovery procedure.
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

- Deployment and installed readiness passed for Q. Automatic QA parent
  `44908919750/1` failed its exact-public-success gate because public child
  `44908938481/1` exited 2 during fixture provisioning, before browser tests.
  UserQA was not dispatched. The canonical active-Q `recover-bundle` procedure
  later completed under lock with exact cleanup and absence proof; this cleanup
  does not change the failed sequencer result or earn QA pass credit.
- A separate source-bound public run used `provision=false` and an empty marker:
  `44911090229`. It did not provision or consume roster credentials. The
  browser suite reported 46 passed, 10 skipped, 1 failed, and no flaky or
  interrupted cases. The closed failed identity is `image_csp_hosts` on
  `live-webkit-mobile`, attempt 1, at
  `tests/smoke/live-launch.spec.ts:1010:67`. That assertion checks whether the
  image inventory completed; page-error state was absent. This does not
  establish a Cloudflare, CSP or network cause. UserQA and its cleanup child
  have not been dispatched, and there is no passing public-browser QA result
  yet. Do not treat installed-origin API acceptance as public-browser QA.
- Raw HTTP probing returned Cloudflare Error 1010 (`browser_signature_banned`),
  which is a blocked client signature rather than evidence of an application
  route failure. Do not retry that raw probe or weaken Cloudflare protections;
  use canonical browser QA for rendering evidence.
- The failed gate/process and provider mount targets are absent, and the
  installed Q guard preflight passed. The canonical read-only storage
  diagnostic and retention preview passed; 6,947,573,760 bytes were free
  (82.62%), with zero QA cache candidates and zero transient/browser artifacts.
  No filesystem retention deletion was performed. The exact fixture recovery
  is recorded above; no UserQA or public QA pass is claimed.
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
