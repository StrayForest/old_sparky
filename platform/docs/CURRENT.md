# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-14

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production runs application source
  `d52eb46080fed7a4dbc6c86c4b875cbe11cfa3a5` in release
  `gha-43572099804-1-d52eb46080fe`; `previous` is original rollback source
  `cb1a75114aac0f4fb0ee6cc407011f90c6e76f31` in release
  `gha-43367073524-1-cb1a75114aac`. Preserve `cb1a...` as the rollback target.
  Host-tools C4 `64cae2a4c8235df1ba5c044446cd2f497f4abdca` is selected and C3
  `625bbaa11023ffaf059dfa03a34a4850b02d4f93` retained. The shared venv origin
  is d52; Alembic current equals head `20261112_0055`.
- Source `d52eb46080fed7a4dbc6c86c4b875cbe11cfa3a5` passed exact-SHA
  Security run `43569313507/1` and backup `43571845676/1`. AUTO
  `43570533763/1` succeeded. Production deploy `43572099804/1` succeeded;
  actual-deploy artifact `14105022133` binds mode `deploy`, release
  `gha-43572099804-1-d52eb46080fe` and source d52. Authorization artifact
  `14104787647` binds sequencer `43572731917/1` (workflow 417784007) to that
  deploy attempt and source. Public child `43572752214/1` created and uploaded
  its receipt, passed child authentication, and completed public QA. Artifact
  `14105615416` reports 57 attempts: 47 passed and 10 allowlisted expected
  skips, with zero failures, flaky, interrupted or timed-out cases. The
  coverage contract requires all 57 expected cases and validates each skip
  annotation. UserQA child `43573228506/1` (workflow 340531613) bound to public
  `43572752214/1`, deploy `43572099804/1` and source d52; artifact `14105311501`
  reports complete coverage, one journey/attempt passed, zero skipped or
  failed, child exit 0. Exact cleanup path and absence assertions passed.
  Sequencer `43572731917/1`, public QA and UserQA all completed successfully.
  Final strict preflight, origin
  smokes and public smokes (33 checks, zero failures each) passed. Final
  post-cleanup readiness is ready. API and web returned 200, all three app
  services were active, both locks were free, there were no pending systemd
  jobs, operation marker or diagnostic plans, and final consistency passed.
  No GitHub workflow runs remained active. The maintenance service is
  inactive/dead and its timer active/enabled. `dev` remains frozen.
- The a289 sequencer attempt `43567504351/1` dispatched public child
  `43567523083/1`, but failed while writing the exact child receipt; public
  authentication rejected the child before browser QA, and no public or
  UserQA result is credited. The six-value writer correction is in the d52
  release. Human Chrome and live Cloudflare challenge/scoring remain
  optional diagnostics; the CI Siteverify fixture does not establish
  production Cloudflare scoring.
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

- Required production deployment, automatic public/UserQA, exact cleanup and
  final idle-readiness gates are closed for d52. Human Chrome and real
  Cloudflare challenge/scoring remain optional diagnostics; the CI Siteverify
  fixture does not establish production Cloudflare scoring.
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
