# AS-12 origin perimeter closure evidence — 2026-09-05

- Status: Resolved
- Production source SHA: `97db79b681dd90cc8e89dd91f549610c943c16b8`
- Production deploy: [GitHub Actions run 33956247679](https://github.com/StrayForest/old_sparky/actions/runs/33956247679)
- Security/build gate: [GitHub Actions run 33955679070](https://github.com/StrayForest/old_sparky/actions/runs/33955679070)
- Read-only proof: [GitHub Actions run 33956430416](https://github.com/StrayForest/old_sparky/actions/runs/33956430416)

## Evidence

The SHA-locked production proof checked the active release under the release
lock, confirmed the platform services and Nginx configuration, and passed:

- listener inventory: PASS; Nginx owns the public web listeners, SSH remains
  the approved administration listener, and the only additional expected
  non-web socket is the `systemd-network` DHCP `UDP/68` socket under UFW's
  default-deny incoming policy;
- forwarded-header trust: PASS with `FORWARDED_ALLOW_IPS=127.0.0.1` in the
  rendered API environment and active API process;
- Cloudflare/Nginx/UFW parity: PASS with 22 current ranges in each source;
- direct-origin negative test: PASS from a GitHub-hosted runner outside the
  origin network; IPv4 and IPv6 probes to ports 80 and 443 all returned
  `BLOCKED_NO_HTTP_RESPONSE`.

No origin addresses, credentials or raw environment data are retained in the
repository. The proof workflow publishes only this summary and remains
available for a fresh run after any listener, perimeter or trust change.

This closure supersedes AS-12's open status in the active tracker. The
pre-release audit report and worklog retain their historical point-in-time
status from 2026-09-05 and are intentionally unchanged.
