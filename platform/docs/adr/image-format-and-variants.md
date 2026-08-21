# ADR: Image Format and Variants

- Status: Accepted
- Date: 2026-08-01

## Decision

Decode JPEG, PNG and WebP with pinned Pillow. Reject other containers,
animation, malformed/trailing polyglot data, decompression bombs and configured
byte/pixel/dimension excess. Apply EXIF orientation, convert to sRGB, strip
metadata, preserve meaningful alpha and encode deterministic WebP variants.

- avatar: 128, 256 and 512 square, center-cover;
- tournament banner: 560x140 and 1120x280;
- profile banner: the two measured CSS/DPR sizes owned by the profile layout.

Quality search is bounded. A source original is never public or retained after
a successful job. AVIF is deferred until a representative benchmark proves a
material byte saving without violating CPU/queue/R2 budgets.

## Rationale

WebP has broad browser support and keeps one predictable output pipeline.
Pillow 12.3.0 is pinned and must receive security updates; native decoders run
inside a concurrency-one worker with task time/memory bounds.

Reference: [Pillow security guidance](https://pillow.readthedocs.io/en/latest/reference/security.html).
