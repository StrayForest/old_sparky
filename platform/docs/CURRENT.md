# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-23

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production runs application source
  `ee1703a14968232a02c793b3b0fc4be98bde93c4` in release
  `gha-45131172511-1-ee1703a14968`, deployed by automatic child
  `45131172511/1`. Exact-SHA Security `45128995964/1`, source-bound
  restore-verified backup `45130756642/1`, and AUTO `45130690722/1` passed.
  Host-tools generation
  `da9981d6a7e97ec6aeea8501b02218f6d78fb50f` is unchanged and installed.
  Deployment smoke and post-maintenance health passed; a Public QA gate is
  failed and UserQA has not run. This CURRENT update is documentation-only and
  does not change the active application source.
- Active application source is
  `ee1703a14968232a02c793b3b0fc4be98bde93c4`; previous release remains
  `gha-45047467972-1-7fd08091df9e`. The automatic release workflow reported
  all six services active, API live/ready and web returning 200. Local and
  origin health checks returned 200; public homepage returned 200, while the
  direct public API health URLs returned 403. The 403 responses are an
  observed, unattributed boundary and do not override the successful origin
  and deployment smoke checks. No release operation is pending.
  See the
  [deployment runbook](deployment-runbook.md) and host-tools ADRs for the
  source-bound procedure.
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

- After successful roster provisioning, the deployed release includes
  automatic exact cleanup for a confirmed terminal Public failure and
  preserves the fixed browser-output protocol.
- Automatic post-deploy QA sequencer `45132147425/1` failed after Public QA
  child `45132182150/1`: 46 passed, 10 skipped, 1 failed. The failure was
  `image_csp_hosts` on `live-webkit-mobile`; exact-source private evidence
  showed one unloaded home image among 10, with no missing `currentSrc` and no
  page error events. The cause is unknown. This is compatible with the
  previously documented intermittent unloaded-WebKit-image issue, but the
  same root cause is not established. Public failure fixture cleanup completed
  (`public_failure_cleanup=cleaned`); UserQA was not run and receives no QA
  credit. No CSP policy, assertion or timeout change is supported by this
  evidence. See the [CSP/live-QA runbook](csp-live-qa-runbook.md) for the
  evidence and recovery contract.
- Storage diagnostics `45133282711/1` and authorized bounded maintenance
  `45134350443/1` completed successfully for source `ee1703a`. Current and
  previous releases were protected; one older unprotected release was removed
  (281,836,950 bytes), and no LiveQA cache or transient files were deleted.
  One restore- and Alembic-verified backup pair was retained across 39 tables;
  the new archive is `platformdb-20261123T075333Z.dump` (135,104,179 bytes).
  Final disk usage was 48.64% with 20,531,122,176 bytes free. All six services
  were active; the maintenance timer remains enabled and waiting, and the
  maintenance service exited successfully. The CI Siteverify fixture does
  not establish production Cloudflare challenge/scoring.
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
