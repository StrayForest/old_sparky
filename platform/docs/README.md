# Platform documentation

- Status: Active
- Owner: Platform maintainers
- Last reviewed: 2026-11-23

Start with [`CURRENT.md`](CURRENT.md) for verified production state and the
current engineering priority. Then follow the route below to the owner document
for the task. Open deeper references only when the task needs them.

## Navigation tree

### Start and orient

| Task | Owner document |
| --- | --- |
| Current production state and next priority | [Current state](CURRENT.md) |
| Prioritized backlog | [Roadmap](platform-roadmap.md) |
| Local setup, verification, commit and push | [Development guide](development-guide.md) |
| Focused agent reads and handoffs | [Agent work protocol](agent-token-efficiency.md) |

### Understand the platform

| Task | Owner document |
| --- | --- |
| Components, data flow and trust boundaries | [Production architecture](production-architecture.md) |
| Product, role and workflow contracts | [Product reference](product-reference.md) |
| Admin console structure and metric definitions | [Admin console IA](admin-console-information-architecture.md) |
| UI system and responsive rules | [Visual theme](platform-visual-theme.md) |
| Deadlock patch translation / Valve glossary | [Patch translation](patch-translation.md) |
| Standing translation regression and warm-up procedure | [Patch translation QA](patch-translation.md#qa) |

### Change and verify

| Task | Owner document |
| --- | --- |
| Docs, AGENTS, ADR and skill maintenance | [Documentation governance](documentation-governance.md) |
| Test placement, gate ownership and CI/live runners | [Test-suite governance](test-suite-governance.md) |
| Automated authentication lifecycle coverage | [Auth lifecycle E2E](auth-lifecycle-e2e.md) |

### Release, recovery and data operations

| Task | Owner document |
| --- | --- |
| Normal production release and exceptional recovery paths | [Deployment runbook](deployment-runbook.md) |
| Exact LiveQA bytecode-only payload repair | [LiveQA payload integrity repair](liveqa-payload-repair-runbook.md) |
| Release transaction and recovery | [Release state machine](release-state-machine.md) |
| Production application rollback | [Production rollback runbook](production-rollback-runbook.md) |
| Production web/API/worker service recovery | [Service recovery](service-recovery-runbook.md) |
| Read-only production readiness and private verifier staging | [Read-only production readiness](read-only-production-readiness.md) |
| Backup or restore | [Backup and restore](backup-restore-runbook.md) |
| Keep-one verified backup retention | [Backup retention](backup-retention-protection.md) |
| Services, storage, media and performance | [Operations runbook](operations-runbook.md) |
| Host tmpfs allocation diagnosis | [Host tmpfs allocation diagnostic](host-tmpfs-allocation-diagnostic.md) |
| Host disk free-space thresholds | [Disk policy ADR](adr/disk-free-space-thresholds.md) and [operations](operations-runbook.md) |
| Shared-host task artifacts and temporary worktrees | [Operator storage-retention runbook](operator-storage-retention-runbook.md) |
| Exceptional fallback runtime-cache compaction | [Fallback cache compaction](fallback-runtime-cache-compaction-runbook.md) |
| Retained-load cleanup and hermetic web verification | [Retained-load and web verification](retained-load-and-web-verification.md) |
| Production SSR capacity diagnostic | [SSR capacity diagnostic](ssr-capacity-diagnostic-runbook.md) |
| External-load runner safety and evidence contract | [External-load execution safety](performance-external-load-safety.md) |
| Authenticated HTML transport / TTFB hops | [Performance transport](performance-transport-runbook.md) |
| Load profiles and performance evidence | [`platform/performance/README.md`](../performance/README.md) |

### Validate and secure

| Task | Owner document |
| --- | --- |
| Security operations / CSP policy | [Security runbook](security-runbook.md) |
| CSP rollout / production browser and live-user QA | [CSP and live QA](csp-live-qa-runbook.md) |
| Incident handling | [Incident response](incident-response.md) |
| Security findings and evidence | [Application security audit](application-security-audit.md) |
| Public QA recovery after pre-roster provisioning refusal | [Public QA recovery](csp-live-qa-failed-provision-recovery.md) |
| Remove exact LiveQA credential files after successful UserQA | [Post-UserQA cleanup](liveqa-post-userqa-cleanup.md) and [CSP/live QA](csp-live-qa-runbook.md) |
| Clean up an expired exact LiveQA bundle roster | [Stale LiveQA bundle recovery](liveqa-stale-bundle-recovery.md) |
| Missing immutable LiveQA engine provider | [Provider recovery](liveqa-provider-recovery.md) |
| External fixture credential transport | [Credential transport](fixture-credential-transport.md) |
| Cloudflare dashboard work | [Cloudflare checklist](cloudflare-production-checklist.md) |
| Locally installed Codex archived-session retention | [Codex archive-retention procedure](codex-archive-retention.md) |

### Accepted decisions and historical evidence

- Space-bounded release bootstrap: [subset extraction ADR](adr/release-bootstrap-subset-extraction.md).
- Candidate forward-migration safety: [candidate forward migration guard](adr/candidate-forward-migration-guard.md).
- Immutable retained-release recovery bootstrap: [recovery bootstrap ADR](adr/recovery-bootstrap-retained-abort.md), [first-install systemd ADR](adr/first-install-systemd-activation.md), and [deployment runbook](deployment-runbook.md#immutable-recovery-bootstrap).
- Prune one consumed quarantined candidate: [signed quarantine-pruning ADR](adr/quarantined-candidate-pruning.md) and [deployment runbook](deployment-runbook.md#immutable-recovery-bootstrap).
- Failed maintenance evidence and guarded reset: [encrypted evidence transport ADR](adr/maintenance-failure-evidence-encryption.md), [failure capture and reset ADR](adr/maintenance-failure-capture-and-reset.md), and [deployment runbook](deployment-runbook.md#immutable-recovery-bootstrap).
- Report-only failed-deployment recovery: [recovery bootstrap ADR](adr/recovery-bootstrap-retained-abort.md#operator-recovery-deployment-for-a-report-only-invalid-marker) and [manual workflow fallback](deployment-runbook.md#manual-workflow-fallback).
- Secret-bearing workflow artifact boundary: [secret-job artifact boundary ADR](adr/secret-job-artifact-boundary.md).
- Release-independent host-tools generations and trusted PR handoff: [host-tools provisioning ADR](adr/production-host-tools-provisioning.md).
- Trusted CI-handoff source/merge identity: [source-versus-tested merge identity](adr/production-host-tools-provisioning.md#closed-source-versus-tested-merge-identity).
- Private tournament bearer reads: [private bearer read boundary](adr/private-tournament-bearer-read-boundary.md).
- Ready Check / bracket boundary: [timing and bracket ADR](adr/ready-check-and-bracket-boundary.md).
- Public standalone Draft / Cloudflare edge boundary: [Draft edge ADR](adr/public-draft-edge-boundary.md) and [completion without result links](adr/public-draft-completion-without-result-links.md).
- Standalone Draft implementation and Cloudflare deployment: [`platform_draft` README](../apps/platform_draft/README.md) and its [path-specific agent guide](../apps/platform_draft/AGENTS.md).
- Historical implementation context: [`archive/`](archive/).
- 2026-11-13 simplification and performance audit: [archived audit](archive/simplification-performance-audit-2026-11-13.md).

## Reading and ownership rules

- Read `CURRENT.md`, then at most the one or two owner documents relevant to the task.
- Use headings and bounded ranges for long runbooks and audits. Security, release and historical evidence are on-demand context.
- Do not duplicate current state across runbooks. Link back to its owner.
- Keep engineering documentation in English and product UI copy in Russian. Describe current behavior in present tense; do not append release diaries.
- Keep commands copyable and safe by default; mark mutations and operator gates. Refer to configuration names, never secret values or live personal data.
- Store detailed test, load and deploy output outside Git; record only the current conclusion and report location.
- Follow [documentation governance](documentation-governance.md) for instruction hierarchy, architecture-to-document ownership and maintenance.
- When deleting or renaming a document, update repository links in the same change and run the documentation and skill consistency check. That check validates structure and links; semantic synchronization is reviewed against the owner map in documentation governance.

From `platform/`, run the canonical documentation checks:

```bash
tools/platform_run_quiet.sh "Platform docs gate" -- .venv_platform/bin/python tools/platform_verify.py docs
tools/platform_run_quiet.sh "Platform verification contract gate" -- .venv_platform/bin/python tools/platform_verify.py verification-contract
```
