# Platform deploy assets

This directory contains production Nginx/systemd/journald definitions for the
isolated `platformdb.platform` web stack. It must not recreate or depend on the
retired legacy Telegram bot, `sparkydb` or PgBouncer.

Authoritative procedures:

- [production architecture](../docs/production-architecture.md);
- [deployment and rollback](../docs/deployment-runbook.md);
- [operations and retention](../docs/operations-runbook.md);
- [security policy](../docs/security-runbook.md).

Runtime layout:

```text
/opt/oldsparky/platform/releases/<release-slug>
/opt/oldsparky/platform/current
/opt/oldsparky/platform/previous
/opt/oldsparky/platform/shared
```

Build/install tools create immutable artifacts; the release deploy wrapper
retains a durable transaction through migration, restart/readiness, Nginx and
smoke before committing `current`/`previous`. Nginx installation validates and
updates the vhost plus shared security-header snippet as one rollback unit.
The systemd installer installs all API/web/worker, health, Cloudflare,
maintenance and off-site-backup units; only the reviewed recurring timers are
enabled automatically. Do not edit live files without the documented state
machine and smoke path.
