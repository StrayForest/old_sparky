# Performance stage request — 2026-09-06

This file preserves the user's complete performance-stage request so the
working objective survives context compression. It is an active work order;
completed evidence belongs in a dated archive report and the current
conclusion belongs in `platform/docs/CURRENT.md`.

## Task

Провести следующий performance-этап production после полной нагрузочной
матрицы. Сначала диагностика и фиксация baseline, затем оптимизация, затем
повтор той же матрицы без подмены сценариев.

## Context and baseline

Полная production-нагрузочная матрица из 13 dispatchable-профилей завершена:

- 10 PASS
- 3 FAIL
- production после тестов очищен
- активных load-run и lock нет
- `dev == origin/dev`
- lifecycle-профили на production не запускать: они разрешены только через QA/preprod

| Профиль | Результат |
| --- | --- |
| Ready Vote SLO | PASS: 500+100 запросов, 0 ошибок, p95 193 ms, p99 425 ms |
| Read mix human | PASS: 500 запросов, p95 232 ms |
| Read mix stress | PASS: 30k запросов, 0 ошибок, CPU origin до 100% |
| Read concurrency ramp | FAIL: 5 timeout на c16; throughput далее ≈105–107 req/s |
| Authenticated page load | PASS: 20k ответов 200; p95 3.745 s, TTFB p95 3.159 s |
| Ready Vote capacity | FAIL на 80 actions/s; SLO capacity = 70 actions/s, 80 даёт 12.9% shedding |
| Saturation v1 | PASS, shedding 1.28% |
| Saturation v2 | PASS, shedding 16.1% |
| Saturation v3 | FAIL: 79 timeout, 1×522 |
| Saturation v4 | PASS: final failure 0.39% |
| Stress 15k | PASS stress behavior; shedding 46.5% |
| Stress 20k | PASS stress behavior; shedding 52.7%, CPU ~94% |
| Spike | PASS: 1800/1800, включая burst 80 actions/s, 0 ошибок |

Дополнительная аномалия: run `33991798604` дал 72 timeout + 2×520; следующий
SLO-run после read-only health-check прошёл. Root cause не доказан. Считать это
`unexplained transient production anomaly`, а не закрывать предположением о
Cloudflare/origin.

## Main goal

Не добавлять новые нагрузочные сценарии ради количества тестов.

1. Определить точные bottleneck-и authenticated/read path.
2. Устранить причины высокого TTFB и CPU saturation.
3. Устранить timeout/522 при saturation.
4. Не ухудшить Ready Vote.
5. Повторить ту же нагрузочную матрицу и сравнить результаты с baseline.

Приоритет — уменьшить стоимость одного запроса, а не увеличивать
concurrency/pool size вслепую.

## Required phases

### 1. Baseline

- Найти текущую документацию нагрузочных тестов и performance-аудита.
- Зафиксировать результаты всех 13 профилей как baseline.
- Зафиксировать production workers, uvicorn/gunicorn, nginx limits/timeouts/keepalive,
  PgBouncer, PostgreSQL pool sizes, Redis, application concurrency limits,
  shedding/admission-control thresholds и repository-controlled Cloudflare assumptions.
- Не менять профили/thresholds ради PASS.
- Не менять production infrastructure без доказанной причины.

Создать рабочий performance-документ с полями для каждого изменения:
проблема, доказательство, изменение, ожидаемый эффект, риск, тест, результат
до/после.

### 2. Authenticated page load

Разобрать baseline total p95 `3.745 s` и TTFB p95 `3.159 s`. Добавить
production-safe или QA/preprod instrumentation для Cloudflare/ingress (если
измеримо), nginx, worker queue, FastAPI middleware, session/auth, Redis, DB
checkout, SQL, template/render/serialization и response start. Отделить
client-observed TTFB, nginx request/upstream time и application time, если
Cloudflare нельзя увидеть из приложения.

Проверить SQL count/repetition/N+1/sequential independent queries/joins/indexes,
repeated user/session/profile/permission reads, Redis round-trips, synchronous
work, middleware, crypto/serialization, blocking I/O, DB pool wait and
authenticated-read contention. Для подозрительных SQL использовать безопасный
`EXPLAIN (ANALYZE, BUFFERS)`. SQL не оптимизировать без доказательства.

### 3. Read-mix ceiling

Разобрать ceiling `≈105–107 req/s`, CPU `100%`, latency growth and no useful
gain from more concurrency. Production-safe или reproducible QA/preprod
profiling должен показать CPU по endpoint, Python stack, JSON/Pydantic/ORM/DB,
auth/session, logging/middleware, templating, compression, Redis и networking.

Для каждого read endpoint измерить requests/sec, p50/p95/p99, CPU cost, SQL
count/DB duration, pool checkout, Redis operations, response size, cache hit/miss
и serialization/render cost. Составить рейтинг `request frequency × CPU cost/request`.

### 4. DB/pool saturation

Измерить active/idle PostgreSQL connections, waiting backends, query/lock/
transaction duration, PgBouncer wait, application pool checkout p50/p95/p99,
pool utilization, simultaneous DB operations, PostgreSQL CPU/I/O. Установить,
является ли DB bottleneck, является ли pool bottleneck или очередью перед CPU,
и поможет/навредит ли увеличение pool. При saturated origin CPU pool/concurrency
не увеличивать без доказательства.

### 5. Saturation v3

Объяснить путь 79 timeout + 1×522: accepted → waiting resource → saturation →
почему shedding не сработал → timeout/522. Сравнить v1/v2/v3/v4 и проверить
admission control, semaphore, queue, worker exhaustion, DB wait, nginx timeout,
keepalive, backlog, reuse, Cloudflare boundary, cancellation, retries,
endpoint mix, slow clients и leaks. Controlled overload должен завершаться
ранним предсказуемым допустимым отказом, не timeout/522. PASS следующего run
сам по себе не считать исправлением.

### 6. Transient anomaly

Коррелировать run `33991798604` с nginx/app/system/CPU/load/memory/network/
conntrack/socket/PostgreSQL/Redis/Cloudflare/deploy/restart/health data.
Результат: установленная причина, сильная гипотеза или недостаточно данных.
Root cause не придумывать. При недостаточной observability добавить минимальные
bounded метрики/логи для следующего аналогичного эпизода.

### 7. Optimizations

После диагностики выполнять только доказанные изменения: лишние SQL/indexes/
ORM/batch queries/Redis round-trips/request-scoped auth reuse/safe read-only
cache/serialization/response schemas/blocking I/O/middleware/template/server
compute/early shedding/bounded queues/concurrency limits. Не делать rewrite,
microservices, Kafka и аналогичную инфраструктуру без измеримой необходимости.

### 8. Regression gates

После каждого значимого изменения запускать unit, integration, relevant browser,
security/build и targeted performance checks. Контролировать Ready Vote,
authentication, cache/session isolation, authorization, rate limits,
CSRF/Turnstile, private/no-cache semantics и cleanup. Не допускать security
regression ради performance.

### 9. Targeted retest

- Authenticated page: target TTFB p95 `< 1.0 s`, desirable `< 700 ms`, zero
  unexpected errors, correct auth and unchanged semantics.
- Read concurrency ramp: increase useful throughput ceiling or lower latency at
  same throughput; no timeout in normal operating region.
- Saturation v3: zero timeout and zero 520/522; only contractual controlled shedding.
- Ready Vote SLO: no regression from p95 `193 ms`, p99 `425 ms`, zero errors;
  sustained capacity must not fall below `70 logical actions/s`.

Не считать успехом увеличение concurrency без роста useful throughput.

### 10. Full 13-profile matrix

Only after targeted PASS, repeat the original production matrix with the same
workload definitions, concurrency/rates, thresholds and dataset sizes. Do not
ease scenarios. Keep the lifecycle production ban. For every profile record
baseline, new result, absolute delta, percentage delta and PASS/FAIL. Compare
Ready Vote p95/p99/capacity, read p95/useful throughput/knee, authenticated
p95/TTFB, shedding, timeout/520/522, CPU, DB pool waits and PostgreSQL use.

### 11. Capacity conclusion

Current baseline unless repeatable measurements change it:

- Ready Vote sustained SLO capacity: `70 logical actions/s`
- recommended operating range: `<= 60–65 actions/s`
- burst: `80 actions/s` confirmed for tested spike
- useful read ceiling: `≈105–107 req/s`
- read concurrency knee: `≈32`
- authenticated page: functionally stable but p95/TTFB unacceptable as final target

### 12. Cleanup and docs

After every production load test remove test users, tournaments, sessions and
test audit logs; retain only the control account; verify no active load runs or
locks; stop observers; check working tree and `dev == origin/dev`. Do not retain
load fixtures without explicit need.

## Definition of Done

- authenticated root bottleneck established;
- read ceiling root bottleneck established;
- Saturation v3 timeout path explained and fixed or documented as external/
  unfixable with evidence;
- `33991798604` investigated as far as data allows;
- evidence-backed optimizations implemented;
- targeted gates pass;
- Ready Vote does not regress;
- original 13-profile production matrix repeated;
- before/after documented;
- production cleanup confirmed;
- lifecycle tests not run on production;
- repository clean;
- completed performance step archived;
- active docs contain only unfinished next steps.

## Working rule

`measure → prove bottleneck → change → targeted test → compare → continue`.

Не делать несколько крупных performance-изменений одновременно, если это
лишает возможности понять, какое изменение дало результат. Не оптимизировать
ради PASS; цель — реальное увеличение useful capacity и уменьшение latency при
сохранении корректности и защиты production.
