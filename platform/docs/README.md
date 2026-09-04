# Platform documentation

- Status: Active
- Owner: Platform maintainers
- Last reviewed: 2026-09-04

Start with [`CURRENT.md`](CURRENT.md). It is the compact source of current production state and next engineering priority. Open deeper documents only when the task requires them.

## Choose the document by task

| Need | Document |
| --- | --- |
| Current production state / next priority | [Current state](CURRENT.md) |
| Prioritized backlog | [Roadmap](platform-roadmap.md) |
| Components, data flow and trust boundaries | [Production architecture](production-architecture.md) |
| Local setup, verification, commit and push workflow | [Development guide](development-guide.md) |
| Maintaining docs, AGENTS and skills | [Documentation governance](documentation-governance.md) |
| Product and workflow contracts | [Product reference](product-reference.md) |
| Deadlock patch translation / Valve glossary contract | [Patch translation](patch-translation.md) |
| Standing translation regression and warm-up procedure | [Patch translation](patch-translation.md#qa) |
| UI system and responsive rules | [Visual theme](platform-visual-theme.md) |
| Admin console structure and metric definitions | [Admin console IA](admin-console-information-architecture.md) |
| Normal release or rollback | [Deployment runbook](deployment-runbook.md) |
| Release transaction and recovery | [Release state machine](release-state-machine.md) |
| Test-suite ownership and CI/live runners | [Test-suite governance](test-suite-governance.md) |
| CSP rollout / production browser and live-user QA | [CSP and live QA runbook](csp-live-qa-runbook.md) |
| Backup or restore | [Backup and restore](backup-restore-runbook.md) |
| Services, storage, media and performance | [Operations runbook](operations-runbook.md) |
| Security operations / CSP policy | [Security runbook](security-runbook.md) |
| Cloudflare dashboard work | [Cloudflare checklist](cloudflare-production-checklist.md) |
| Incident handling | [Incident response](incident-response.md) |
| Security findings and evidence | [Application security audit](application-security-audit.md) |
| Historical implementation context | [`archive/`](archive/) |
| Accepted architectural decisions | [`adr/`](adr/) |
| Ready Check / bracket boundary | [Timing and bracket ADR](adr/ready-check-and-bracket-boundary.md) |
| Public standalone Draft / Cloudflare edge boundary | [Public Draft edge ADR](adr/public-draft-edge-boundary.md) |

## Token-efficient reading contract

- Do not read the entire docs directory for routine work.
- Read `CURRENT.md`, then at most the one or two owner documents relevant to the requested change.
- Use headings/search and bounded ranges for long runbooks/audits instead of loading them end to end.
- `csp-live-qa-runbook.md`, security audits and historical plans are cold/on-demand context; never load them for ordinary feature or release work.
- Treat detailed security/release evidence as on-demand context, not default context.
- Do not duplicate current state across runbooks; link back to its owner.

## Documentation contract

- Use English for engineering documentation and Russian for product UI copy.
- Describe current behavior in present tense. Do not append release diaries.
- Keep one owner for each fact. Link to that owner instead of duplicating it.
- Put procedures in runbooks, stable facts in reference documents, decisions in ADRs, current state in `CURRENT.md`, and prioritized future work in the roadmap.
- Keep the instruction hierarchy and skill maintenance rules in [documentation governance](documentation-governance.md).
- Completed implementation plans belong in `archive/`; they are not active task context.
- Keep commands copyable and safe by default. Mark mutations and operator gates.
- Refer to configuration names, never secret values or live personal data.
- Store detailed test, load and deploy output outside Git; record only the current conclusion and report location.
- When deleting or renaming a document, update repository links in the same change and run the documentation and skill consistency check.

```bash
cd /root/old_sparky/platform
.venv_platform/bin/python tools/platform_verify.py docs
.venv_platform/bin/python tools/platform_verify.py verification-contract
```
