# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-28

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production runs application source
  `259118797db9b000fab061888cff8c3433420457` in automatic release
  `gha-46102005767-1-259118797db9`. Exact-SHA Security run
  `46100564969/1`, source-bound restore-verified backup `46101741188/1`,
  deployment `46102005767/1` and post-deploy activation passed. The separately
  installed shared host-tools generation is
  `b655b2969c70e8787805d98ebb61a8a3e6a7bd74`. The host-tools pin and app
  release are separate identities. See the
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
- Automatic Public QA for source
  `259118797db9b000fab061888cff8c3433420457`, deployment parent
  `46102642752/1` and exact Public child `46102660292/1` passed: 56 passed,
  10 skipped, 66 total, and zero failed, flaky, timed-out or interrupted
  attempts. UserQA child `46103242805/1` passed 1/1; sequencer parent
  `46102642752/1` succeeded. Public and UserQA cleanup completed, and the
  fixed LiveQA credential files are absent. The objective screenshot now
  captures the viewport while retaining the route, HTTP/MIME, PNG-signature
  and overflow assertions. Earlier capture failures remain without a proven
  internal browser cause; this passing run does not establish one. See the
  [CSP/live-QA runbook](csp-live-qa-runbook.md).
- The 2026-11-28 02:37 UTC maintenance invocation failed with a closed lock
  marker. Its historical lock owner and exact cause remain unknown. The
  certificate-free capture `46130553213/1`, bound to Security
  `46128787815/1`, recovery producer `46129738168/1` and publisher
  `46129774355/1` for source
  `58bcf332d6b0329d618384c97eadce067f679922`, recorded the service as failed
  with a bound invocation, active+enabled timer, no queued jobs and no cgroup
  members. The exact current-state acknowledgement `46130730347/1` succeeded;
  it reset only systemd failure metadata and did not establish a historical
  cause or maintenance success. Read-only storage diagnostics `46130890080/1`
  then reported an unlocked host, active API/worker/web services, an
  active+enabled source-matched timer with no drop-ins, and a restore/Alembic/
  checksum-verified backup. Disk use was 68% with 12.342 GB free, above the
  shared minimum. The installed-unit-bound retention preview returned
  `status=ok`, `mode=dry-run`; its summary showed 2 protected and 6 retained
  production releases with 7 proposed deletions. No production deletion or
  maintenance-service start was performed. The app release remains
  `259118797db9b000fab061888cff8c3433420457`; its existing Public/UserQA
  passes remain the current application QA evidence. This storage report did
  not include a fresh `platform_health_monitor.py` strict-readiness record; an
  external health URL check from the runner returned 403 and is inconclusive
  about origin readiness. See the certificate-free capture and acknowledgement
  procedure in the [deployment runbook](deployment-runbook.md#immutable-recovery-bootstrap).
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
