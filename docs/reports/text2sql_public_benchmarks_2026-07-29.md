# Text-to-SQL: BIRD Mini-Dev и Spider 2.0-Lite SQLite

Прогон начат 29 июля 2026 года и завершён 30 июля 2026 года.

## Итог

После первичной диагностики исправлены подтверждённые дефекты schema linking,
semantic verification, structured output, NLU и retry policy. Затем оба
набора запущены заново на одной и той же ревизии
`38ce19915109fb1250198eb84385f5d605f319a0`.

| Набор | Кейсы | Official EX | SQL исполнен | Conditional EX |
|---|---:|---:|---:|---:|
| BIRD Mini-Dev SQLite | 500 | 195/500 = **39,00%** | 299/500 = **59,80%** | 195/299 = **65,22%** |
| Spider 2.0-Lite SQLite subset | 135 | 2/135 = **1,48%** | 2/135 = **1,48%** | 2/2 = **100%** |

Исторические artifacts зафиксированы в [baseline registry](text2sql_public_benchmark_baseline_2026-07-29.json). Это проверяемое историческое свидетельство, а не release evidence и не основание для production admission.

Главный вывод:

- на BIRD качество дошедшего до исполнения SQL уже существенно выше общего
  score, но пайплайн теряет 201 кейс до execution и даёт неверный результат в
  99 из 299 исполненных кейсов;
- на Spider измерено прежде всего разрушение coverage: 133 из 135 кейсов не
  дошли до execution. Обе исполнившиеся заявки официально правильны, но выборка
  из двух кейсов ничего не доказывает о semantic accuracy;
- `DB_AUDIT_OUTPUT_INVALID` не является основной причиной провалов. Это
  ошибочная terminal projection: в 239 случаях двух наборов `db_audit` был
  пропущен после более раннего сбоя.

## Что оценивалось

### Наборы

- BIRD Mini-Dev: все 500 SQLite-кейсов, 11 баз,
  `148 simple / 250 moderate / 102 challenging`.
- Spider 2.0-Lite: все 135 локально доступных `local*` SQLite-кейсов, 30 баз
  и 13 внешних документов. Это подмножество официальных 547 Lite-задач, а не
  полный leaderboard run.

Источники:

- [BIRD Mini-Dev](https://github.com/bird-bench/mini_dev), revision
  `b3d4bcbbae9a96934ad812551eb400c7a3b23c12`;
- [Spider 2.0](https://github.com/xlang-ai/Spider2), revision
  `01a4c67c1e3f6ab9032716b050a927abbb245f65`.

### Протокол

Каждый scored-кейс проходил полный authenticated путь:

`question + public evidence -> API -> workflow -> SQL -> SQLite execution -> terminal outcome`

Условия:

- шесть изолированных runtime-shards, один worker в каждом;
- один одновременно исполняемый run на supervisor;
- timeout одного кейса — 900 секунд;
- SQL-memory и heuristic schema fallbacks отключены;
- gold SQL и gold result использовал только официальный evaluator;
- неисполненный кейс получает заведомо невалидный SQL, поэтому не может
  случайно получить балл за пустой expected result;
- каждый ordinal присутствует в канонических observations ровно один раз.

Диагностические probes не включены в score. Probe с четырьмя workers показал
18 queue-deadline timeout из 51 заявки. Probe с admission limit 1 создал 168
HTTP 409. Оба сохранены отдельно и не смешаны с каноническими результатами.

Gateway не сообщает фактически выбранную downstream-модель, seed и sampling
metadata каждого stage. Manifest поэтому фиксирует logical routes и hashes
конфигурации, но не может доказать downstream model identity.

## Что было исправлено перед повторным запуском

В clean restart вошли следующие изменения:

1. Декоративная LLM-токенизация убрана с critical path; NLU теперь локальная.
2. Для уже связанных таблиц всегда строится closure по authoritative foreign
   keys, независимо от отключённых heuristic fallbacks.
3. Дубликаты двунаправленных FK-нормализаций схлопываются.
4. Валидированные LLM joins объединяются с authoritative FK closure вместо
   взаимного исключения.
5. Filter считается разрешённым при точном физическом binding
   `table + column`, даже если semantic role изменилась.
6. Query controls и derived concepts больше не требуют несуществующей
   физической колонки.
7. Verifier получает исходный вопрос и intent, а не только SQL.
8. Workflow принимает ровно один однозначный JSON-object из fenced agent
   response; неоднозначный результат остаётся ошибкой.
9. Engine-level retries для долгих model stages отключены, чтобы timed-out
   вызовы не накладывались друг на друга.

Изменения покрыты regression-тестами. Перед прогоном прошли:

- schema-linking suite: 120 tests;
- Text-to-SQL core suite: 97 tests;
- AG-UI/workflow suite: 229 tests.

Практическая проверка исправления FK: BIRD ordinal 1 перешёл из
`JOIN_PATH_FAILED` в `succeeded`. Ordinal 3 прошёл schema linking с корректным
join graph, но позже упал по timeout генерации. Это показывает, что join fix
работает, а следующий bottleneck находится уже дальше в pipeline.

## BIRD Mini-Dev SQLite

Официальный execution score:

| Difficulty | Correct / total | EX |
|---|---:|---:|
| Simple | 93/148 | **62,84%** |
| Moderate | 76/250 | **30,40%** |
| Challenging | 26/102 | **25,49%** |
| Total | 195/500 | **39,00%** |

### Разложение всех 500 кейсов

| Класс | Кейсы | Доля |
|---|---:|---:|
| Officially correct | 195 | 39,0% |
| SQL исполнен, результат неверный | 99 | 19,8% |
| Schema-linking abstention | 62 | 12,4% |
| Другой terminal failure | 137 | 27,4% |
| Runner/transport failure | 5 | 1,0% |
| Prediction execution error | 1 | 0,2% |
| Verifier rejection | 1 | 0,2% |

Terminal statuses:

- `succeeded`: 295;
- `failed`: 137;
- `abstained`: 63;
- runner/transport без валидного terminal outcome: 5.

### Что скрывается за 137 terminal failures

| Реальная первая упавшая стадия | Кейсы | Детализация |
|---|---:|---|
| SQL generation | 65 | 53 timeout, 12 invalid structured output |
| SQL verification | 53 | 50 timeout, 3 invalid structured output |
| Schema linking | 16 | 14 timeout, 2 no linked entities |
| Intent extraction | 1 | 1 timeout |
| DB audit | 2 | 2 invalid structured output |

Из них 131 ошибочно получили reason
`DB_AUDIT_OUTPUT_INVALID`: `db_audit` был `skipped` после failure предыдущего
шага. Ещё четыре generation timeout получили `OUTPUT_RETRY_CHAIN_FAILED`.

Schema abstention:

- `UNRESOLVED_ENTITIES`: 45;
- `JOIN_PATH_FAILED`: 18.

Сумма decision reasons на единицу больше terminal schema-abstention, потому
что decision reason и итоговый failure class являются разными projections.

### Ошибки после успешного execution

99 исполненных SQL вернули неправильный официальный результат. Это уже не
transport-проблема, а semantic precision:

- теряются обязательные predicates и literals;
- semantic entity привязывается к физически существующей, но неверной таблице;
- меняются aggregation, grouping или требуемая cardinality;
- verifier одобряет безопасный и исполнимый SQL, который не полностью отвечает
  исходному вопросу.

Следовательно, один рост coverage не поднимет EX автоматически. Нужен
детерминированный semantic coverage gate над SQL AST и typed intent.

### Latency

| Метрика | Время |
|---|---:|
| Mean | 311,43 s |
| Median | 314,16 s |
| P90 | 466,01 s |
| P95 | 554,42 s |
| Max | 881,87 s |

Среднее по дорогим стадиям:

- intent extraction — 23,19 s;
- schema linking — 52,06 s;
- SQL generation — 122,55 s;
- SQL verification — 110,56 s.

## Spider 2.0-Lite SQLite subset

Официальный evaluator: **2/135 = 1,4815%**. Правильные кейсы:
`local040` и `local264`.

Строку evaluator `2/547 = 0,3656%` нельзя использовать как leaderboard score:
400 non-SQLite задач не запускались. Корректный denominator этого эксперимента
равен 135.

### Разложение всех 135 кейсов

| Класс | Кейсы | Доля |
|---|---:|---:|
| Officially correct и executed | 2 | 1,48% |
| Другой terminal failure | 114 | 84,44% |
| Schema-linking abstention | 18 | 13,33% |
| Runner/transport failure | 1 | 0,74% |

Pipeline сгенерировал SQL только в шести кейсах и исполнил два.

### Что скрывается за terminal failures

108 кейсов получили ложный umbrella reason `DB_AUDIT_OUTPUT_INVALID`:

| Реальная первая упавшая стадия | Кейсы | Детализация |
|---|---:|---|
| SQL generation | 66 | 64 timeout, 2 invalid structured output |
| Schema linking | 36 | 35 timeout, 1 no linked entities |
| SQL verification | 4 | 4 timeout |
| Intent extraction | 2 | 2 timeout |

Оставшиеся технические потери:

- 6 `MANDATORY_STEP_NOT_COMPLETED` за 1,03–1,05 секунды с ошибкой
  `Text-to-SQL finished before workflow invocation`;
- 1 runner error: terminal payload содержал `run_id`, не совпадающий с
  requested run;
- 18 безопасных schema abstention:
  12 `UNRESOLVED_ENTITIES` и 7 `JOIN_PATH_FAILED` decision reasons.

Spider заметно тяжелее BIRD по schema context и SQL complexity. Среднее время
schema linking выросло с 52,06 до 94,87 секунды, generation — со 122,55 до
160,58 секунды. При текущих stage caps это превращает большую часть набора в
timeout benchmark, а не в измерение качества SQL.

### Latency

| Метрика | Время |
|---|---:|
| Mean | 290,50 s |
| Median | 283,48 s |
| P90 | 450,22 s |
| P95 | 466,91 s |
| Max | 536,19 s |

Низкий max относительно BIRD не означает более быстрый pipeline: большинство
Spider-кейсов прекращается на раннем stage timeout и не доходит до verifier.

## Подтверждённые проблемы, которые остаются

### P0. Один deadline и реальная cancellation

Stage timeout останавливает ожидание, но синхронный model call в thread может
продолжить работу и удерживать worker slot. Локальные budgets не образуют один
абсолютный deadline.

Нужно:

- передавать единый `deadline_at` во все model/tool calls;
- ограничивать transport timeout как
  `min(stage_cap, remaining_run_budget)`;
- использовать cancellable request или отдельный завершаемый process;
- иметь одного владельца retry;
- после terminalization не оставлять `running` checkpoint и занятый slot.

Acceptance: hung fixture завершается не позже `deadline + 5 s`; после этого
нет живого model call, stale checkpoint и второго terminal outcome.

### P0. Исправить terminal reason projection

Сейчас finalizer подменяет первичную ошибку последним skipped/invalid output.
Из-за этого 239 failures двух прогонов названы `DB_AUDIT_OUTPUT_INVALID`.

Нужно хранить immutable primary failure:

`stage + attempt + failure_kind + source error + deadline state`.

Skipped downstream stages не могут заменить primary reason. HTTP result,
event stream, durable checkpoint и benchmark observation должны быть
projections одного envelope.

Acceptance: таблица fault-injection для timeout/invalid JSON/no bindings на
каждой стадии возвращает один и тот же reason во всех API surfaces.

### P0. Устранить workflow invocation и run_id race

Шесть Spider-кейсов завершились до workflow invocation, ещё один получил
чужой `run_id`. Это protocol correctness, а не модельное качество.

Нужно:

- создавать immutable canonical terminal envelope ровно один раз;
- связывать invocation, checkpoint и terminal по requested `run_id`;
- сделать повторную публикацию идемпотентной;
- не переиспользовать terminal state предыдущего run.

Acceptance: 1 000 последовательных и конкурентных runs дают ровно один
logical terminal каждый; ни один terminal не меняет `run_id`.

### P0. Ограничить и изолировать local vector-memory runtime

Первичный неполный BIRD-run был остановлен после 26 SIGSEGV в
`chromadb_rust_bindings`; persisted HNSW metadata содержал
`dimensionality=None`. Чистые изолированные stores обработали повторный
полный прогон без нового SIGSEGV. Диагностика сохранена отдельно.

Нужно не инициализировать vector memory для Text-to-SQL, когда она отключена
политикой, либо дать local store одного process-owner. Невалидную metadata
следует обнаруживать до входа в Rust loader и перестраивать из authoritative
records.

Acceptance: stress, restart и corrupted-metadata fixture дают 0 native
signals и не теряют authoritative records.

### P1. Уменьшить schema context и сделать typed linking contract

Текущий LLM получает слишком большой контекст Spider, а identity сущности
может потеряться между NLU, linking и generation.

Нужно:

- двухступенчатое deterministic retrieval: database catalog -> candidate
  tables/columns -> bounded LLM context;
- stable `source_entity_id` для каждого required physical binding;
- отдельные типы для physical binding, literal, predicate, derived metric и
  query control;
- authoritative FK graph и validated seed joins как часть typed output;
- cache использовать только как оптимизацию, а schema snapshot/version
  передавать через workflow state.

Acceptance: очистка cache не меняет результат; known connected graph не даёт
ложный `JOIN_PATH_FAILED`; required binding не исчезает при смене semantic
role.

### P1. Semantic AST coverage до дорогого verifier

Контекст вопроса уже передаётся verifier-у, но 99 BIRD SQL остаются
семантически неправильными.

Нужен deterministic gate, который сравнивает typed intent и SQL AST:

- обязательные tables/columns и bindings;
- predicates и literals;
- aggregation/grouping;
- ordering/limit/distinct;
- ожидаемую cardinality.

LLM verifier должен получать structured mismatch, а safety и semantic verdict
следует хранить отдельно.

Acceptance: известные negative regressions отклоняются до execution,
эквивалентные CTE/join rewrites не получают false reject; conditional EX
растёт без падения execution safety.

### P1. Provider-level structured output

Single-object recovery закрывает fenced JSON, но остаются 17 BIRD и 2 Spider
структурных сбоя generation/verifier/audit.

Нужно использовать constrained JSON schema на provider boundary и проверять
types/enums до возврата agent step. Второй объект, trailing conflicting text
и schema mismatch должны оставаться hard failure.

Acceptance: corpus fenced/trailing/malformed responses даёт стабильную
классификацию; валидный single object не теряется.

## Рекомендуемый порядок следующей итерации

1. Terminal reason projection и `run_id`/invocation race.
2. Настоящая cancellation и единый deadline.
3. Bounded schema retrieval для Spider.
4. Typed linking IR со stable entity IDs.
5. Semantic AST coverage gate.
6. Provider-level structured output.
7. Повторить сначала BIRD 500, затем тот же Spider SQLite subset 135 на
   неизменном протоколе.

Следующий benchmark gate:

- 0 runner/transport failures;
- 0 `finished before workflow invocation`;
- 0 reason, указывающих на skipped downstream stage;
- BIRD execution coverage выше 59,80% без снижения conditional EX ниже
  65,22%;
- Spider execution coverage существенно выше 1,48%; пока coverage меньше
  50%, semantic accuracy Spider интерпретировать отдельно;
- официальный EX обоих наборов не ниже текущего baseline.

## Артефакты

### BIRD

- [manifest.json](/srv/git_projects/MultiAgent/test_output/text2sql-benchmarks/2026-07-29/bird-mini-dev-sqlite-after-fix/manifest.json)
- [observations.jsonl](/srv/git_projects/MultiAgent/test_output/text2sql-benchmarks/2026-07-29/bird-mini-dev-sqlite-after-fix/observations.jsonl)
- [official_eval.log](/srv/git_projects/MultiAgent/test_output/text2sql-benchmarks/2026-07-29/bird-mini-dev-sqlite-after-fix/official_eval.log)
- [summary.json](/srv/git_projects/MultiAgent/test_output/text2sql-benchmarks/2026-07-29/bird-mini-dev-sqlite-after-fix/analysis/summary.json)
- [diagnostics.jsonl](/srv/git_projects/MultiAgent/test_output/text2sql-benchmarks/2026-07-29/bird-mini-dev-sqlite-after-fix/analysis/diagnostics.jsonl)
- [runtime_evidence](/srv/git_projects/MultiAgent/test_output/text2sql-benchmarks/2026-07-29/bird-mini-dev-sqlite-after-fix/runtime_evidence)

### Spider

- [manifest.json](/srv/git_projects/MultiAgent/test_output/text2sql-benchmarks/2026-07-29/spider2-lite-sqlite-after-fix/manifest.json)
- [observations.jsonl](/srv/git_projects/MultiAgent/test_output/text2sql-benchmarks/2026-07-29/spider2-lite-sqlite-after-fix/observations.jsonl)
- [official_eval.log](/srv/git_projects/MultiAgent/test_output/text2sql-benchmarks/2026-07-29/spider2-lite-sqlite-after-fix/official_eval.log)
- [summary.json](/srv/git_projects/MultiAgent/test_output/text2sql-benchmarks/2026-07-29/spider2-lite-sqlite-after-fix/analysis/summary.json)
- [diagnostics.jsonl](/srv/git_projects/MultiAgent/test_output/text2sql-benchmarks/2026-07-29/spider2-lite-sqlite-after-fix/analysis/diagnostics.jsonl)
- [predictions](/srv/git_projects/MultiAgent/test_output/text2sql-benchmarks/2026-07-29/spider2-lite-sqlite-after-fix/spider_predictions)
- [runtime_evidence](/srv/git_projects/MultiAgent/test_output/text2sql-benchmarks/2026-07-29/spider2-lite-sqlite-after-fix/runtime_evidence)

### Исключённые диагностические прогоны

- [BIRD before-fix и Chroma incident](/srv/git_projects/MultiAgent/test_output/text2sql-benchmarks/2026-07-29/bird-mini-dev-sqlite-before-fix)
- [BIRD probes](/srv/git_projects/MultiAgent/test_output/text2sql-benchmarks/2026-07-29/bird-mini-dev-sqlite-after-fix)

## Ограничения интерпретации

- Первичный before-fix BIRD run содержит только 196 уникальных observations и
  был прерван; он не является сопоставимым full-denominator baseline.
- Execution equivalence не доказывает единственность или переносимость SQL.
- BIRD и Spider используют разные официальные execution comparators.
- Model routes недетерминированы при temperature `0.7`; gateway не экспортирует
  resolved downstream model, seed и per-stage token/call metadata.
- Sharding безопасен для score при отключённой SQL-memory и независимых
  ordinal, но latency нельзя трактовать как single-server production SLO.
