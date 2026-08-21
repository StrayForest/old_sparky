# Deadlock patch translation QA — 2026-08-22

## Scope

This checkpoint turns the latest four production patches into the standing translation regression set:

- `1840944183775204` — `Minor Update - 08-12-2026`;
- `1839676055886206` — `Matchmaking Update`;
- `1839041357039193` — `Minor Update - 07-28-2026`;
- `1836506165584438` — `Minor Update - 07-09-2026`.

The previous `ru-v7` production QA already proved structural correctness for this set while exposing terminology and prose-quality failures. The current `dev` implementation is `ru-v8` and is intended to fix those known failures before any broader translation architecture is added.

## Confirmed `ru-v7` failures

The regression set must continue to cover these classes of failure:

1. Numeric/stat uses of `Bullet Lifesteal` and `Spirit Lifesteal` were left in English.
2. Coordinated Bullet/Spirit/Melee Lifesteal wording was incorrectly interpreted as resistance terminology.
3. A true item-name reference involving `Melee Lifesteal` must remain the English entity name rather than being forced through the mechanic glossary.
4. Long Matchmaking Update prose had a clause omission/truncation case.
5. Some output preserved gameplay meaning but did not match concise, natural Valve-style Russian wording.

## Valve Russian terminology checkpoint

Comparison against Russian Steam localization for the same recent update family confirms the current canonical direction:

| English concept | Canonical Russian direction |
| --- | --- |
| Move Speed | скорость передвижения |
| Sprint Speed | скорость бега |
| Spirit Power | спиритическая мощь |
| spirit scaling / coefficient | коэффициент масштабирования от спиритической мощи |
| Cooldown value | время перезарядки |
| Falloff Range | эффективная дальность |
| Bullet Resist | сопротивляемость пулям |
| Spirit Resist | сопротивляемость спиритизму |
| Bullet Lifesteal | кража здоровья пулями |
| Spirit Lifesteal | кража здоровья спиритизмом |
| Melee Lifesteal | кража здоровья в ближнем бою |

The checked-in glossary already covers the reusable stat families above. `ru-v8` additionally instructs the model to render cooldown and spirit-scaling phrases naturally rather than forcing a literal noun substitution.

## Architecture decision

Do **not** add a runtime translation-memory service, dynamic Steam scraping, a generated alias table, or another LLM pass yet.

For the current problem, Russian Steam should be treated as a **QA oracle and terminology reference**, not as a runtime dependency. The immediate failure was entity/mechanic ambiguity plus insufficient prose constraints, and `ru-v8` addresses that directly with:

- a small reviewed canonical glossary;
- explicit item/mechanic collision handling;
- per-segment context;
- immutable numeric/tier protection;
- explicit no-omission instructions;
- versioned result caching.

A larger translation-memory layer becomes justified only if `ru-v8` still shows repeated sentence-level deviations after the four-patch regression pass.

## Release gates for `ru-v8`

Before production release, all of the following must hold:

- normal CI is green;
- all four patch structures preserve segment IDs/order and numeric/tier facts;
- no stat use of Bullet/Spirit/Melee Lifesteal is left in English;
- no Lifesteal family phrase is translated as Resist/Resistance;
- true item-name collision references stay canonical English entity names;
- the long Matchmaking Update sample contains every source clause;
- recurring mechanics use the canonical Russian terminology above;
- a manual EN -> Old Sparky RU -> Valve RU review finds no material meaning loss in the sampled lines.

If a failure remains after this pass, classify it first as terminology, ambiguity, omission, style, numeric fact, or entity preservation. Change the smallest responsible layer and bump the translation version only when runtime translation behavior changes.

## Regression coverage added

`tests/test_platform_patch_translation_real_regressions.py` now locks in the July 28 Lifesteal failures at the model-input boundary and verifies coverage of the recurring terminology families used by the latest patches.

This test does not pretend to score free-form Russian prose. Prose quality remains a production QA comparison against Valve Russian wording; deterministic tests protect the inputs and invariants that previously made a correct translation impossible.
