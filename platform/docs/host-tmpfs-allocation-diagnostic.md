# Host tmpfs allocation diagnosis

- Status: Active operator procedure
- Owner: Platform performance
- Last reviewed: 2026-11-23

`tools/platform_tmpfs_allocation_audit.py` is a one-shot, read-only operator
diagnostic for visible inode allocation under the fixed `/dev/shm` root. It
walks metadata twice through no-follow directory descriptors, checks inode and
mount identity, and deduplicates by `(device, inode)`. It writes a bounded
JSONL metadata inventory and one bounded SQLite index inside a newly created
private output directory. SQLite uses an in-memory temp store, a 1 MiB cache,
and a 128 MiB index cap; the scan is bounded to 262,144 entries, depth 32,
64 MiB of inventory output, 16 MiB of encoded names, 90 seconds wall time and
85 CPU seconds. Caps, read failures, identity drift, or incomplete link
metadata produce `UNKNOWN`.

The tool samples only `MemAvailable` and `Shmem` from bounded
`/proc/meminfo` reads before and after the scan. Those values provide time
context; they are not added together, mapped to named files, or used for an
admission decision. `st_blocks * 512` is filesystem allocation for visible
inodes, not resident RAM or a complete tmpfs page count. Deleted-open inodes,
external hardlinks, leases, service references, and process ownership require
separate evidence. Names and metadata can be sensitive, so keep the output
directory private and review it locally. The tool has no delete, cleanup,
backup, service, or deployment behavior and is outside the host-tools bundle
and release payload.

Run only in an explicitly authorized diagnostic window, with an absolute new
output directory whose existing parent chain is root-owned or caller-owned
and not group/other-writable:

Run the command from the `platform/` directory with its canonical Python
interpreter:

```bash
.venv_platform/bin/python tools/platform_tmpfs_allocation_audit.py \
  --output-dir /secure/private-parent/tmpfs-audit-<unique-id>
```

Treat the operation as complete only when the observed process exit is zero,
`terminal.json` is private and readback-valid with
`status=COMPLETE_METADATA`, and no `failure.json` exists. A positive summary
or inventory on its own is not a complete execution receipt. Preserve all
artifacts for review; a name or allocation total never authorizes deletion.
