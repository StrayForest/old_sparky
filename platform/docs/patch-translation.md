# Deadlock patch translation

- Status: Active / QA paused
- Owner: Platform maintainers
- Last reviewed: 2026-08-19

This document owns the contract for automatic Russian translation of Deadlock patch-note changes.

## Current checkpoint — 2026-08-19

Work is intentionally paused here because GitHub Actions limits are exhausted. Do not continue implementation blindly; resume from this checkpoint.

### Confirmed and deployed

`ru-v7` was deployed successfully and production QA was run once against the four newest patches available at that moment:

- `1840944183775204` — `Minor Update - 08-12-2026`;
- `1839676055886206` — `Matchmaking Update`;
- `1839041357039193` — `Minor Update - 07-28-2026`;
- `1836506165584438` — `Minor Update - 07-09-2026`.

Structural QA passed for all four patches:

- segment IDs/order were preserved;
- numeric values and `T1`/`T2`/`T3`-style tiers were preserved;
- structural hero/item/ability labels were unchanged;
- the checked-in glossary contained 54 terms;
- no runtime glossary fetch, Redis glossary cache or dynamic glossary discovery remained.

The production QA also exposed terminology/quality problems that structural validation correctly does not hide. Important examples:

- `+10% Bullet Lifesteal` remained `Bullet Lifesteal` in English;
- `+10% Spirit Lifesteal` remained `Spirit Lifesteal` in English;
- coordinated `Bullet, Spirit and Melee Lifesteal` was mistranslated as resistance terminology;
- long `Matchmaking Update` prose showed at least one truncation/omission case, so the prompt must explicitly require every source clause to be represented;
- some Russian wording is mechanically correct but stylistically awkward and still needs manual regression review.

### Root cause found

The Lifesteal failure is not caused by the 54-term glossary being too small.

`Bullet Lifesteal`, `Spirit Lifesteal` and `Melee Lifesteal` are ambiguous in Deadlock because the same English strings can be both:

1. actual item names, which normally must remain untranslated; and
2. gameplay stat/mechanic names, which must use the canonical Russian glossary term.

The existing entity-protection stage treated these strings as item names before the model saw them, so mechanic occurrences could become `ENTITY` placeholders and were therefore impossible for the model to translate correctly.

### Already implemented in `dev`, but NOT yet CI-verified or deployed

The next revision is `ru-v8`. The code changes are already committed to `dev`, but GitHub Actions limits stopped us before verification/deployment.

`ru-v8` currently includes:

- the same small 54-term canonical glossary;
- an explicit three-entry ambiguity set: `Bullet Lifesteal`, `Spirit Lifesteal`, `Melee Lifesteal`;
- those three strings are no longer blindly hidden as entity placeholders;
- the model is instructed to use segment context to distinguish item-name usage from mechanic/stat usage;
- examples in the prompt distinguish `+10% Bullet Lifesteal` as a mechanic from wording such as `same change for Melee Lifesteal` as an item reference;
- the prompt now explicitly forbids shortening/truncating long segments and requires every source clause to be represented;
- OpenAI `prompt_cache_key` is set to a stable translation-version key;
- OpenAI usage logging records input tokens, cached input tokens and output tokens so prompt-cache effectiveness can be measured rather than guessed;
- `get_translation_glossary()` remains static and no longer performs a pointless `force_refresh` runtime operation;
- new unit tests were added for the ambiguity set, visibility of ambiguous mechanics to the model, stable prompt cache key and cached-token usage parsing.

Important: these `ru-v8` changes have **not** yet passed CI and have **not** been deployed. Treat `dev` as an unverified checkpoint until Actions are available again.

### Resume sequence after GitHub Actions limits reset

Do this in order:

1. Run normal CI for current `dev` and fix only actual failures.
2. Deploy `ru-v8` only after CI is green.
3. Re-run one fail-fast production QA against the four newest patches; do not add a polling loop.
4. Confirm specifically that numeric/stat uses of `Bullet Lifesteal`, `Spirit Lifesteal` and coordinated `Bullet, Spirit and Melee Lifesteal` are translated into canonical Russian terminology.
5. Confirm a true item-name reference such as `same change for Melee Lifesteal` remains the English item name.
6. Re-check the long `Matchmaking Update` segment and verify that no source clause is omitted.
7. Review `patch_translation_openai_usage` logs across repeated requests and record whether `cached_tokens` is materially greater than zero. Do not build another application-side glossary cache just to imitate provider prompt caching.
8. Compare EN -> Old Sparky RU -> Valve RU wording where available. If a recurring important mechanic is genuinely missing, add only that canonical term to the checked-in glossary and repeat the same regression set.
9. Remove/close the temporary production-QA branch/PR used for the one-off `ru-v7` inspection if it is still present. It must never be merged into `dev`.

Do not reintroduce dynamic glossary generation, per-request glossary filtering, a large alias table, or broad Russian prose heuristics unless a concrete regression demonstrates the need.

## Scope

Only patch **change text** is translated. The translation subsystem must not translate structural/entity labels such as:

- hero names;
- item names;
- ability names;
- rank names;
- section titles and other patch structure fields.

Those labels remain in the canonical form already used by the site. Protected game entity names that appear inside a change sentence are replaced with placeholders before the model call and restored unchanged afterwards.

The only current exception to blind entity placeholdering is the explicit ambiguity set documented above. Those strings remain visible to the model so it can distinguish an item reference from a gameplay mechanic using sentence and segment context.

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

1. Home-content refresh discovers patch IDs and queues translation jobs.
2. Worker loads the structured English patch and Deadlock entity catalog.
3. Only change strings are extracted.
4. Numeric/tier facts and non-ambiguous protected entity names are placeholder-protected.
5. Read-only segment context, the ambiguity list and the checked-in canonical glossary are attached to the OpenAI request.
6. The model semantically maps English wording variants to canonical glossary concepts and disambiguates the three item/mechanic collisions from context.
7. The model returns the same segment IDs in the same order without dropping source clauses.
8. Placeholders are restored and Russian numeric/unit notation is normalized.
9. Numeric values and tiers are compared against the English source.
10. Successful translations are cached under `PATCH_TRANSLATION_VERSION` and served by the patch API.

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
- `tests/test_platform_patch_translation*.py` — regression coverage.
