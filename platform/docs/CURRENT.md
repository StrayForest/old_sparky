# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-11-13

Read this page for live pins, pending release work, open QA gates and hard
operational limits. Product contracts, procedures and detailed run evidence
belong in the linked owner documents below.

## Live and pending state

- Production app and installed LiveQA payload source is
  `126d4114542369af9d999e219563fa1d53b09568`; the previous app is the initial
  rollback source `cb1a75114aac0f4fb0ee6cc407011f90c6e76f31`. Active
  host-tools pin C3 is `625bbaa11023ffaf059dfa03a34a4850b02d4f93`. Exact-SHA
  Security `43401840198`, source-bound restore-verified backup `43404066694`,
  AUTO `43403887168`, and automatic production child `43404454746` completed
  successfully. The child activated the app once; post-deploy readiness and
  health passed with an operations receipt/checkpoint. Deployment preflight
  verified Alembic `current` against `heads` at `20261112_0055`; no migration
  downgrade occurred. The app change added no schema, dependency, or
  host-tools-pin change.
- Public browser launch remains uncredited. Canonical launches
  `43405943415` and `43408704800` both timed out with exit 124 and no case
  counts. Both orphan gates were recovered with the trusted exact-path
  recovery command; those recoveries preserved the provisioned bundle and its
  13 generated roster entries. A later supported direct public run used the
  same deployed source, but its terminal result was not retained. Its systemd
  unit lasted about 300 seconds, and the gate and runtime mount aliases were
  cleaned. With no durable count receipt, do not claim that run passed or
  failed. Automated
  user QA has not started; human Chrome authentication and Turnstile QA remain
  for the user.
- The 13 generated roster entries remain pending canonical cleanup. The
  recovery bundle's recorded `created_at` is `2026-11-13T10:02:41Z`; its
  four-hour eligibility time is after `2026-11-13T14:02:41Z`. Fresh-bundle
  cleanup currently refuses, so no inventory was fabricated. The main manual
  account was not pre-created, and the control identity is excluded. A
  separate public-QA timeout change is not deployed and does not change the
  active C3 pin.
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

## Open QA and cleanup gates

- Resolve the public browser launch timeout and obtain a source-bound case-count
  receipt before crediting public QA. The two canonical timeout results and
  the incomplete direct run are recorded above; use the [CSP and live-QA
  runbook](csp-live-qa-runbook.md) for the trusted launch and recovery paths.
  Keep the pending roster cleanup on the canonical recovery-bundle path and
  wait until its recorded eligibility time; do not create substitute inventory.
- Complete automated user QA and the later human Chrome authentication and
  Turnstile checks. The separate timeout change is not deployed and does not
  change QA evidence for the active source.
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
