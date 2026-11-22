# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-22

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production runs application source H
  `7fd08091df9e4a442da59fe907af6302911aa839` in release
  `gha-45047467972-1-7fd08091df9e`, deployed by automatic child
  `45047467972/1`. Host-tools generation C
  `da9981d6a7e97ec6aeea8501b02218f6d78fb50f` is installed and its pinned
  `host-capabilities` self-test passed.
- Current `dev` source H is
  `7fd08091df9e4a442da59fe907af6302911aa839`. Its exact-SHA Security run
  `45045592748/1`, source-bound restore-verified backup `45047169182/1`,
  AUTO `45046961891/1`, and production child `45047467972/1` passed. Final
  health checks reported all six services active, API live/ready and web
  returning 200; no release operation, recovery state or QA gate remains.
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
- Current H QA: automatic parent `45048265499/1` and launch child
  `45048286058/1` failed during provisioning before browser execution. The
  documented standalone Public run `45048858491/1` passed (57 cases: 47
  passed, 10 skipped, none failed, flaky, timed out or interrupted), followed
  by standalone UserQA `45049634886/1` passing 1/1. Exact roster cleanup and
  database absence proof passed; post-UserQA credential cleanup removed the
  fixed bundle and helper. The automatic parent did not receive ordered QA
  credit. The new `provision=false` terminal-failure recovery path is covered
  by tests but was not exercised by this passing Public run.
- An anonymous HTTP check of the homepage, home API and nine unique image URLs
  found HTTP 200, permitted image hosts in the page CSP, valid image MIME
  types and successful image decoding. It did not identify the WebKit image
  previously reported as unloaded or establish a cause; that intermittent
  browser issue remains unresolved. See the CSP/live-QA runbook for the
  supported recovery path.
- The operator storage-retention timer is installed, enabled and active. Its
  service completed a successful apply; the timer uses the reviewed resource
  limits of 25% CPU, 512 MiB memory and 30 minutes. Retention run
  `45050481188/1` passed: it retained the current and previous releases and
  one verified backup, removed two older backup files and three unpinned old
  releases (847,136,876 bytes), and made no cache or transient-file deletions.
  The retained backup `platformdb-20261122T173739Z.dump` passed checksum,
  restore and Alembic verification across 39 tables. Final storage was 47%
  used with 20 GB free and 18% inode use. Human Chrome and real Cloudflare
  challenge/scoring remain optional diagnostics; the CI Siteverify fixture
  does not establish production Cloudflare scoring.
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
