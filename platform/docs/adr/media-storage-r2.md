# ADR: Prepared Public Media in Cloudflare R2

- Status: Accepted
- Date: 2026-08-01

## Decision

Store only normalized user-facing variants in the Standard-class `oldsparky`
R2 bucket. Use immutable object keys containing owner and asset UUID. Store
keys, dimensions, MIME type, byte size and SHA-256 in PostgreSQL. Serve browser
reads directly through `https://cdn.old-sparky.com`; API/page rendering must
not call S3.

Sources are staged privately and deleted after processing. Partial uploads are
removed before a job fails. Reconciliation recovers pending jobs and schedules
delayed cleanup for replaced assets.

## Consequences

- CDN hits avoid FastAPI/Next bandwidth and R2 Class B reads.
- Replacement is a new URL; no query-string cache busting is required.
- DB backup preserves the media mapping but not object bytes, so bucket
  inventory/reconciliation and an explicit media recovery policy remain needed.
- Privacy deletion may require purge-by-URL because immutable browser/edge
  responses can outlive R2 deletion.

The Standard free tier is account-wide: 10 GB-month, one million Class A and
ten million Class B operations per month as of the review date. Warn at 80%
and retain headroom; do not infer free operation from architecture alone.

References: [R2 pricing](https://developers.cloudflare.com/r2/pricing/),
[R2 cache integration](https://developers.cloudflare.com/cache/interaction-cloudflare-products/r2/).
