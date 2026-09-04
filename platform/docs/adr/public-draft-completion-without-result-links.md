# Public Draft completion without result links

- Status: Accepted
- Date: 2026-09-05
- Owner: Platform maintainers
- Supersedes in part: The completed-result URL clauses in [Public Draft edge boundary](public-draft-edge-boundary.md).

## Context

Public Draft rooms are intentionally ephemeral and do not provide durable match
history. Encoding the completed state into a URL added a second result screen
and long share links that are not required by the product flow.

## Decision

A completed online or Solo draft remains on its current screen and shows the
final team compositions and pick/ban sequence there. Draft does not expose a
`/draft/result` shell, encode completed state into URL fragments or offer
result-link copy controls.

The room URL continues to be usable only while its ephemeral Durable Object
state exists. Starting another draft returns the browser to `/draft`.

## Consequences

- Completion has one screen and no additional result navigation step.
- Finished drafts cannot be shared as durable stateless links.
- Draft still stores no history in PostgreSQL, Redis or the platform VPS.
