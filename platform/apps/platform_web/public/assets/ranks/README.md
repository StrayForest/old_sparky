# Rank icons

Deadlock rank icons use the same case-sensitive slug as the web UI. All eleven
files are current tier badges sourced from the Deadlock Assets API
(`assets-bucket.deadlock-api.com/assets-api-res/images/ranks/rank{tier}_lg.webp`)
and optimized to 256 pixels wide as WebP for the web UI:

- `Eternus.webp`
- `Ascendant.webp`
- `Phantom.webp`
- `Oracle.webp`
- `Emissary.webp`
- `Ritualist.webp`
- `Mystic.webp`
- `Sentinel.webp`
- `Acolyte.webp`
- `Seeker.webp`
- `Initiate.webp`

Generated rank URLs include the stable `DEADLOCK_RANK_ASSET_VERSION` query
version from `lib/deadlock.ts`. Rank assets are cached for one year as
immutable, so bump that constant whenever any icon in this directory changes
before releasing the updated asset. `Initiate.webp` remains the fallback icon.
