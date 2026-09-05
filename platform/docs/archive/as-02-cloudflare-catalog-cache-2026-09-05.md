# AUD-02 — Cloudflare catalog cache evidence

- Date: 2026-09-05
- Surface: `https://old-sparky.com/api/v1/tournaments`
- Scope: public catalog cache behavior and private-route boundary

## Live evidence

The exact public catalog path returned the origin cache contract:

```text
Cache-Control: public, max-age=5, s-maxage=15, stale-while-revalidate=30
```

Two immediate anonymous requests with the same query produced:

```text
first:  HTTP 200, CF-Cache-Status: MISS
second: HTTP 200, Age: 0, CF-Cache-Status: HIT
```

The production session cookie name is `__Host-old_sparky_session` from the
checked-in production env contract. A probe using that cookie returned
`CF-Cache-Status: DYNAMIC`; a probe with `Authorization: Bearer probe` also
returned `DYNAMIC`. The response body digest matched the anonymous probe for
all three requests (`4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945`).

The private catalog returned:

```text
GET /api/v1/tournaments/mine?limit=9
HTTP 401
Cache-Control: no-store
CF-Cache-Status: DYNAMIC
```

## Conclusion

The previous contradiction was caused by treating a non-production probe
cookie as the session-cookie test. The live behavior now proves the intended
cache boundary: anonymous public catalog responses may be cached, while the
actual session-cookie/authorization and `/mine` paths bypass the edge cache.

This closes the runtime cache-behavior part of AUD-02. Direct Cloudflare
dashboard evidence for the rule expression and the other operator-owned
controls remains tracked in the active checklist.
