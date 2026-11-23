---
name: deadlock-backend-production
description: Use when implementing OldSparky FastAPI, PostgreSQL, Redis, Celery, Alembic, domain models, API schemas, or production backend behavior.
---

# Deadlock Backend Production

This repository-owned skill is the canonical OldSparky backend overlay for the
installed `/root/.codex/skills/deadlock-backend-production/SKILL.md` copy. Edit
this file first, then copy it to that installed path to keep the active local
skill synchronized. Do not make the installed copy the only source of a rule.

## Workflow

1. Use `platform/docs/CURRENT.md`, `platform/docs/README.md` and
   `platform/docs/test-suite-governance.md` to select the current owner contract
   and verification gate.
2. Keep FastAPI routes thin; put contracts in
   `platform/apps/platform_api/app/api/schemas.py`, business rules in
   `platform/python_packages/platform_domain/`, and DB/session/cache/security
   adapters in `platform/python_packages/platform_infra/`.
3. Put persistence changes in Alembic migrations targeting `platformdb`, schema
   `platform`. Never touch the retired legacy bot or `sparkydb` from platform
   work.
4. Put focused regression coverage in `platform/tests/`; use the canonical
   selectors in `platform/tools/platform_verify.py` rather than substituting
   raw test discovery or direct migration commands for owner gates.

## Data and runtime boundaries

- PostgreSQL is authoritative for users, profiles, tournaments, registrations,
  teams, matches, audit events and durable workflow state.
- Redis may support cache, rate limits, locks, Celery transport and bounded
  temporary state. It is not the final correctness authority for durable
  cardinality, authorization or workflow decisions.
- Celery handles scheduled work and notifications; routes and browser code do
  not replace server-side eligibility, lifecycle or persistence checks.
- Public media uses prepared descriptors through the CDN; API/page rendering
  does not proxy object bytes.
- Terminal tournament states freeze organizer match administration except for
  explicitly authorized correction flows.

## Authorization contract

Do not assume private tournament data is visible only to organizers, active
participants and admins. The current OldSparky contract also permits a valid,
nonrevoked, nonexpired invite code to read exactly the private summary,
workspace, participants, matches and bracket GET routes. Anonymous and
retained inactive viewers may use that closed allowlist. The code grants no
profile, invite-management or workflow reads and no writes; new child routes
need explicit allowlist opt-in and regression coverage. Apply the full contract
in `platform/docs/product-reference.md#tournament-contract` and
`platform/docs/adr/private-tournament-bearer-read-boundary.md`.

Contact fields require an explicit privacy decision before public exposure.
Authorization and workflow state remain server-side, even when the web client
renders capability DTOs or Cloudflare Access adds an edge check.

## Installed skill synchronization

When this tracked skill changes, synchronize the local installed copy with:

```bash
cd /root/old_sparky
cp .agents/skills/deadlock-backend-production/SKILL.md \
  /root/.codex/skills/deadlock-backend-production/SKILL.md
```
