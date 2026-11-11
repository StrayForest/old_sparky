# Deadlock patch translation

- Status: Active / DB-first implementation deployed
- Owner: Platform maintainers
- Last reviewed: 2026-11-11

This document owns the contract for automatic Russian translation of Deadlock patch-note changes.

## Source implementation — 2026-11-11

Translation state is durable in the PostgreSQL `platform.patch_translations`
table. A record is identified by the patch ID, the hash of the extracted
English change segments, locale, translation version and model. Redis remains a
derivative result cache: a patch-detail read checks Redis first, then reads a
completed translation from PostgreSQL and warms Redis on a miss.

When Steam data is discovered, the application registers the exact source
version in PostgreSQL before publishing one Celery task for that version. The
unique identity and row lock prevent repeated home refreshes, startup refreshes
or patch-detail cache-miss refreshes from creating duplicate tasks. A user read
never starts OpenAI translation. If a task cannot find its registered database
record, it fails closed and the condition is logged for diagnosis. Failed
translations are not requeued on every refresh. One failed record may receive
one delayed automatic retry after 15 minutes when its error is a known network
or timeout failure and its attempt count is below the fixed retry limit. Other
failures remain durable and do not incur repeated model calls.

The translation version configured in this source tree is `ru-v16`, defined in
`apps/platform_api/app/services/patch_translation_config.py`. It includes the
durable DB-first state, the reviewed ambiguity handling and the immutable
numeric/tier validation described below. It gathers typed ENTITY/FACT
placeholder failures, numeric-fact mismatches and the reviewed effectiveness
comparison failure across the initial batches before using the single shared
follow-up request. That request contains only the affected stable segment IDs.
The `Gun Damage` entity/mechanic collision is source-sensitive: named ability
references such as Rem's `Gun Damage` stay protected as entity names, while
source-backed `Base gun damage` stat lines remain visible to the model and use
the checked-in `Weapon Damage` glossary value. A source-matched gun-damage stat
row that still contains the English mechanic label is rejected into the same
single follow-up correction path; named ability references remain protected.
The four-patch regression procedure
and its historical evidence live in
[`archive/patch-translation-qa-2026-08-22.md`](archive/patch-translation-qa-2026-08-22.md).

## Scope

The original Steam `content` field remains English source text. The translated
patch `sections` carry Russian change lines. Ability balance sections expose
the ability image/name and translated changes; they omit generic catalog
descriptions and stats. New-hero and new-ability introductions, plus full
ability replacements, expose the catalog/announcement description and
source-backed numeric stats. Stats come from the latest game-data catalog, not
a historical launch snapshot, so the client labels them as current values.
Each ability carries `presentation_kind` with
`balance`, `introduction` or `replacement`; the client displays description
and stats only for the latter two kinds. Intro stats use `{label, value}` rows
from the English/Russian game-data catalog, retaining only labeled numeric
damage, cooldown, duration, range/radius, healing, resistance, speed and related
properties or numeric upgrade bonuses. Only localized user-facing labels are
published; missing translated labels or source values stay absent. Introduced
and fully replaced abilities also carry `stats_pending`: `true` means supported
numeric source properties are unknown or only partly localized, `false` means
they are complete or the valid source property bag contains no supported
numeric stats, and a missing/null value means readiness is unknown (including
older cached catalog projections). Any available localized rows remain visible
while pending. Only these introduction/replacement states participate in the
existing five-minute asset-catalog retry; balance updates do not request
catalog retries for hidden metadata. A valid ability with no supported numeric
properties does not retry indefinitely.
Structural and entity labels keep their canonical English identity, with
separate localized display labels where the catalog provides them.

Mid Boss changes receive the `mid_boss` objective identity and the verified
neutral boss marker image when the source icon catalog provides it. Decorative
Steam image markup is removed from patch prose and translation input; section
art comes from the validated game-data catalog. Steam `[url]` wrappers retain
their visible anchor text without exposing the BBCode wrapper. Embedded
`Mid-Boss:` headings are split before objective grouping, and both `Mid Boss:`
and `Mid-Boss:` are removed from the displayed change after ownership is
assigned.

Patch parsing and acceptance are source-to-entity checks, not section-count
checks. For each announcement, compare the source changes with the resulting
DTO and confirm every change is represented exactly once under its source
owner: general, hero, ability, item or objective. Resolve source aliases such
as a short hero name to the canonical catalog entity, including when scope
headings and changes are inline BBCode in one paragraph. Do not merge adjacent
source groups into a single change, leave a group heading in change text, drop
an unmatched line, or route an entity change into General. Structural titles
and entity labels use canonical English names; localized display labels may
appear only as separate subtitles when the UI explicitly requests them.
Mode-specific hero changes are separate hero sections carrying their source
mode label in `mode_name`; the mode heading must not become a change under the
preceding hero ability or General. A catalog-backed auxiliary ability can be
attached to its owning hero even when it is not one of that hero's four
signature slots. Translated-text counts and section totals alone do not
establish that this contract holds.

The current hero, ability and shop-item catalog combines the community Deadlock API's per-patch game-data endpoints (`/v1/assets/heroes` and `/v1/assets/items`) with the legacy deadlock.io index when it returns valid JSON. The community endpoints expose the latest known client build and accept a `language=russian` parameter. Old Sparky joins English and Russian payloads by stable hero ID, ability class name and shop item name. Shop entries are projected from released `type=upgrade` records with a recognized `item_slot_type` and validated image URL. If either legacy endpoint is unavailable, redirected or non-JSON, valid community hero and item data supplies the catalog; if all sources fail, the last-good catalog is retained. These are community-parsed game files, not a Valve-operated API; Steam's announcement feed remains the source for patch prose. The API selects Russian values when present and English values otherwise, strips HTML and inline game tokens from descriptions, and constructs image URLs only from validated relative `/assets/` paths or the fixed Deadlock API asset CDN host. Hero entries may expose ordered `image_candidates` and patch hero sections may expose `hero_image_candidates`; the singular image URL mirrors the first candidate when the list is nonempty. Lists contain at most four distinct, validated remote URLs. Valid existing/prior candidates are retained before current community variants, whose order is card WebP, card PNG, small WebP, then small PNG. These are variants from the same approved asset host, not independent providers. Invalid and legacy deadlock.io/SteamDB image URLs are excluded. The UI tries each candidate once, then a bundled image that exists, then the placeholder; when an explicit list is present it does not retry the same API image after candidate exhaustion. Catalog HTML is never rendered.

When a new Steam announcement appears, the normal home-content refresh fetches the catalog in the same bounded refresh and reparses the four displayed patch details under the current detail-cache namespace. The worker's existing `home-content-refresh` schedule checks the Steam feed every 5 minutes. It avoids downloading asset bundles when nothing changed, and retries cached hero introductions with genuinely incomplete required metadata or unknown/partial supported stats at most once per 5 minutes, under the existing Redis lock. Catalog-pending state is separate from Russian-translation-pending state, so an intentionally hidden balance description does not trigger asset retries. An announcement can precede its game-data source; the UI reports required intro metadata and incomplete current stats as pending while the bounded schedule checks again. Missing Russian intro descriptions use English game text when available, otherwise the matching Steam ability span, and register only those fields with the DB-first translation pipeline. Detection is bounded by the 5-minute schedule; upstream publication delay remains outside the service's control.

The translation subsystem must not translate structural/entity labels such as:

- hero names;
- item names;
- ability names;
- rank names;
- section titles and other patch structure fields.

Protected game entity names that appear inside a change sentence are replaced with placeholders before the model call and restored unchanged afterwards.

The only current exception to blind entity placeholdering is the explicit
ambiguity set in `patch_translation_glossary.py`. Those strings remain visible
to the model so it can distinguish an item reference from a gameplay mechanic
using sentence and segment context.

## Source and canonical glossary

Patch changes are parsed from Valve/Steam content in English.

[`ISteamNews/GetNewsForApp`](https://partner.steamgames.com/doc/webapi/ISteamNews)
requests `maxlength=0`; Steam documents this as
returning full content, while a nonzero limit may return a generated blurb that
loses BBCode and paragraph boundaries before parsing. The decoded response is
bounded at 8 MiB in both the home refresh and patch-detail miss refresh; each
announcement's normalized text remains capped at 30,000 characters. Parser
`[/*]` list-item closers and separator-only hyphen lines are structural markup,
not change text, and are removed before parsing or translation. Parser changes
must bump the patch-detail cache namespace and register translations under the
hash of newly extracted segments, so old normalized source cannot be reused as
current detail or translation input.

The translation glossary is a **small checked-in canonical glossary owned by Old Sparky**. It is curated from Valve/Steam Russian Deadlock terminology and contains only important reusable gameplay mechanics: movement, damage, resistances, lifesteal, cooldowns, duration, range/radius, control durations and similar core concepts.

The glossary intentionally does **not** contain:

- hero names;
- item names;
- ability names;
- rank names;
- one-off item property labels;
- narrow combinations such as `Cooldown Per Headshot NPC`, `Move Speed per Stack`, or `Ambush Spirit Power`;
- a generated alias list for spelling/wording variants.

There is no runtime glossary discovery, network fetch, Redis glossary cache or
per-request glossary filtering. A small number of demonstrated wording aliases
may be explicit glossary keys that point to the same canonical Russian value.
Changing glossary terminology is a normal reviewed code change.

The entire canonical glossary is sent with every translation request because it is deliberately small. An English key is a **semantic concept anchor**, not an exact-match trigger. For example, `Move Speed` also governs semantically equivalent source wording such as `Movement Speed`, `Movespeed` and `move-speed`; source-backed `Gun Damage` mechanic wording maps to the `Weapon Damage` concept, while exact named-ability references use ENTITY protection. Keep aliases narrow and evidence-based; do not add a general generated alias table.

Compound wording must retain the mechanic family. For example, `Bullet, Spirit and Melee Lifesteal` represents `Bullet Lifesteal`, `Spirit Lifesteal` and `Melee Lifesteal`; it must never be reinterpreted as resistance terminology.

## Model input and context

The OpenAI request receives:

1. all extracted patch change segments with stable IDs;
2. the complete checked-in canonical glossary;
3. the explicit item/mechanic ambiguity list;
4. read-only context for each terse change when available: section kind/title and hero/item/ability label;
5. protected placeholders for non-ambiguous entity names and immutable facts;
6. instructions to use concise Valve-style Russian Steam patch-note wording without omitting source clauses.

Context exists only to disambiguate source wording. It is not output and does not expand translation scope: the model still returns only each segment ID and translated change text.

The request uses a stable `prompt_cache_key` derived from locale/translation version. Provider-side prompt caching should therefore be measured via returned `cached_tokens`. Old Sparky does not implement a separate LLM glossary cache. The existing Redis translation result cache remains more valuable because a fully cached patch avoids the model request entirely.

## Validation contract

Validation is intentionally narrow. It protects facts that can silently change gameplay meaning without trying to judge the overall quality of Russian prose.

The immutable checks are:

- numeric values, including signed values and percentages;
- ability upgrade tiers such as `T1`, `T2`, `T3`, etc.;
- protected entity placeholders;
- segment IDs/order and structured output shape.
- the source-specific meaning of `only N% as effective`, which denotes a
  fraction of baseline effectiveness rather than an increase or decrease by N
  percent.
- source-matched `Base gun damage` stat lines, which must not leave `Gun Damage`
  untranslated; named ability references keep their protected entity spelling.

Operators, arrows, English sentence order and other prose formatting are not treated as immutable facts.

The translation is not rejected merely because wording differs from the English syntax. Quality and terminology should primarily come from the model plus the canonical glossary, not from a large post-translation heuristic filter.

## Russian notation and style

After placeholders are restored, deterministic formatting aligns common numeric notation with Russian patch-note typography without changing numeric values:

- decimal point becomes a decimal comma: `0.75` -> `0,75`;
- seconds use Cyrillic `с`: `12s` -> `12 с`;
- meters use Cyrillic `м`: `28m` -> `28 м`;
- explicit `m/s` becomes `м/с`;
- milliseconds use `мс`.

Numeric comparison canonicalizes `.` and `,` so this localization does not cause a false fact mismatch.

Percent FACTs are dimensionless, including movement-speed percentages. During
placeholder restoration only, an immediately adjacent duplicate `%` or `m/s` /
`м/с` attached to a source FACT that itself ends in `%` is removed. This is
source-bound cleanup; it does not globally rewrite repeated percent signs or
alter absolute speed facts. The comparison check above is likewise limited to
the explicit source construction `only N% as effective`; it is not a general
Russian prose classifier.

For source shorthand where an `m` value semantically represents movement/sprint/dash speed, the model should use Valve-style `м/с`; distance/range/radius values remain `м`. This distinction is semantic and belongs in the translation instruction rather than a literal code-side keyword filter.

For prose, prefer Valve terminology and natural constructions. The model may inflect and restructure Russian text naturally while retaining the canonical glossary concept, but it must not omit source clauses.

## Runtime flow

1. Home-content or patch-detail refresh discovers Steam patch details, reparses all four displayed details under the current parser/catalog, and registers each exact source version in PostgreSQL.
2. Only a newly registered `pending` version is published to the low-priority translation queue; the database identity prevents repeated tasks.
3. Worker loads the structured English patch and Deadlock entity catalog, then verifies that the source hash still matches the queued version.
4. Change strings and descriptions required by introduction/replacement presentation are extracted; balance-only catalog descriptions and all image URLs are excluded.
5. Numeric/tier facts and non-ambiguous protected entity names are placeholder-protected.
6. Read-only segment context, the ambiguity list and the checked-in canonical glossary are attached to the OpenAI request.
7. The model returns every requested stable segment ID in source order; PostgreSQL is updated only after the full response validates.
8. Placeholders are restored and Russian numeric/unit notation is normalized.
9. Numeric values and tiers are compared against the English source.
10. Successful translations are committed to PostgreSQL first, then written to Redis under `PATCH_TRANSLATION_VERSION` and served by the patch API.
11. A Redis miss falls back to PostgreSQL; a missing database translation does not invoke OpenAI from the user request.

Patch source and catalog cache generations are independent of translation
state. Their keys and freshness markers are owned by service constants and
schema fields; do not duplicate transient key versions in runbooks. A cache
projection repair leaves older values untouched and uses the normal bounded
refresh. No Redis flush or database reset is part of the refresh.

An automatic retry of a failed translation task is limited to one delayed retry
for known network/timeout errors, guarded by the same source hash and row lock.
One task may issue up to three source-ordered initial provider requests. The
planner balances batches toward 100 segments and 6,500 source characters; these
are partition targets, not truncation limits. It preserves each segment ID and
all extracted text. The 30,000-character cap applies to normalized Steam
article content; required catalog-backed introduction and replacement
descriptions may add translated text beyond the article body and are not
subject to a new aggregate cap. After all initial batches finish, the worker
collects only stable segment IDs with a typed ENTITY/FACT placeholder failure
or a numeric-fact fingerprint mismatch. It keeps valid translations in memory
and, if needed, sends one source-ordered correction request containing only the
affected segments. The correction prompt requires unchanged IDs and exact
ENTITY/FACT placeholders; it does not retry arbitrary schema, ID or provider
errors. A transient provider retry and this aggregate validation correction
share one follow-up slot across the task. If that slot is already used, the
task fails; if corrected output still fails placeholder or fact validation,
the task fails without another request. No result is persisted unless every
source ID passes final validation.
The task therefore makes at most four provider calls and does not persist
partial batch results. At most two requests run at once within a task. The
worker's default concurrency is two tasks per worker instance, so that
instance can have at most four translation requests in flight. Configured
worker concurrency sets the aggregate bound. A 120-second monotonic task
deadline bounds provider work; each call is limited to the
smaller of 75 seconds or the remaining task time. The delayed durable retry
remains a separate queue attempt, and the existing maximum of two durable
attempts is unchanged.

The warm-up's `max_openai_calls` setting and `translation_calls` report count
cache-miss patch translations; they are not a count of provider HTTP requests.
Validation failures are not retried in a loop.

## Parser and catalog cache changes

When patch parsing or source normalization changes extracted sections, update
the detail-cache contract so new reads cannot reuse stale structure. Catalog
readiness is tracked by the `hero_image_candidates_version` marker: a warm
catalog without the marker is incomplete and schedules the normal bounded
refresh, while the last-good catalog remains available if the source is down.
The guarded home/game-assets refresh sets its existing pending-retry marker
when the projection is still incomplete, so repeated reads cannot trigger
unbounded source requests. Do not rewrite or delete older cache values to
repair them. Cache keys and
freshness markers are defined by the service constants; readers and writers
must use those shared definitions rather than duplicating key strings. The
base `home_content.refresh_home_content`, the guarded
`home_content_security.refresh_home_content`, and the bounded
`patch_detail_security._refresh_patch_cache_after_miss` path publish a rebuilt
catalog to both the current and last-good keys in one cache pipeline. The
platform worker reaches the base refresh through
`patch_detail.get_patch_detail`; the worker task
`platform.home_content_refresh` and public `/home` route call
`home_content_runtime.refresh_home_content`, which uses the guarded home
refresh. Public `/game-assets` uses the guarded catalog reader. Public patch
detail reads use `patch_detail_security.get_patch_detail`; a detail-cache miss
uses the bounded patch-miss path. The base catalog reader is also used by
`patch_detail.get_patch_detail` and rebuilds through the base home refresh.
Thus an old warm catalog cannot suppress rebuilding a projection that lacks
its required readiness marker. Existing older cache entries remain untouched.
The patch-miss refresh uses bounded negative/global gates to fetch the current
full Steam feed with `maxlength=0`, publishes current details under the
configured detail namespace, and registers translations keyed by the newly
extracted source hashes. The full HTTP response remains subject to the 8 MiB
byte cap, and each normalized
article remains capped at 30,000 characters. A cold catalog may defer one
bounded refresh through that same full-feed path; it must not fan out a
full-history reprocess. Existing translation rows are not automatically valid
for changed segments. Hero display metadata follows the current catalog
refresh, while English source content and canonical patch labels remain
intact.

A translation-version bump intentionally invalidates previous translation result cache entries when the translation contract or canonical glossary changes.

## Glossary maintenance

Keep the glossary small. Add a term only when at least one of these is true:

- it is a common Deadlock gameplay mechanic that appears across patches;
- the model repeatedly mistranslates or leaves it in English;
- comparison with Valve's Russian wording shows a stable canonical term that materially improves consistency.

Do not add a term merely because a single item property exposes it. Do not add source aliases such as `Movespeed` when `Move Speed` already represents the concept.

Likewise, do not grow the item/mechanic ambiguity list speculatively. Add an entry only when the same canonical English string is demonstrably both an entity name and a reusable gameplay mechanic and that collision changes model input semantics.

## QA

For translation changes, use at least the four newest patches available in production as a regression set. Compare:

- the English source change;
- Old Sparky's generated Russian `sections` change (the source `content` field remains English);
- Valve's Russian Steam wording where the same concept/change is available.

Also verify balance abilities omit catalog prose/stats, introductions and
replacements include only catalog-backed descriptions/stats, image URLs are
not translation segments, new heroes retain image/name metadata, and each
objective change remains grouped with its identity/image. Reparse the exact
four displayed patch IDs from their source details after a parser namespace
change; a source-only parser test does not repair cached patch structures.

Pay particular attention to movement speed, cooldown, spirit power/scaling, bullet/spirit/melee resistance, bullet/spirit/melee lifesteal, range/radius, duration and common upgrade wording.

If a real patch exposes a missing important concept, add that canonical term to the checked-in glossary and rerun the same regression set. Do not introduce dynamic glossary discovery or per-request filtering.

Structural production checks are fail-fast, but the post-deploy workflow is a
controlled cache warm-up rather than read-only QA: a cache miss acquires a
Redis lock, may call OpenAI and writes the translation cache. The workflow is
named `Platform patch translation warm-up`, requires an explicit maximum number
of cache-miss patch translations (four by default), and reports that patch
translation budget in its result. A translation may make one additional
bounded request from the shared transient-retry or aggregate validation
correction allowance. It must not run in a polling loop or be described as a
pure QA check. A read-only check must fail on cache miss instead of invoking
the translator.

Manual/diagnostic comparison is still required when changing prompt/glossary behavior because numeric correctness alone cannot detect terminology mistakes, item/mechanic ambiguity, prose truncation or awkward Russian phrasing.

## Code owners

- `apps/platform_api/app/services/patch_translation_config.py` — translation version/cache constants and OpenAI endpoint.
- `apps/platform_api/app/services/patch_translation_glossary.py` — checked-in canonical glossary and explicit item/mechanic ambiguity list.
- `apps/platform_api/app/services/patch_translation_terms.py` — entity/fact protection and Russian notation.
- `apps/platform_api/app/services/patch_translation_runtime.py` — model input/context, prompt-cache key, request, validation, result cache and merge flow.
- `apps/platform_worker/worker.py` — background scheduling/execution.
- `python_packages/platform_infra/models.py` and the corresponding Alembic revision — durable translation state and its identity constraints.
- `tools/platform_refresh_home_content.py` — operator/startup refresh without a second translation enqueue path.
- `tests/test_platform_patch_translation*.py` — regression coverage.
