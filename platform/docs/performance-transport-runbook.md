# Authenticated HTML transport runbook

- Status: Active operator procedure
- Owner: Platform maintainers

Use this procedure to separate authenticated HTML latency across direct
Next.js, local Nginx and public Cloudflare. It is diagnostic evidence only; the
canonical external load profile remains the authority for production latency,
correctness, origin safety and cleanup.

## Preconditions

Record the deployed source/release, `nginx -v`, `nginx -t`, the diagnostic
profile state and the exact page path. Use a disposable authenticated test
account and a private one-line Cookie-header file with mode `0600`. Never put a
cookie in shell history, command output, an artifact or a report.

The source Nginx policy disables response buffering only in the dynamic HTML
proxy location and emits `X-Accel-Buffering: no`. API and static locations keep
their existing buffering/cache contracts. The JSON access log records upstream
and client encoding, transfer and buffering headers.

## Same-request hop probe

Run from an operator host or the origin. The probe reads the cookie, measures
time to response headers and total body time, records selected transport
headers, and never prints the cookie or response body:

```bash
cd /opt/oldsparky/platform/current
python3 tools/platform_ttfb_probe.py \
  --cookie-file /run/oldsparky/qa-cookie \
  --request-id ttfb-hop-<run-id> \
  --hop next=http://127.0.0.1:3000/tournaments/<slug> \
  --hop nginx=https://127.0.0.1/tournaments/<slug> \
  --hop cloudflare=https://old-sparky.com/tournaments/<slug>
```

Compare `ttfb_ms`, `content-encoding`, `transfer-encoding`,
`x-accel-buffering`, `cf-cache-status` and `cf-ray`. The public hop is the
visitor-facing evidence. Repeat the probe enough times to see variance; do not
add its single-request values to external-load p95s.

## Compression A/B

Next compression is a build-time switch, not a runtime env toggle. Keep the
default artifact compressed, then build a separately named candidate from the
same source with:

```bash
PLATFORM_WEB_NEXT_COMPRESSION=false \
  tools/platform_web_npm.sh --prefix apps/platform_web run build
```

Run the same control/candidate request shape and hop probe. Retain compression
unless an unchanged-window comparison shows a reproducible first-byte gain
without response-integrity or security regressions. Do not disable Nginx or
Cloudflare compression globally as a first response.

## Cloudflare evidence

Run the read-only Cloudflare audit workflow and inspect
`response-buffering-zone-setting` plus `response-body-buffering-rules`. The
first is the legacy Enterprise zone setting; the second reports per-request
Configuration Rules. Do not change either setting from this repository. A
`none` response-body rule is acceptable only after confirming that the scoped
authenticated HTML path does not require response-body inspection by WAF or
Bot Management.
