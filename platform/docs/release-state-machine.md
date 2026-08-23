# Release state machine and recovery contract

- Status: Active release design
- Owner: Production operator and platform maintainers
- Last reviewed: 2026-08-23

This document owns the end-to-end release transaction. The normal production
path is `tools/platform_release_deploy.sh`; the low-level installer is a
filesystem primitive and must not be called directly by CI or an operator.

## State flow

```text
candidate artifact
    -> staged
    -> migration-pending
    -> migration-applied
    -> activation-pending
    -> services-restarted
    -> nginx-applied
    -> smoke-passed
    -> activation-committed
```

The installer validates the artifact and wheelhouse, creates the candidate and
shared-runtime transition, and leaves `shared/.release-operation.json` at
`staged`. The deploy wrapper then owns the migration decision, pointer switch,
service preparation/restart, readiness, Nginx apply and origin/public smoke.
The state file is removed only after the final commit phase.

## Failure behavior

- A failure before `staged` is recovered by the installer while the original
  pointers and shared venv are still authoritative.
- A failure in `staged` can be recovered with
  `tools/platform_release_rollback.sh --recover-pending --app-dir ...`.
- `migration-pending` and `migration-failed` are intentionally not auto-
  recoverable. The Alembic outcome may be uncertain, so the state is retained
  until an operator checks the database and either resumes with
  `platform_release_deploy.sh --resume` or performs an explicit compatibility
  review before code/runtime rollback. The explicit abort path is:

  ```bash
  tools/platform_release_deploy.sh \
    --abort-retained \
    --confirm-migration-not-reversed \
    --app-dir /opt/oldsparky/platform
  ```

  This restores the recorded pointers and venv, restarts old services when
  activation had started, reapplies the old Nginx config when needed, and never
  downgrades Alembic. The confirmation is an operator statement that the
  database migration was not reversed and compatibility has been reviewed.
- `migration-applied` and every later phase retain the candidate and state on
  failure. Do not delete the state file, downgrade Alembic, or run a second
  unrelated install. Resume first; rollback remains a code/runtime operation
  and never reverses database migrations automatically.

The retained state is the recovery receipt: it records the original pointers,
candidate identity and shared-venv identities. A restart/readiness or smoke
failure therefore cannot silently leave an untracked pointer/venv combination.

## Operator command

```bash
cd /opt/oldsparky/platform/current
tools/platform_release_deploy.sh \
  --artifact /path/to/<release-slug>.tar.gz \
  --app-dir /opt/oldsparky/platform \
  --expected-csp-mode enforce
```

If the command reports a retained transaction, inspect the phase and database
revision, then resume only the same transaction:

```bash
tools/platform_release_deploy.sh --resume --app-dir /opt/oldsparky/platform
```

The deploy gate also requires a read-only Cloudflare/Nginx/UFW range-parity
proof. This repository does not contain live VPS evidence; a release is not
production-approved until that proof and the direct-origin negative test are
recorded by the operator.

## Explicit non-goals

- Alembic downgrade is not part of deploy recovery.
- A production browser/translation warm-up is not read-only QA. The patch
  translation workflow is now named and reported as a controlled warm-up with
  an explicit cache-miss/OpenAI call budget.
- Building on the production host from a source archive is not immutable
  provenance. Hash-locked dependencies and a CI-built, signed artifact remain
  required before fully automatic production deployment can be reconsidered.
