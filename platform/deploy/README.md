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

Build/install tools create immutable artifacts and move `current`/`previous`
atomically. Nginx installation validates and updates the vhost plus shared
security-header snippet as one rollback unit. Do not edit live files without
the documented installer and smoke path.
