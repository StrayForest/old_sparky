# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-21

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production runs application source
  `91e59493901c186c0316eef912f5d9de7f7d11cb` in release
  `gha-44919878014-1-91e59493901c`; `previous` is source
  `59b6af86cc3e3215c47c03f76ff105951b4b8906` in release
  `gha-44908202177-1-59b6af86cc3e`. Host-tools pin
  `d0f60dc1d93a463a529592ff8c7dd59de398cead` selects installed C23
  `23e76706728064defeff1024c48e939cd1a8f6bc` under manifest
  `production-host-tools-v4`; the prior AA4 generation
  `aa4f9e075156bd9aadb699eb3a13253e80daf79a` remains preserved. The active
  runtime manifest is v3 with Node 26.3.1. Alembic current and head are both
  `20261115_0056` (admin tournament test fixtures).
- Source S passed exact-SHA Security run `44918125474/1` and the
  restore-verified source-bound backup `44919533488/1`. AUTO
  `44919430203/1` authenticated the cumulative Q-to-S range, verified the
  existing C23 generation and dispatched the baseline-reconcile child
  `44919878014/1`. The child installed S as an app-bearing release, with no
  host-tools generation change. Its exact deployment receipt records mode
  `baseline-reconcile`; use the deployment and backup runbooks for the
  source-bound receipts and recovery procedure.
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

- The earlier automatic parent `44908919750/1` remains failed: its public child
  `44908938481/1` exited during fixture provisioning before browser tests. The
  old roster was later removed through the exact active-Q `recover-bundle`
  procedure. S is an app-bearing baseline-reconcile release, so the automatic
  post-deploy QA handoff was intentionally skipped and earns no pass credit.
  The required standalone public and UserQA workflows were run against S.
- Public S run `44921172350/1` passed with 47 cases passed, 10 skipped, and
  zero failed, flaky or interrupted cases. Its sanitized report binds source
  and app to S and records a successful child exit. The three prior
  `patch_owner_sections` cases and the earlier image-inventory gate passed on S.
  UserQA S run
  `44922668026/1` passed its complete 1-case contour. The trusted EXIT cleanup
  proved absence for its exact roster/session/tournament/media inventory and
  removed the root-only run state; the workflow's bound success marker and
  final summary passed. The exact root-only CSP bundle and mailbox helper were
  retired under the release and LiveQA locks after that proof. No task-owned
  fixture rows or credential files remain.
- The earlier Q standalone public run `44911090229/1` failed
  `image_csp_hosts` on `live-webkit-mobile`, at the image-inventory completion
  assertion; its page-error state was absent and no cause was established.
  S public QA passed that gate. Raw HTTP probing returned Cloudflare Error 1010
  (`browser_signature_banned`), a blocked client signature rather than
  evidence of an application route failure. Do not retry that raw probe or
  weaken Cloudflare protections; use canonical browser QA for rendering
  evidence.
- Final read-only storage diagnostic `44924655082/1` passed for S. Release
  locks were clear, services and the maintenance timer were healthy, and the
  restore-verified backup was 0.931 hours old. The filesystem had 6,591,004,672
  bytes free at 83.51% used, above the 5 GiB/90% limits. The LiveQA cache preview
  had zero candidates. Seven unrelated release-retention candidates remained
  in the preview; no general release pruning was performed.
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
