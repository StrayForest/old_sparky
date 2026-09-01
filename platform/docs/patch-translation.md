# Deadlock patch translation

- Status: Active / DB-first implementation deployed
- Owner: Platform maintainers
- Last reviewed: 2026-09-01

This document owns the contract for automatic Russian translation of Deadlock patch-note changes.

## Current implementation — 2026-09-01

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
translations are retained as failed state rather than silently requeued on
every refresh.

The deployed translation version is `ru-v10`, defined in
`apps/platform_api/app/services/patch_translation_config.py`. It includes the
durable DB-first state, the reviewed ambiguity handling and the immutable
numeric/tier validation described below. The four-patch regression procedure
and its historical evidence live in
[`archive/patch-translation-qa-2026-08-22.md`](archive/patch-translation-qa-2026-08-22.md).

## Scope

Only patch **change text** is translated. The translation subsystem must not translate structural/entity labels such as:

- hero names;
- item names;
- ability names;
- rank names;
- section titles and other patch structure fields.

Those labels remain in the canonical form already used by the site. Protected game entity names that appear inside a change sentence are replaced with placeholders before the model call and restored unchanged afterwards.

The only current exception to blind entity placeholdering is the explicit
ambiguity set in `patch_translation_glossary.py`. Those strings remain visible
to the model so it can distinguish an item reference from a gameplay mechanic
using sentence and segment context.

## Source and canonical glossary

Patch changes are parsed from Valve/Steam content in English.

The translation glossary is a **small checked-in canonical glossary owned by Old Sparky**. It is curated from Valve/Steam Russian Deadlock terminology and contains only important reusable gameplay mechanics: movement, damage, resistances, lifesteal, cooldowns, duration, range/radius, control durations and similar core concepts.

The glossary intentionally does **not** contain:

- hero names;
- item names;
- ability names;
- rank names;
- one-off item property labels;
- narrow combinations such as `Cooldown Per Headshot NPC`, `Move Speed per Stack`, or `Ambush Spirit Power`;
- a generated alias list for spelling/wording variants.

There is no runtime glossary discovery, network fetch, Redis glossary cache or per-request glossary filtering. Changing glossary terminology is a normal reviewed code change.

The entire canonical glossary is sent with every translation request because it is deliberately small. An English key is a **semantic concept anchor**, not an exact-match trigger. For example, `Move Speed` also governs semantically equivalent source wording such as `Movement Speed`, `Movespeed`, `move-speed` and similar phrasing. The model is responsible for semantic matching; the application does not maintain an alias table.

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

For source shorthand where an `m` value semantically represents movement/sprint/dash speed, the model should use Valve-style `м/с`; distance/range/radius values remain `м`. This distinction is semantic and belongs in the translation instruction rather than a literal code-side keyword filter.

For prose, prefer Valve terminology and natural constructions. The model may inflect and restructure Russian text naturally while retaining the canonical glossary concept, but it must not omit source clauses.

## Runtime flow

1. Home-content or patch-detail refresh discovers Steam patch details and registers the exact source version in PostgreSQL.
2. Only a newly registered `pending` version is published to the low-priority translation queue; the database identity prevents repeated tasks.
3. Worker loads the structured English patch and Deadlock entity catalog, then verifies that the source hash still matches the queued version.
4. Only change strings are extracted.
5. Numeric/tier facts and non-ambiguous protected entity names are placeholder-protected.
6. Read-only segment context, the ambiguity list and the checked-in canonical glossary are attached to the OpenAI request.
7. The model semantically maps English wording variants to canonical glossary concepts and disambiguates the three item/mechanic collisions from context.
8. The model returns the same segment IDs in the same order without dropping source clauses.
9. Placeholders are restored and Russian numeric/unit notation is normalized.
10. Numeric values and tiers are compared against the English source.
11. Successful translations are committed to PostgreSQL first, then written to Redis under `PATCH_TRANSLATION_VERSION` and served by the patch API.
12. A Redis miss falls back to PostgreSQL; a missing database translation does not invoke OpenAI from the user request.

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
- Old Sparky's generated Russian change;
- Valve's Russian Steam wording where the same concept/change is available.

Pay particular attention to movement speed, cooldown, spirit power/scaling, bullet/spirit/melee resistance, bullet/spirit/melee lifesteal, range/radius, duration and common upgrade wording.

If a real patch exposes a missing important concept, add that canonical term to the checked-in glossary and rerun the same regression set. Do not introduce dynamic glossary discovery or per-request filtering.

Structural production checks are fail-fast, but the post-deploy workflow is a
controlled cache warm-up rather than read-only QA: a cache miss acquires a
Redis lock, may call OpenAI and writes the translation cache. The workflow is
named `Platform patch translation warm-up`, requires an explicit maximum number
of cache-miss/OpenAI calls (four by default), and reports the mutation budget in
its result. It must not run in a polling loop or be described as a pure QA
check. A read-only check must fail on cache miss instead of invoking the
translator.

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
