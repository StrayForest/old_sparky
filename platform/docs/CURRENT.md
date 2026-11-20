# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-20

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production runs application source
  `8c575dbc0a510be5cf4cf6bbd4abc312c9a7db89` in release
  `gha-44700419645-1-8c575dbc0a51`; `previous` is source
  `720a6ea1a90090ef9aacc76c93b2ef8d8cc40a24` in release
  `gha-43743890016-1-720a6ea1a900`. Host-tools pin
  `aa4f9e075156bd9aadb699eb3a13253e80daf79a` is installed
  as manifest `production-host-tools-v4` (SHA-256
  `8363ca5c5b90de003e0e1e3b39179852621fd1a8383c167bc8107b3efa3b73c7`). The
  deployed source updates Next.js to patched version 16.4.6. Active runtime
  manifest is v3 with Node 26.3.1; runtime provider v2 is bound to source 8c.
  Alembic current and head are both `20261115_0056` (admin tournament test
  fixtures). The shared virtual-environment origin resolves to current 8c.
- Source 8c passed exact-SHA Security run `44694724609/1` and backup
  `44696295210/1`. AUTO `44696145318/2` succeeded, followed by production
  deploy `44700419645/1`. The lock-held release preflight, runtime verification
  and loopback/public-origin smokes passed.
- Public QA `44701659435/1` passed 47 cases, skipped 10 allowlisted cases, and
  had zero failures. UserQA `44702730670/1` succeeded. Parent run
  `44701610827/1` succeeded with exact UserQA cleanup. The retention preview
  and apply protected current and previous releases, deleted one
  eligible old release, and removed no live-QA cache. The post-apply preview
  has no remaining eligible deletions.
  Canonical post-QA maintenance run `44705121244/1` succeeded in apply mode,
  creating a new restore-verified backup of 135,102,570 bytes and retaining one
  backup; disk had 6,804,692,992 bytes free (about 83% used). The earlier
  deploy backup run remains the exact CI-bound release backup. Final readiness
  found source 8c current and 720 previous, all app services active, live,
  ready and web root endpoints returning 200, no transaction, zero LiveQA
  gates and zero retained UserQA state directories. Maintenance service is
  inactive with successful result and its timer is active/enabled. The fresh
  post-retention preview protects current, previous and the current venv
  origin, with zero remaining deletion candidates.
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

- Required production deployment, public QA, UserQA and exact UserQA cleanup
  passed for source 8c. Post-QA readiness, exact absence and maintenance
  retention closeout passed. Human Chrome and real Cloudflare challenge/scoring
  remain optional diagnostics; the CI Siteverify fixture does not establish
  production Cloudflare scoring.
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
