# Каталог ручек настройки Text-to-SQL

«Ручка настройки» (tuning knob) — параметр, который оператор может поменять
в конфиге / переменной окружения (или, если явно помечено, только правкой
кода), чтобы повлиять на качество, стоимость или скорость пайплайна
text-to-sql — **без изменения логики**. Документ фиксирует: где лежит
ручка, какое у неё значение сейчас, какой диапазон допустим, на что она
влияет и как проверить эффект.

Если вы меняете параметры бюджета исследования (research) или солвера —
после изменений прогоняйте протокол бенчмарка
`docs/operations/text2sql-benchmark-protocol.md`, чтобы отличить реальное
улучшение от подгонки под конкретные кейсы. Протокол прямо запрещает вот
что (пункт 5):

> 5. Запрещены условия, подсказки, словари и обработчики, зависящие от
>    номера задачи, текста вопроса, имени базы или другого признака
>    конкретного benchmark-кейса.

То есть ручки из этого каталога — это настройки уровня пайплайна
(модель, лимит токенов, таймаут), а не «если вопрос такой-то — сделай
так». Специфичные под один кейс костыли сюда не добавляем.

Формат таблиц: **переменная/константа → где → значение по умолчанию →
что делает → риск/что измерить → откат**.

---

## Оглавление

1. [Модели по шагам](#1-модели-по-шагам)
2. [Параметр `stop_review_model`](#2-параметр-stop_review_model)
3. [Бюджет модельных вызовов](#3-бюджет-модельных-вызовов)
4. [Остальные ручки адаптивной политики](#4-остальные-ручки-адаптивной-политики)
5. [Лимиты токенов по шагам](#5-лимиты-токенов-по-шагам)
6. [LLM safety audit](#6-llm-safety-audit-советующий-аудит-sql-перед-выполнением)
7. [DSN-профиль](#7-dsn-профиль-w1-12)
8. [Глоссарий DSN](#8-глоссарий-dsn-w2-22)
8а. [Редактор метаданных (UI)](#8а-редактор-метаданных-ui)
9. [Память успешных SQL](#9-память-успешных-sql-w2-23)
10. [RRF в каталоге схемы](#10-rrf-в-каталоге-схемы-w3-31)
11. [Подсказка код↔метка](#11-подсказка-кодметка-w3-32)
12. [Provenance (след выполненного запроса)](#12-provenance-след-выполненного-запроса-w4-41)
13. [Уточняющий вопрос](#13-уточняющий-вопрос-w4-42)
14. [Гейт docs↔schema](#14-гейт-docsschema-w4-43)
15. [Диагностика / наблюдаемость](#15-диагностика--наблюдаемость)
16. [Известные ограничения и планы](#16-известные-ограничения-и-планы)
17. [Итог: сколько ручек в каталоге](#17-итог-сколько-ручек-в-каталоге)

---

## 1. Модели по шагам

### 1.1 Реестр классов моделей (`agent_command.py::_MODEL_CONFIGS`)

Каждый alias — это класс модели (уровень «дёшево/быстро» vs «дорого/точно»),
привязанный к конкретному gateway-алиасу (`model_id`). Чтобы поменять модель
целого класса — правится это место в коде (это не yaml, но одна правка сразу
меняет модель во всех шагах, которые используют этот alias).

| Alias | `model_id` (gateway-алиас) | `max_tokens` | `temperature` |
|---|---|---|---|
| `model_search` | `llmgateway/qwen3.5` | 32768 | — |
| `model_lite` | `llmgateway/light_model` | 32768 | — |
| `model_code` | `llmgateway/qwen3.5` | 32768 | 0.7 |
| `model_hard` | `llmgateway/high` | 32768 | 0.7 |
| `model_summary` | `llmgateway/light_model` | 32768 | 0.2 |
| `model_big` | `llmgateway/big_context` | 32768 | — |
| `model_vision` | `llmgateway/high` | 32768 | 0.4 |
| `model_reranker` | `llmgateway/light_model` | 32768 | 0.2 |
| `model_ultimate` | `llmgateway/high` | 65536 | 0.6 |

**Как измерить эффект:** артефакт бенчмарка `model_calls_tokens_cost_receipts`
(см. раздел 3) — поле `calls[].model_identity`; плюс качество ответа
(корректность SQL) из отчёта бенчмарка.

**Когда крутить:** если шаг систематически ошибается на сложных
формулировках — поднять класс модели (например, с `model_code` на
`model_hard`/`model_ultimate`); если шаг стабильно точен, но дорог/медленный
— понизить класс (`model_lite`).

### 1.2 Какой класс модели использует каждый шаг

11 ключей `step_models` в `config/text_to_sql/llm_models.yaml` — это полный
список шагов пайплайна, у которых класс модели вынесен в конфиг. Читает их
`custom_tools/text_to_sql/llm_models_config.py::step_model_name(step, *,
explicit_profile=None)` — берёт активный профиль (аргумент → env
`TEXT_TO_SQL_LLM_MODELS_PROFILE` → `"default"`) и возвращает
`step_models.<step>` как строку-alias; резолвинг alias → реальный объект
модели происходит уже на стороне вызывающего кода через
`agent_command.model_mapping`.

| Шаг пайплайна | Где задан | Текущая модель (`default`) | Как поменять |
|---|---|---|---|
| Schema research (исследование схемы) | `agent_profiles/schema_research_agent.yaml` (ключ `model`) | `model_code` | Правка yaml-ключа `model` (допустимые значения — любой alias из §1.1). `step_models.schema_research` в `llm_models.yaml` — зеркало этого значения (контракт-тест `tests/test_text_to_sql_step_models_registry.py`), не источник истины |
| SQL solver (генерация/починка SQL в адаптивном цикле) | `agent_profiles/sql_solver_agent.yaml` (ключ `model`) | `model_code` | Правка yaml-ключа `model`. `step_models.sql_solver` в `llm_models.yaml` — зеркало (тот же контракт-тест), не источник истины |
| Result review (проверка результата запроса) | `custom_tools/text_to_sql/adaptive/result_review_runtime.py::build_result_review_runtime` — вызов `create_text_to_sql_model(step_model_name("result_review"), ...)` | `model_hard` | Ключ `step_models.result_review` в `config/text_to_sql/llm_models.yaml` |
| Safety LLM audit (советующий, не блокирующий аудит SQL) | `custom_tools/text_to_sql/core/_sql_generation_api.py::_run_llm_safety_audit` — `call_openai_api(..., model=model_mapping[step_model_name("safety_llm_audit")])` | `model_code` | Ключ `step_models.safety_llm_audit` |
| NLU query understanding (адаптивный разбор запроса) | `custom_tools/text_to_sql/nlu.py::NLUProcessor._understand_query`, первый вызов `call_openai_api(...)` | `model_code` | Ключ `step_models.nlu_query_understanding` |
| NLU completeness (проверка полноты разбора) | `custom_tools/text_to_sql/nlu.py::NLUProcessor._understand_query`, второй вызов `call_openai_api(...)` | `model_code` | Ключ `step_models.nlu_completeness` |
| NLU intent (извлечение intent/сущностей, legacy-путь) | `custom_tools/text_to_sql/nlu.py::NLUProcessor.extract_intent` | `model_code` | Ключ `step_models.nlu_intent` |
| Schema enricher (генерация описаний колонок) | `custom_tools/text_to_sql/schema_enricher.py::SchemaEnricher.enrich_descriptions_with_llm` | `model_code` | Ключ `step_models.schema_enricher` |
| Schema linking (LLM-связывание сущностей со схемой) | `custom_tools/text_to_sql/schema_linking/llm_linker.py::LLMLinker.llm_linking` — вызов через DI `active_llm_caller()`; caller по умолчанию — `utils.call_openai_api` (внедряется в `custom_tools/text_to_sql/schema_linker.py::SchemaLinker.with_defaults`, `_default_llm_caller`, без обёртки, режущей kwargs) | `model_code` | Ключ `step_models.schema_linking` |
| SQL generation (генерация SQL из linked entities) | `custom_tools/text_to_sql/sql_generator.py::SqlGenerator._llm_generation_direct` (модель читает `_sql_generation_model`) | `model_code` | Ключ `step_models.sql_generation` |

С W1-1.1 все 11 шагов из этой таблицы — конфиг-ручки, правка кода для смены
модели не нужна (кроме двух зеркал `schema_research`/`sql_solver`, где
источник истины — agent-профиль, см. выше).

Дефолт `call_openai_api` (`utils.py`, `if not model: model = model_code`) в
text-to-sql шагах больше не участвует — все 9 не-зеркальных шагов теперь
передают `model=` явно через `step_models`. Этот дефолт остаётся только для
прочих потребителей `call_openai_api` вне text-to-sql пайплайна.

**Эффект «дороже/точнее» vs «дешевле/быстрее»:** повышение класса (например,
`model_code` → `model_hard`/`model_ultimate`) обычно увеличивает точность и
задержку/стоимость вызова; понижение (→ `model_lite`) — наоборот.

**Как измерить:** отчёт бенчмарка (корректность SQL по шагам) + артефакт
`model_calls_tokens_cost_receipts` (раздел 3) для стоимости в токенах.

### 1.3 Профиль `experiment` (W5, training-free подбор моделей по шагам)

`config/text_to_sql/llm_models.yaml` теперь содержит **два** профиля:
`default` и `experiment`. Профиль `experiment` отличается
от `default` **только** секцией `step_models` — `schema_linking`,
`sql_generation`, `nlu` там те же значения, что и в `default`, чтобы разница
в поведении объяснялась исключительно классом модели по шагу, а не заодно
изменившимся лимитом токенов.

Активируется явно: `TEXT_TO_SQL_LLM_MODELS_PROFILE=experiment`. По умолчанию
и прод, и бенчмарк используют `"default"` — это форсирует
`canonical_runtime_environment` (`custom_tools/text_to_sql/eval/release_inputs.py`),
которая пишет `TEXT_TO_SQL_LLM_MODELS_PROFILE=default` в канонический
набор переменных окружения релизного прогона независимо от того, что
выставлено в окружении оператора.

| Шаг | Текущая модель (`default`) | В `experiment` | Почему можно понижать | Риск | Что измерить | Откат |
|---|---|---|---|---|---|---|
| `schema_research` | `model_code` | не меняем | зеркало agent-профиля, не входит в критерий ниже | — | — | — |
| `sql_solver` | `model_code` | не меняем | зеркало agent-профиля, не входит в критерий ниже | — | — | — |
| `research_stop_review` | `model_code` | `model_lite` | JSON-ответ без строгой JSON-schema-валидации, деградация fail-open (просто хуже решение «хватит ли данных») | ранняя/поздняя остановка research | доля итераций research + доля `BUDGET_EXHAUSTED`/`STAGNATED` в исходах (experiment vs default) | `TEXT_TO_SQL_LLM_MODELS_PROFILE=default` |
| `result_review` | `model_hard` | `model_lite` | ответ дешевле генерации (короткий verdict, не SQL); провал сверки не блокирует финализацию — просто помечает кандидат "contradicted"/"malformed" | больше ложных `contradicted`/`malformed` | доля таких verdict в отчёте бенчмарка | `TEXT_TO_SQL_LLM_MODELS_PROFILE=default` |
| `safety_llm_audit` | `model_code` | `model_lite` | документирован как advisory (не блокирующий); статический слой безопасности обязателен и не зависит от этого шага | пропуск advisory-находок LLM-аудита | доля `advisory_issues` с `issue_type` `LLM_AUDIT_*` + ручная сверка выборки SQL | `TEXT_TO_SQL_LLM_MODELS_PROFILE=default` |
| `nlu_query_understanding` | `model_code` | не меняем | ошибка не перехватывается (не fail-open) | — | — | — |
| `nlu_completeness` | `model_code` | не меняем | тот же вызов, что выше | — | — | — |
| `nlu_intent` | `model_code` | не меняем | эвристический fallback (`custom_tools/text_to_sql/nlu.py::_fallback_extract_intent`) — opt-in только при `TEXT_TO_SQL_NLU_ALLOW_FALLBACKS=1`; по умолчанию (`0`) ошибка LLM здесь поднимает `RuntimeError`, а не деградирует изящно | — | — | — |
| `schema_enricher` | `model_code` | не меняем | генерация контента (описания колонок), ошибка не деградирует изящно | — | — | — |
| `schema_linking` | `model_code` | не меняем | генерация контента (связывание сущностей) | — | — | — |
| `sql_generation` | `model_code` | не меняем | генерация контента (сам SQL) | — | — | — |

**Guard при смене модели между инкарнациями (crash/resume).** Ledger-резервация
модельного вызова (`reserve_model_call_budget`,
`custom_tools/text_to_sql/adaptive/_policy_model_budget.py`) неизменяема: если
между падением и возобновлением того же `call_id` оператор поменял
`llm_models.yaml`/`adaptive.yaml` так, что `model_identity` или
`policy_digest` резервации разошлись с уже записанной — резервация не
переиспользуется, поднимается `BudgetConflictError` с текстом «model call
reservation's policy changed between incarnations… resume impossible, start
a new run», и в лог пишется `WARNING`. То же самое (только `WARNING` без
исключения — консервативный отказ доиграть недосогласованный вызов, а не
падение) логирует `settle_incomplete_reentry_model_call`
(`workflow/_text_to_sql_solver_reentry.py`) для незавершённой (`STARTED`, но
не `COMPLETED`) резервации research-модели на возобновлении run'а. Вывод:
менять `TEXT_TO_SQL_LLM_MODELS_PROFILE`/`llm_models.yaml` посреди активного
run'а небезопасно — доиграть его до конца после этого нельзя, нужен новый
run.

Крах процесса в любом из двух окон durable-записи собственного модельного
вызова SQL-solver'а (`solver-generate-*`) — «бронь без `STARTED`» или «`STARTED`
без `RESULT`» (это три отдельные sqlite-транзакции) — не блокирует resume:
`_resume_open_generation`
(`workflow/text_to_sql_adaptive_solver.py`) принудительно закрывает зависшую
бронь через `settle_incomplete_solver_model_call`
(`workflow/_text_to_sql_solver_reentry.py`) — симметрично research-вызовам —
и только потом solver делает новую попытку; провайдер вызывается один раз.
Зависшая бронь закрывается с неизвестным usage (консервативный заряд по
максимуму брони). Те же два окна закрывает `settle_incomplete_reentry_model_call`
для research-вызовов re-entry.

**Детектор пустого ответа провайдера.** И schema-research/stop-review
(`workflow/text_to_sql_typed_research.py::_typed_schema_model`), и SQL-solver
(`workflow/text_to_sql_adaptive_solver.py::_production_solver_model`) при
пустом ответе модели (пустая строка/пробелы — предикат `_is_blank_text`)
делают **один** повторный вызов того же провайдера. Общий код — в
`custom_tools/text_to_sql/adaptive/_empty_response_retry.py`
(`retry_once_on_empty_response`, `sum_model_token_usage`). Повтор идёт
**внутри той же** ledger-резервации (одна бронь = один модельный вызов с
не более чем одним внутренним повтором провайдера), новой брони не создаётся.
Usage обеих попыток **суммируется**, но результат **клампится** (обрезается
сверху) к потолку брони `model_budget.input_tokens_per_call` /
`output_tokens_per_call`: иначе сумма могла бы превысить
`reservation.maximum_*_tokens`, а `model_charge` при этом бросает
`ModelUsageBudgetError` уже после durable-записи фазы `result` — бронь
навсегда падала бы при согласовании и на resume («poison-pill»). Кламп
логируется `WARNING` (текст: «usage summed across N provider attempts
exceeded the reservation cap; clamped»); осознанный
компромисс — недоучёт впустую потраченной пустой попытки вместо
невосстановимого run'а. Оба продовых вызова (`run_typed_schema_research`
и re-entry в `workflow/_text_to_sql_solver_reentry.py`) передают потолок
через `input_tokens=`; если вызвать обёртку без него, кламп невозможен и
берётся usage только успешной попытки. Проверка «дедлайн run'а уже истёк
→ повтор не делать» есть только в обёртке SQL-solver; schema-research
обёртка повторяет безусловно (лимит времени там держит сам research
loop). Если повтор тоже пуст —
`ValueError`, шаг завершается ошибкой. Не ловится: ответ из невидимых
символов (zero-width) или из одного тега `<think>…</think>` — он не «пустой»
для предиката.

Отдельно, `result_review_runtime.py` (вызов `review()`) передаёт в
`call_openai_api` `max_retries=1` — там это встроенный retry самого
`call_openai_api` (этот шаг не на ledger-бюджете). Побочный эффект:
`call_openai_api` повторяет по ключевым словам ошибок, включая `400`/
`bad request`, так что один лишний повтор возможен и для непроходной 4xx.
`utils.py::call_openai_api` в любом случае логирует `WARNING` перед тем,
как вернуть пустую строку вызывающему коду после исчерпания всех попыток.

**Флаг бенчмарка.** `scripts/text2sql_public_benchmark.py --llm-models-profile
<имя>` пробрасывает профиль в канонический набор переменных окружения
релизного прогона (`release_inputs.py::canonical_runtime_environment`)
вместо форсированного `default`. Имя валидируется рано: неизвестный профиль
→ `ValueError("unknown llm models profile: …")` при сборке release lock,
пустая строка эквивалентна отсутствию флага. Профиль входит в identity
окружения, поэтому lock-файлы старых релизных прогонов (до W5) не совпадают
с новым набором канонических ключей и падают с «environment identity
mismatch» — сравнение с ними невозможно, нужен полный перезапуск.

---

## 2. Параметр `stop_review_model` (отдельная модель для решения «прекратить исследование»)

`workflow/text_to_sql_typed_research.py::_research_stop_review_model(profile_model,
output_tokens, run_id)` — провайдер под тот же JSON-schema маршрут, что и
research, но с отдельным alias модели. Оба вызывающих места передают
`step_model_name("research_stop_review")` из `config/text_to_sql/llm_models.yaml`
(секция `step_models`), а не `profile.model` напрямую:

- `run_typed_schema_research` (основной путь запуска research);
- `_continue_production_research` в `workflow/_text_to_sql_solver_reentry.py`
  (re-entry путь, используется при таргетном/семантическом повторном
  исследовании).

Чтобы подставить более дешёвую модель (например, `model_lite`) под
stop-review — правится только yaml-ключ `step_models.research_stop_review`,
без изменения кода (см. также §1.3 — это ровно то, что делает профиль
`experiment`). По умолчанию значение то же, что и у research (`model_code`).

Важно: ledger-идентичность вызова (`stable_schema_research_model_identity`,
которую `_ResearchLoopCoordinator.__init__`
(`custom_tools/text_to_sql/adaptive/research_loop.py`) сохраняет как
`self._model_identity`) строится из `profile.model` напрямую и НЕ зависит от
значения `step_models.research_stop_review` — смена stop-review модели не
путает бюджетный/replay-учёт, привязанный к identity основной research-модели.

**На что влияет:** stop-review — это решение «хватит ли данных для ответа»
внутри цикла исследования; более дешёвая модель здесь может ускорить/удешевить
исследование ценой риска менее точной остановки (слишком рано или слишком
поздно).

**Как измерить:** число итераций research в артефактах прогона + доля
`BUDGET_EXHAUSTED` в исходах.

---

## 3. Бюджет модельных вызовов (`config/text_to_sql/adaptive.yaml`, секция `model_budget`)

```yaml
model_budget:
  model_calls: 256
  input_tokens_per_call: 32768
  output_tokens_per_call: 32000
  total_tokens: 1048576
```

и связанный с ним `resource_limits.model_tokens: 1048576`.

### Жёсткая связка (валидатор)

`custom_tools/text_to_sql/adaptive/_policy_config.py` требует равенства:

```python
if self.resource_limits.model_tokens != self.model_budget.total_tokens:
    raise ValueError("resource_limits.model_tokens must equal model_budget.total_tokens")
```

и рядом — второе такое же жёсткое требование:

```python
if self.operation_counts.model_decisions != self.model_budget.model_calls:
    raise ValueError("operation_counts.model_decisions must equal model_budget.model_calls")
```

**Значит:** `resource_limits.model_tokens` и `model_budget.total_tokens`
всегда правятся вместе (одно и то же число), так же как
`operation_counts.model_decisions` и `model_budget.model_calls`.

### ВАЖНО: все текущие значения уже на потолке, зашитом в коде

`custom_tools/text_to_sql/adaptive/_policy_common.py` задаёт константы
`MAX_*`, которые ограничивают, насколько высоко можно поднять любое из
значений `adaptive.yaml` (иначе `_policy_config.py` бросает
«cannot widen the policy safety envelope»). Сверка показала: **каждое**
значение в `config/text_to_sql/adaptive.yaml` сейчас **в точности равно**
своему потолку:

| Ключ в `adaptive.yaml` | Текущее значение | `MAX_*` в `_policy_common.py` |
|---|---|---|
| `model_budget.model_calls` | 256 | `MAX_MODEL_CALLS_V2 = 256` |
| `model_budget.input_tokens_per_call` | 32768 | `MAX_MODEL_INPUT_TOKENS_PER_CALL = 32768` |
| `model_budget.output_tokens_per_call` | 32000 | `MAX_MODEL_OUTPUT_TOKENS_PER_CALL = 32000` |
| `model_budget.total_tokens` / `resource_limits.model_tokens` | 1048576 | `MAX_MODEL_TOTAL_TOKENS = 1048576` |

**Практический вывод:** просто поднять `total_tokens` в yaml выше
1048576 (или любое из трёх других значений) **не получится** — конфиг не
пройдёт валидацию. Чтобы реально расширить бюджет модельных вызовов, нужно
**сначала** поднять соответствующую `MAX_*`-константу в
`custom_tools/text_to_sql/adaptive/_policy_common.py` (это правка кода), и
только потом — значение в `adaptive.yaml`. Уменьшать (сокращать бюджет)
можно уже сейчас, правкой одного yaml.

### Консервативное списание токенов (когда провайдер не вернул usage)

Провайдер не всегда возвращает реальный расход токенов (usage) с ответом.
Логика списания — `custom_tools/text_to_sql/adaptive/model_budget.py::model_charge`:

```python
input_tokens = (
    reservation.maximum_input_tokens
    if usage.input_tokens is None
    else usage.input_tokens
)
output_tokens = (
    reservation.maximum_output_tokens
    if usage.output_tokens is None
    else usage.output_tokens
)
```

Если `usage.input_tokens`/`usage.output_tokens` не пришли — списывается
**максимум по резервации** (то есть `input_tokens_per_call` +
`output_tokens_per_call`), а не фактический расход. Это отражается флагом
`usage_was_conservative=True` в `ModelCallReconciliation`.

**SQL-солвер теперь тоже отчитывается реальным usage, когда его знает
провайдер (изменилось в этой ветке).** Раньше
`SqlSolverProposalAdapter.propose` (`custom_tools/text_to_sql/adaptive/sql_solver_agent.py`)
всегда получал от модели голую строку и списывался консервативно на каждый
вызов. Теперь у адаптера есть `propose_with_usage`: если модель-провайдер
(`_production_solver_model`, `workflow/text_to_sql_adaptive_solver.py`)
возвращает `SqlSolverModelResponse(raw_response, usage)` с реальным
`ModelTokenUsage` (через `_provider_model_usage`, тот же хелпер, что и у
research), это реальное значение уходит в `execute_model_call_with_budget_async`
и списывается по факту. Консервативное списание (`ModelTokenUsage(input_tokens=None,
output_tokens=None)`) остаётся только запасным путём — если провайдер сам не
возвращает usage, либо если модель подключена «сырым» коллбэком без обёртки
в `SqlSolverModelResponse` (например, в тестах).

**Research**, как и раньше, отчитывается реальным usage через
`_provider_model_usage` (`workflow/text_to_sql_typed_research.py`) — читает
`token_usage`/`usage`/`raw.usage` из ответа модели и берёт
`prompt_tokens`/`completion_tokens`, если они положительные целые.

### Где смотреть фактический расход

Бенчмарк-артефакт `model_calls_tokens_cost_receipts`
(`custom_tools/text_to_sql/eval/public_benchmark_bwrap.py::_model_calls_tokens_cost_receipts_evidence`)
даёт:

- `by_step` — расход, сгруппированный по шагу (имя шага получается из
  `call_id` отрезанием суффикса `-<revision>-<attempt>`, например
  `"research-stop-review-2-3"` → `"research-stop-review"`);
- `totals` — суммарно по всему прогону;
- в обоих — поля `call_count`, `reconciled_call_count`,
  `conservative_call_count` (сколько вызовов списаны консервативно),
  `input_tokens`/`output_tokens` (реальные, если известны),
  `charged_input_tokens`/`charged_output_tokens`/`charged_total_tokens`
  (что реально списано с бюджета);
- `calls[]` — по каждому вызову: `usage_was_conservative` (True/False/None,
  где None = ещё не сверен).

**Когда крутить:** если в артефактах прогона видите `BUDGET_EXHAUSTED`
(причину остановки research или отказ солвера) — это симптом того, что
бюджета не хватило. Сначала посмотрите `conservative_call_count` в
`model_calls_tokens_cost_receipts`: если он большой, бюджет тратится
консервативными списаниями, а не реальным расходом — в этом случае может
быть достаточно **уменьшить** число обращений (или ускорить сходимость), а
не поднимать сам бюджет. Если же и после этого бюджета не хватает —
поднимайте `total_tokens` и `resource_limits.model_tokens` синхронно, но
помните про потолок `MAX_*` из раздела выше (без правки кода дальше
1048576 не уйти).

### Усечение контекста солвера, если промпт всё равно не влезает в лимит

И солвер (`workflow/text_to_sql_adaptive_solver.py::_solver_context`), и
ревью результата (`custom_tools/text_to_sql/adaptive/result_review_runtime.py`)
переводят лимит токенов в лимит байт по одной и той же грубой оценке
«байт ≈ токен × 4» (токен — единица, которой провайдер считает длину
текста; здесь размер прикидывается по байтам, не токенизируя текст
по-настоящему; для кириллицы, где один символ часто занимает больше 1
байта в UTF-8, эта оценка может как переоценивать, так и недооценивать
реальное число токенов — стоит сверить на реальном трафике). Это одна
именованная константа `APPROXIMATE_BYTES_PER_TOKEN = 4` в
`custom_tools/text_to_sql/adaptive/model_budget.py` — оба потребителя
берут её оттуда.

Если собранный контекст солвера всё равно не помещается в
`input_tokens_per_call × APPROXIMATE_BYTES_PER_TOKEN` байт,
`_solver_context` не падает сразу с `ValueError`, а по очереди применяет
усечения (каждое — на копии данных, ничего не мутирует на месте),
пересчитывая размер после каждого шага и останавливаясь, как только
влезло. Порядок шагов (в этой ветке добавлен новый первый шаг — сброс
похожих примеров из памяти успешных SQL, см. раздел 9):

0. `_drop_similar_examples` — первым делом выбрасывает
   `similar_successful_sql_examples` (подсказки из памяти успешных SQL
   прошлых запусков, раздел 9) целиком: это наименее авторитетный
   источник в контексте — просто retrieval-подсказка, а не часть
   доказательной базы текущего запуска.
1. `_truncate_document_content` — обрезает `content` каждого доверенного
   документа до `DOC_CONTENT_CHAR_CAP` символов (= 8000, константа рядом с
   `_solver_context`), добавляя пометку `"…[truncated N chars]"`.
2. `_limit_document_count` — оставляет документы по порядку `document_id`,
   пока помещается (тот же приём «пробуем добавить — если влезло,
   оставляем, если нет — пробуем следующий», что и в
   `SchemaLimiter.build_schema_summary`,
   `custom_tools/text_to_sql/validators/schema_limiter.py`).
3. `_truncate_solver_history` — оставляет только последние `HISTORY_KEEP`
   (= 2) SQL-кандидата и привязанные к ним `check_results`/`execution_results`,
   а также последние `HISTORY_KEEP` записей
   `action_history`/`missing_evidence_requests`.
4. `_truncate_check_diagnostics` — обрезает свободный текст
   `observed_error`/`required_change` до `DIAG_CHAR_CAP` символов (= 500), но
   только у проверок, которые относятся не к последнему (самому свежему), а
   к более старым SQL-кандидатам.

Эти константы (`DOC_CONTENT_CHAR_CAP`, `HISTORY_KEEP`, `DIAG_CHAR_CAP`)
намеренно не вынесены в `adaptive.yaml`: все ключи yaml из этого раздела —
это *потолки* поверх констант `MAX_*` из `_policy_common.py`, а эти
константы не про потолок бюджета, а про то, *как* обрезать промпт, когда
он уже превысил лимит, — своего yaml-ключа для этого нет и не планируется.

Неусекаемое «ядро», которое не трогает ни один шаг: `coverage_requirements`
целиком, скалярные поля `solver_state` (`query_spec`, `revision`,
`schema_namespace_version` и т.д.), `deterministic_sql_repair_receipt` и
`sql_parse_feedback` целиком, а также последний SQL-кандидат и его
`check_results`/`execution_results` (переживают шаг 3, потому что
`HISTORY_KEEP = 2 ≥ 1` — самый свежий кандидат всегда попадает в
«последние `HISTORY_KEEP`»).

Итоговый payload всегда содержит поле `context_truncation` — даже если
усечение не понадобилось (тогда `applied_steps: []`): `applied_steps` —
какие шаги сработали, `original_bytes` — размер контекста до усечения,
`final_bytes` — фактический размер того, что уходит модели (включая само
поле `context_truncation`). Если контекст не помещается даже после всех
пяти шагов, `_solver_context` всё равно бросает `ValueError`, но с
текстом: `"adaptive solver context exceeds configured model input after
truncation"`.

**Страховка от расхождения политики между инкарнациями (`policy_digest`).**
Каждая резервация модельного вызова (раздел 1.3) и проб-бюджета несёт
`policy_digest` — канонический хеш всей конфигурации политики
(`canonical_digest(checked_config)`). Он используется не только для гейта
«модель поменялась между инкарнациями» (§1.3), но и как более общая
проверка: если *любая* часть `adaptive.yaml`/`llm_models.yaml` изменилась
между сохранением резервации и попыткой её повторно использовать при
возобновлении run'а, `policy_digest` разойдётся и резервация будет
отклонена вместо того, чтобы молча продолжить работу под другой политикой.

---

## 4. Остальные ручки адаптивной политики (`config/text_to_sql/adaptive.yaml`)

Полное содержимое файла (7 верхнеуровневых ключей, 14 листовых значений;
13 из них имеют потолок `MAX_*`, у `policy_version` его нет):

| Ключ | Текущее значение | На что влияет (по `_policy_config.py` / `_policy_common.py` / `research_loop.py`) |
|---|---|---|
| `policy_version` | `2` | Версия схемы политики. `1` отключает `model_budget` (принудительно `model_tokens=0`); `2` — текущий формат с `model_budget`. Менять только вместе с остальной секцией. |
| `wall_clock.wall_clock_seconds` | `14400` (=4 ч) | Общий дедлайн на весь adaptive-прогон (research + solver): `deadline_ms = wall_clock_seconds * 1000`. При исчерпании — прогон останавливается по времени. |
| `resource_limits.model_tokens` | `1048576` | Общий лимит токенов на все модельные вызовы за прогон; должен быть равен `model_budget.total_tokens` (см. раздел 3). |
| `resource_limits.db_probe_ms` | `14400000` (=14400 c) | Суммарное время (мс), которое можно потратить на DB-пробы (запросы к базе в ходе исследования схемы). Равно `wall_clock_seconds * 1000` в текущем конфиге. |
| `operation_counts.actions` | `512` | Максимальное число действий агента (шагов research loop) за прогон. |
| `operation_counts.model_decisions` | `256` | Максимальное число обращений к модели за решениями в research loop; должно быть равно `model_budget.model_calls` (см. раздел 3). |
| `operation_counts.db_probes` | `384` | Максимальное количество отдельных DB-проб (число обращений к БД, а не суммарное время — в отличие от `db_probe_ms`). |
| `result_volume.returned_rows` | `5000` | Максимум строк, которые в сумме можно вернуть исследованию за весь прогон. |
| `result_volume.inline_bytes` | `2097152` (=2 МиБ) | Максимум байт полезной нагрузки (данные, встроенные inline в результат), возвращённой за прогон. |
| `per_action.sample_rows` | `50` | Максимум строк, которые можно получить за одно-единственное действие (одну DB-пробу). |
| `model_budget.model_calls` | `256` | См. раздел 3. |
| `model_budget.input_tokens_per_call` | `32768` | См. раздел 3. |
| `model_budget.output_tokens_per_call` | `32000` | См. раздел 3. |
| `model_budget.total_tokens` | `1048576` | См. раздел 3. |

Как и в разделе 3: **все 13 значений сейчас равны своим `MAX_*`-потолкам** в
`_policy_common.py` (`MAX_WALL_CLOCK_SECONDS=14400`, `MAX_DB_PROBE_MS`
(=`MAX_WALL_CLOCK_SECONDS*1000`), `MAX_ACTIONS=512`,
`MAX_MODEL_DECISIONS=256`, `MAX_DB_PROBES=384`, `MAX_RETURNED_ROWS=5000`,
`MAX_INLINE_BYTES=2*1024*1024`, `MAX_SAMPLE_ROWS=50`, плюс четвёрка
`model_budget` из раздела 3). Поднять любой из них выше текущего — сначала
код (`_policy_common.py`), потом yaml. Понижать можно сразу в yaml.

**Когда крутить:** симптом «прогон упирается в лимит времени/действий/DB-проб
раньше, чем находит ответ» — смотреть, какой конкретно лимит исчерпался
(`ResearchStopReason.BUDGET_EXHAUSTED` в артефактах прогона/логах research
loop), и целенаправленно поднимать именно его (с учётом потолка выше).

---

## 5. Лимиты токенов по шагам (`config/text_to_sql/llm_models.yaml`)

Файл содержит 2 профиля — `default` и (с W5) `experiment`. Этот
раздел (`schema_linking`/`sql_generation`/`nlu`) у обоих профилей
**идентичен** — `experiment` от `default` отличается только секцией
`step_models` (раздел 1.3).

| Ключ | Где читается | Значение (оба профиля) | На что влияет |
|---|---|---|---|
| `schema_linking.max_tokens` | `custom_tools/text_to_sql/schema_linking/llm_linker.py::LLMLinker.llm_linking` | `20000` | Лимит `max_tokens` LLM-вызова, который связывает сущности запроса со схемой БД. |
| `schema_linking.schema_prompt_hard_max_chars` | там же; проверяется в `custom_tools/text_to_sql/validators/schema_limiter.py::SchemaLimiter.build_schema_summary` | `32000` | Жёсткий потолок длины (в символах) описания схемы, которое влезает в промпт schema linking. Если обязательная часть схемы не влезает — исключение `SchemaContextBudgetExceeded` («Mandatory schema context exceeds schema_prompt_hard_max_chars»). |
| `sql_generation.max_tokens` | `custom_tools/text_to_sql/sql_generator.py::SqlGenerator._sql_generation_max_tokens` | `20000` | Лимит `max_tokens` LLM-вызова генерации SQL. |
| `nlu.intent_max_tokens` | `custom_tools/text_to_sql/nlu.py::_nlu_max_tokens`, вызывается из `NLUProcessor.extract_intent` | `800` | Лимит `max_tokens` для извлечения intent/сущностей. |
| `nlu.query_understanding_max_tokens` | `custom_tools/text_to_sql/nlu.py::_nlu_max_tokens`, вызывается из `NLUProcessor._understand_query` | `16000` | Лимит `max_tokens` для адаптивного разбора запроса (query understanding + completeness-проверка). |

Мёртвый ключ `nlp_max_tokens` удалён из всех профилей в W0-0.7 (регресс
закреплён тестом `tests/test_schema_linking_epic4_block_b.py::test_llm_models_yaml_has_no_dead_nlp_max_tokens_key`)
— секция `nlu` в рантайме читает только `query_understanding_max_tokens` и
`intent_max_tokens`. Возвращать `nlp_max_tokens` в yaml не нужно — ничего
его не читает.

**Когда крутить:** ответ модели обрезается / не помещается в лимит (пустой
или усечённый JSON-ответ на этом шаге) — поднять соответствующий
`max_tokens`. Схема слишком большая для промпта linking'а — поднять
`schema_prompt_hard_max_chars` (ценой более длинного и дорогого промпта).

---

## 6. LLM safety audit (советующий аудит SQL перед выполнением)

Переменные окружения и их дефолты (`custom_tools/text_to_sql/core/_sql_generation_api.py`):

| Env | Дефолт | Что делает |
|---|---|---|
| `TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_S` | `120` | Сколько секунд ждать ответа LLM-аудита, прежде чем считать вызов зависшим (`TimeoutError`). |
| `TEXT_TO_SQL_LLM_SAFETY_TTL_S` | `300` | Время жизни (сек) кеша **успешных** результатов аудита одного и того же SQL. |
| `TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_NEGATIVE_TTL_S` | `0.0` (выключено), константа `_LLM_SAFETY_TIMEOUT_NEGATIVE_TTL_DEFAULT_S` | Опциональный кеш **таймаутов** — если > 0, повторный такой же запрос в течение этого TTL не уходит в LLM снова, а сразу считается таймаутом. По умолчанию выключен, чтобы временный сбой LLM не «залипал» после восстановления. |
| `TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_CACHE_MAX` | `512` | Верхняя граница размера кеша таймаутов (FIFO-вытеснение старых записей). |

Логика — функции `_run_llm_safety_audit` и `_run_llm_safety_audit_with_timeout`
в том же файле.

**Fail-open (W0-0.5):** если LLM-аудит упал (таймаут, сетевой сбой,
невалидный JSON) — это фиксируется как **неблокирующий** advisory-issue
(`LLM_AUDIT_FAILED`/`LLM_AUDIT_TIMEOUT`), а не как повод отклонить запрос.
Комментарий в коде: «fail-open — static-safe результат не откатываем, сбой
самого LLM-аудита фиксируется как неблокирующий advisory-issue». То есть
**единственный блокирующий слой безопасности — статический анализ SQL**
(regex/AST-проверки на запрещённые операции); LLM-аудит — это совет поверх,
который может промолчать, но не может разблокировать то, что статика уже
запретила, и не может заблокировать то, что статика разрешила.

**Когда крутить:** аудит систематически не успевает (много `LLM_AUDIT_TIMEOUT`
в логах/артефактах) — поднять `TIMEOUT_S` либо (если сбои временные и
регулярные) включить `TIMEOUT_NEGATIVE_TTL_S`, чтобы не долбить упавший
LLM-эндпоинт повторными такими же запросами.

---

## 7. DSN-профиль (W1-1.2)

Пользовательское требование, зафиксированное в коде: «профиль должен
собираться под конкретный DSN (connection string — строка подключения к
конкретной базе) в разрезе схемы БД, универсальных профилей быть не
может». До этого модуля доменные подсказки (алиасы колонок, значимость
колонок, NLU-морфемы, примеры для schema linking) жили только в **именованных**
профилях (`default`), общих для всех БД сразу. `dsn_profile.py`
вводит второй, более приоритетный слой — один yaml-файл на конкретную БД.

| Что | Где / как |
|---|---|
| Путь к файлу | `sqlrag/<dsn_to_sanitized_name(dsn)>.profile.yaml` (`custom_tools/text_to_sql/dsn_profile.py::dsn_profile_path`) — тот же каталог и та же sanitized-схема имени, что и у `sqlrag/<name>.json`/`sqlrag/<name>.md`. |
| Загрузка | `load_dsn_profile(dsn, *, live_schema_fingerprint=None)`. Нет файла → `DsnProfile.empty()`. Файл от другого DSN (`dsn_fingerprint` не совпадает) → `ValueError`. |
| `TEXT_TO_SQL_DSN_PROFILE_STRICT` | env, default выключен (`0`/не задан) → устаревший (не совпадает `schema_namespace_version`) профиль даёт `logger.warning` и работает как есть; `=1` → `RuntimeError` вместо warning. |
| Деградирующий хелпер | `load_dsn_profile_or_empty(dsn, *, live_schema_fingerprint=None, purpose)` — общая точка входа для всех читателей, которым не нужно различать «профиля нет» и «профиль битый»: `ValueError` из `load_dsn_profile` гасится в `logger.warning` + `DsnProfile.empty()`, `RuntimeError` (STRICT) пробрасывается как есть. |

**Правило приоритета** (`custom_tools/text_to_sql/dsn_profile_overrides.py`:
`resolve_column_aliases_profile`, `resolve_significance_profile`,
`resolve_nlu_morphemes`): если DSN-профиль **непуст** в конкретной секции
(`aliases`, `type_hints`, `metric_hints.significant_columns`, `nlu_hints`) —
он **целиком замещает** соответствующую часть именованного (`default`)
профиля, а не мержится с ним поключево. Секции независимы друг
от друга: DSN-профиль может задать только `aliases`, оставив `type_hints`
наследоваться от именованного профиля. Причина — в `DsnProfile` нет
метаданных о том, какие именно ключи внутри секции автор «имел в виду»
переопределить; частичный merge для `significant_columns`/`nlu_hints`
(наборы нескольких списков/произвольная секция) был бы неоднозначен, а
полное замещение секции — единственное правило, одинаково работающее для
всех источников. `dsn` пробрасывается во всех путях линковки (обычная и
scoped-схема через `SchemaLinker._get_scoped_database_schema`) — поведение
одинаковое независимо от того, идёт ли линковка по «живой» схеме или по
сохранённому scoped-снапшоту.

**Скрипт создания профиля** (`autosave=False` — вызывает
`SchemaLoader.get_database_schema(..., autosave=False)`, то есть **не
пишет** побочный `sqlrag/<name>.json` при интроспекции схемы, только
явный `--out`):

- `scripts/text2sql_dsn_profile_scaffold.py --dsn <dsn> [--out PATH] [--force]`
  — интроспектирует схему целевого DSN и пишет пустой
  `sqlrag/<...>.profile.yaml` с заполненными identity-полями (`dsn_fingerprint`,
  `schema_namespace_version`, `captured_at`) — доменные секции оператор
  заполняет вручную.

**Известные ограничения:**
- `tokenizer` (морфемы-подсказки для токенизации) технически может попасть
  в `nlu_hints` DSN-профиля, но реально не используется: единственный
  потребитель токенизации (`NLUProcessor._fallback_tokenize`) вызывает
  резолвер конфига без `dsn` вовсе — DSN-профиль до него не доходит.
  Единственный путь, где DSN-профиль реально переопределяет NLU-конфиг —
  извлечение intent (`_fallback_extract_intent`), куда `dsn` пробрасывается.
- Профиль всегда собирается под конкретный DSN в разрезе схемы (identity —
  `dsn_fingerprint` + `schema_namespace_version`); универсальных
  «подходит для любой похожей базы» профилей архитектура не предусматривает.

---

## 8. Глоссарий DSN (W2-2.2)

`custom_tools/text_to_sql/dsn_glossary.py` превращает секцию `glossary`
DSN-профиля (термин + синонимы + таблица/колонка + kind + note, раздел 7) в
факты схемы двумя параллельными способами:

- **Семантические факты.** `glossary_semantic_facts` разворачивает каждую
  валидную запись глоссария в один `SemanticFact` на каждое значение
  `[term, *synonyms]`, с полями `fact_kind="glossary_term"`,
  `source="dsn_glossary"`, `status="approved"`. Запись, чьи таблица/колонка
  не найдены в живой схеме, пропускается с `logger.warning`, не роняя
  исследование. `apply_dsn_glossary` сохраняет этот набор через
  `SchemaMemoryManager.replace_dsn_glossary_facts(namespace, facts)` —
  замещающая семантика: пустой набор (термин убрали из профиля) тоже
  сохраняется и **очищает** предыдущий снапшот, а не оставляет старые факты
  висеть.
- **Текст описания.** `merge_glossary_synonyms_into_schema` приписывает
  строку `"Синонимы: term1, term2, ..."` к описанию соответствующей
  таблицы/колонки (не заменяя существующее описание, а дописывая к нему —
  тот же приём, что `SchemaLoader._merge_editable_schema`).

Порядок в `workflow/text_to_sql_typed_research.py::run_typed_schema_research`
важен: слияние синонимов в описания происходит **после** сохранения
scoped-снапшота схемы, поэтому строки «Синонимы: …» никогда не попадают в
`sqlrag/`-снапшот и не накапливаются там при повторных запусках; отпечаток
схемы (`schema fingerprint`) их тоже не учитывает.

**Нетранзакционность замены.** `replace_dsn_glossary_facts` →
`_replace_semantic_fact_source` (`custom_tools/text_to_sql/schema_memory_sqlite.py`)
сначала сохраняет маркер снапшота с новым `source_digest`, затем персистит
факты **по одному** отдельными вызовами `save_memory`. Если процесс упадёт
между этими двумя шагами (маркер уже записан, часть/все факты — ещё нет),
следующее чтение (`find_approved_semantic_facts`) увидит неполный или
пустой набор глоссария до следующего успешного вызова
`replace_dsn_glossary_facts` — это не откатывается автоматически.

---

## 8а. Редактор метаданных (UI)

`custom_tools/text_to_sql/metadata_editor.py` — оркестрация backend-действий
для UI-редактора описаний таблиц/колонок, глоссария и статуса семантических
фактов. Это не «ручка настройки пайплайна» в смысле остального документа
(нет env-переменных/констант с диапазоном), а справочник по четырём
AG-UI service actions (`backend/fastapi_app/agui/service.py::handle_service_action`,
раздел `"text_to_sql.metadata.*"`) и связанным инвариантам — оператору
полезно знать, что дёргает файлы `sqlrag/`, прежде чем чинить кэш руками.

| Действие | Права | Что делает |
|---|---|---|
| `text_to_sql.metadata.load` | любой пользователь с доступом к `connection_ref` | Отдаёт живую схему (обязательна доступная БД — валидация только через `SchemaLoader.load_scoped_schema`, «legacy»-путь `get_database_schema` не используется), описания/примеры из `sqlrag/<dsn>.json`, секцию `glossary` из `sqlrag/<dsn>.profile.yaml` и факты `source="typed_probe"` со статусом approved/rejected. |
| `text_to_sql.metadata.save_descriptions` | только роль `admin` | Частичное (read-modify-write) обновление описаний таблиц/колонок и примеров в `sqlrag/<dsn>.json`; `null` = не трогать поле, `""` = явно очистить. Пишет атомарно (tmp + fsync + `os.replace`, тот же приём, что `SchemaFileManager.save_scoped_snapshot`). |
| `text_to_sql.metadata.save_glossary` | только роль `admin` | Полная замена списка `glossary` в `sqlrag/<dsn>.profile.yaml` (не патч по одной записи). Если файла профиля ещё нет — создаёт его с identity-полями (`dsn_fingerprint`, `schema_namespace_version`, `captured_at`), остальные секции (`aliases`/`metric_hints`/`nlu_hints`/…) не трогает. |
| `text_to_sql.metadata.set_fact_status` | только роль `admin` | Единственный способ «отклонить» (`rejected`) конкретный `typed_probe`-факт — см. ниже, почему это отдельный механизм. |

**Почему запись — только `admin`.** Метаданные общие для всех пользователей
DSN (как и `apply_dsn_glossary`/`replace_file_semantic_facts`, которые тоже
применяются глобально к namespace, а не к одному пользователю). Три
write-действия сознательно добавлены только в `_ALL_SERVICE_ACTIONS` (не в
`_USER_ACTIONS`), поэтому автоматически попадают в `_ADMIN_ONLY_ACTIONS`
(`_ALL_SERVICE_ACTIONS - _USER_ACTIONS - ...`) — плюс defense-in-depth: явная
проверка `principal.has_role("admin")` внутри самого обработчика.

**Самообслуживаемая инвалидация — ничего руками сбрасывать не нужно** (см.
§0 плана `docs/plans/2026-09-05-text2sql-metadata-editor.md`):
- правка `sqlrag/<dsn>.json` меняет `editable_schema_digest` → следующий
  `load_scoped_schema` сам считает файл устаревшим и перестраивает снапшот;
- правка `sqlrag/<dsn>.profile.yaml` меняет `mtime_ns`/`size` → кэш
  `load_dsn_profile()` (ключ `(path, mtime_ns, size)`) и `env_fingerprint`
  schema-linking кэша (`SchemaCacheManager.prepare_cache_info`) сами считают
  предыдущее значение устаревшим;
- факты `source in {"file", "dsn_glossary"}` пересохраняются заново на
  каждый прогон исследования (replace-семантика) — старые версии сами
  перестают быть видны `find_approved_semantic_facts`.

**Единственное исключение — статус `typed_probe`-фактов.** У них нет
replace-набора (факт сохраняется один раз при типизированном исследовании и
не пересоздаётся), поэтому `set_fact_status` пишет отдельную запись-оверрайд
(`cache_kind="schema_semantic_fact_status_override"`,
`custom_tools/text_to_sql/schema_memory_sqlite.py::set_semantic_fact_status_override`)
поверх исходного факта, а не мутирует сам факт — это единственное место в
редакторе метаданных, где инвалидация не самообслуживается и требует
отдельного кода.

Оптимистическая блокировка (защита от «два администратора одновременно
правят одно и то же»): `save_descriptions`/`save_glossary` принимают
`expected_schema_digest`/`expected_glossary_digest` из последнего `load` и
сравнивают с текущим digest файла на момент записи; при расхождении —
`SchemaMetadataConflictError` (подкласс `ValueError`), UI должен предложить
перезагрузить метаданные. `set_fact_status` — идемпотентный тумблер без
токена версии (approved/rejected), конкурентная запись не создаёт
конфликта — см. открытый вопрос в §7 плана.

Контракт payload/ответов каждого действия — `docs/text_to_sql_contracts.md`,
раздел «4. Контракт действий редактирования метаданных».

---

## 9. Память успешных SQL (W2-2.3)

Кросс-run память: успешно выполненные и согласованные SQL-запросы
сохраняются и подмешиваются как подсказки в контекст солвера для похожих
будущих запросов той же базы (`custom_tools/text_to_sql/successful_sql_memory.py`).

| Переменная/константа | Где | Значение по умолчанию | Что делает | Откат |
|---|---|---|---|---|
| `TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED` | `successful_sql_memory.py::successful_sql_memory_enabled` | `1` (прод/дефолт рантайма); канонический бенчмарк-прогон (`release_inputs.canonical_runtime_environment`) форсирует `0` | Включает чтение/запись кросс-run памяти успешных SQL | `=0` |
| `SUCCESSFUL_SQL_TOP_K` | `successful_sql_memory.py` | `3` | Верхняя граница числа похожих примеров, возвращаемых `retrieve()` (`retrieve_successful_sql_examples`) | правка константы (код) |
| `MAX_SUCCESSFUL_SQL_CONTEXT_BYTES` | `successful_sql_memory.py` | `8192` | Потолок размера сериализованного retrieval-контекста (после обрезки `user_query`/`sql` до `_MAX_CONTEXT_QUERY_CHARS`=256/`_MAX_CONTEXT_SQL_CHARS`=1200) | правка константы (код) |

Чтение происходит **один раз** в `workflow/text_to_sql_adaptive_solver.py::_initial_solver_state`
(на старте генерации, до первого SQL-кандидата) и фиксируется в
`SolverState.similar_successful_sql_examples` — то есть результат
подсказки попадает в чекпоинт и переживает re-entry, а не перечитывается
заново на каждой итерации. Из контекста солвера эти подсказки выбрасываются
первым делом при переполнении лимита (раздел 3, шаг `_drop_similar_examples`).

Запись происходит в `custom_tools/text_to_sql/core/_audit.py::save_successful_sql`
**только** когда шаг завершения (`custom_tools/text_to_sql/core/_terminal.py::finalize_text_to_sql_run`)
дошёл до блока `if executed:` — то есть SQL реально выполнен **и** (если
result review включён и вернул вердикт) вердикт `"consistent"`. Ключ
`namespace_version_key` (scope + fingerprint) исключает утечку подсказок
между разными базами/схемами (namespace-изоляция). Проброс
`namespace_version_key` в `finalize_text_to_sql_run` теперь идёт **на обоих
путях финализации** (`workflow/enhanced_engine.py`): и на обычном успешном
пути (через `finalizer_step.tool_params`), и на fallback-пути
(`prepared.verified_execution`, semantic-repair/повторное исполнение) —
комментарий в коде явно называет второй «W2-2.3: this is the ordinary
(non-fallback) success path», подразумевая, что до этой правки fallback-путь
не прокидывал ключ. Значения (`user_query`/`sql`) перед записью маскируются
тем же хелпером (`_sanitize_audit_text`), что и legacy `sqlrag/*.md`. Dedup
у двух хранилищ разный: legacy-файл `sqlrag/*.md` хэширует немаскированный
SQL, а кросс-run память (`canonical_example_id`) — уже маскированный текст,
поэтому два запроса, различающиеся только маскируемыми значениями,
схлопнутся в одну запись памяти (см. раздел 16).

**Известное ограничение:** нет TTL и нет верхнего предела на количество
сохранённых примеров — `SUCCESSFUL_SQL_TOP_K`/`MAX_SUCCESSFUL_SQL_CONTEXT_BYTES`
ограничивают только то, что **читается** за один retrieval, но не рост
самого хранилища со временем. Путь к улучшению, не реализовано.

---

## 10. RRF в каталоге схемы (W3-3.1)

`custom_tools/text_to_sql/adaptive/schema_probes.py` ранжирует кандидатов
таблиц каталога схемы через RRF (Reciprocal Rank Fusion — способ объединить
несколько независимых ранжирований в одно: чем выше место в каждом
ранжировании, тем больше вклад `1 / (K + rank)`).

| Константа | Значение | Что делает |
|---|---|---|
| `RRF_K` | `60` | Сглаживающая константа RRF: `score(t) = Σ 1 / (RRF_K + rank)` по трём лексическим сигналам (имя таблицы / описание / колонка). Чем больше `K`, тем меньше вклад разницы в ранге между соседними кандидатами. |
| `FK_ANCHOR_CANDIDATE_COUNT` | `3` | Сколько топ-кандидатов по лексическому ранжированию используются как «якоря» для подсчёта FK-связности соседних таблиц. |
| `_RELATIONSHIP_EDGES_CACHE_MAXSIZE` | `8` | Размер LRU-кеша (`OrderedDict` + `move_to_end`/`popitem`) вычисленных рёбер связей схемы, ключ — `version_key` схемы. Кеш защищён `threading.Lock` (нужен, т.к. читается из нескольких `asyncio.to_thread`-воркеров одновременно). |

**Как считается ранг** (`_rank_catalog_candidates`): в RRF-сумму (`lexical_rrf`)
идут только три лексических сигнала. FK-связность (`fk_link_counts`, число
FK-рёбер к топ-`FK_ANCHOR_CANDIDATE_COUNT` лексическим якорям) в эту сумму
**не** входит — она применяется отдельно, только как тай-брейк между
кандидатами с **точно равным** `lexical_rrf`. Это гарантирует, что
структурно связанная (через FK), но лексически слабая таблица никогда не
обгонит таблицу с более сильным текстовым совпадением. Результат помечается
`ambiguous`, когда после обоих сравнений (`lexical_rrf` и FK-тай-брейк) у
двух и более кандидатов на первом месте всё ещё совпадают оба значения.

Публичная обёртка `relationship_edges_cached(schema, version_key)` даёт
доступ к тому же кешу коду вне `schema_probes.py` (используется подсказкой
код↔метка, раздел 11) — чтобы не пересчитывать рёбра связей повторно на
каждой итерации research. Результат глубоко заморожен: `tuple` рёбер, каждое
ребро — `MappingProxyType` (обёртка словаря «только чтение»), вложенные
`column_pairs` — тоже `tuple` из `MappingProxyType`; попытка мутировать
общий кеш на месте поднимает `TypeError`. Кеш защищён `threading.Lock`.

**Когда крутить:** RRF почти никогда не требует ручной подстройки —
`RRF_K`/`FK_ANCHOR_CANDIDATE_COUNT` это code-level константы, менять их
имеет смысл только если ранжирование каталога схемы систематически
выбирает не те таблицы на бенчмарке.

---

## 11. Подсказка код↔метка (W3-3.2)

`custom_tools/text_to_sql/adaptive/code_label_cascade.py` — если проба
`SEARCH_VALUE` искала значение, похожее на человекочитаемую метку (например,
название), внутри колонки с кодами, и не нашла ни одной строки — модуль
детерминированно (без LLM) предлагает, где искать метку на самом деле:
через FK на другую таблицу (`fk_lookup`) или через соседнюю колонку в той же
таблице по суффиксам вида `_id`/`_code`/`_key` ↔ `_name`/`_label`/`_title`/`_desc`
(`sibling_label`).

| Переменная/константа | Где | Значение по умолчанию | Что делает | Риск/что измерить | Откат |
|---|---|---|---|---|---|
| `TEXT_TO_SQL_CODE_LABEL_CASCADE_HINT` | `code_label_cascade.py::cascade_hint_mode` | `shadow` (неизвестное/пустое значение тоже сворачивается в `shadow` — fail-closed) | `off` — подсказка не считается вовсе; `shadow` — считается и логируется, но не попадает в промпт модели; `on` — попадает в контекст research (`code_label_cascade_hints` в `production_research.py::_bounded_research_context`) | В `on`: подсказки фильтруются по `research_schema` (кандидат вне узкой исследуемой схемы отбрасывается — модель всё равно не может обратиться к невидимой таблице); в `shadow`: лог считает `outside_research_schema=N` — сколько кандидатов отфильтровал бы `on`, чтобы оценить эффект до включения | `TEXT_TO_SQL_CODE_LABEL_CASCADE_HINT=off` |
| `_TOP_N` | `code_label_cascade.py` | `3` | Верхняя граница числа предложенных кандидатов на одну пустую пробу | — | правка константы (код) |

`_fk_lookup_candidates` явно исключает колонки — цель самой FK
(`column_pairs[*]["to_column"]`): это и есть код, который искали, а не
человекочитаемая метка (часто сама строковый первичный ключ вроде
кода территории).

---

## 12. Provenance (след выполненного запроса) (W4-4.1)

Не ручка — поле `provenance` в терминальном контракте всегда заполняется
для успешно выполненного запроса, отключить нельзя. Описано здесь, чтобы
было понятно, что оператор увидит в ответе и на что это влияет при отладке.

`_TEXT_TO_SQL_PROVENANCE_FIELDS` (`workflow/text_to_sql_contract.py`) —
закрытый набор полей: `run_id`, `tables`, `columns`, `row_count`,
`row_limit`, `possibly_truncated`, `safety_llm_audit`, `result_review_verdict`,
`parse_error`, `has_derived_tables`. Собирает их
`custom_tools/text_to_sql/core/_terminal.py::_build_text_to_sql_provenance`
— `tables`/`columns`/`parse_error`/`has_derived_tables` разбираются из уже
выполненного SQL через sqlglot (`_sql_provenance_tables_and_columns`,
best-effort: любая ошибка разбора даёт `parse_error=True`, а не падение),
остальное — из уже посчитанных `execution`/`result_review`.

`has_derived_tables` (булево) отвечает на вопрос «читает ли запрос через
промежуточный слой (CTE — `WITH ... AS (...)`, либо подзапрос-источник в
`FROM`/`JOIN`), а не напрямую из физических таблиц». Если да — `tables`
называет только физические таблицы, которые читает этот промежуточный
слой, а не имя самого CTE/подзапроса, и колонки вида `cte.id` остаются
неразрешёнными до конкретной физической таблицы. Контракт требует
`has_derived_tables=false`, когда `parse_error=true` (разобрать не
удалось — утверждать что-либо о структуре запроса нельзя). Ограничение
(см. также раздел 16): CTE-имя исключается из списка таблиц по простому
совпадению имени, а не через полноценный scope-резолвер — тот же
компромисс, что и для остального разбора этой функции.

Старые чекпоинты/история запусков, сохранённые до появления этого поля, не
содержат ключа `provenance` вовсе — `TextToSqlTerminalResult.from_mapping`
подставляет для них дефолт `{}` (`_LEGACY_OPTIONAL_TERMINAL_FIELDS`), чтобы
не ронять чтение старых записей (checkpoint resume, история в
`backend/fastapi_app/agui/store.py`, доставка результата в
`workflow/result_delivery.py`).

В Streamlit (`streamlit_app/pages/05_Text_to_SQL.py`) provenance выводится
как краткая подпись под ответом: `format_text_to_sql_provenance_footer`
(`workflow/text_to_sql_provenance.py`) собирает читаемую строку, `st.caption(footer)`
её показывает. Если `has_derived_tables=true`, к перечислению таблиц в этой
подписи добавляется пометка «(через промежуточные подзапросы)».

---

## 13. Уточняющий вопрос (W4-4.2)

`workflow/text_to_sql_clarifying_question.py` — если адаптивный цикл
останавливается досрочно из-за неоднозначности/недостатка данных,
пайплайн умеет вернуть детерминированный (без нового вызова LLM,
фиксированный русскоязычный шаблон, заполненный уже известными полями
`AmbiguityReport`/`MissingEvidenceRequest`) уточняющий вопрос вместо
голого отказа.

| Переменная/константа | Где | Значение по умолчанию | Что делает | Откат |
|---|---|---|---|---|
| `TEXT_TO_SQL_CLARIFYING_QUESTIONS` | `clarifying_questions_enabled()` | `1` (включено); канонический бенчмарк форсирует `0` | Включает/выключает вставку `outputs.clarification_needed` в результат раннего останова | `=0` |
| `MAX_CLARIFICATION_OPTIONS` | `text_to_sql_clarifying_question.py` | `8` | Обрезает список вариантов ответа (`options`) для UI-виджета | правка константы (код) |
| `MAX_CLARIFICATION_QUESTION_CHARS` | `text_to_sql_clarifying_question.py` | `600` | Обрезает текст вопроса и каждого варианта ответа (с суффиксом `…`), чтобы не раздувать ответ | правка константы (код) |

`workflow/enhanced_engine.py` кладёт результат в `outputs["clarification_needed"]`
только если `clarifying_questions_enabled()` вернул `True`; манифест
бенчмарка также фиксирует `clarifying_questions_enabled` как флаг
канонического окружения прогона.

---

## 14. Гейт docs↔schema (W4-4.3)

CI-гейт, а не ручка: следит, чтобы контракт терминального результата
(`workflow/text_to_sql_contract.py`), его JSON-схема для фронтенда, ручной
TypeScript-контракт и текстовая документация не расходились.

- `scripts/text_to_sql_contract_schema.py --check` — сверяет
  `tests/fixtures/text_to_sql_contract_schema.json` с тем, что прямо сейчас
  выведет `build_schema()` из `workflow.text_to_sql_contract` (статусы,
  reason codes, обязательные поля); без `--check` — перезаписывает фикстуру.
- `frontend/client/src/app/lib/textToSqlContracts.ts` — ручной TS-контракт,
  синхронизируется вручную при изменении Python-контракта.
- `docs/text_to_sql_contracts.md` — текстовое описание контракта (включая
  семью `contract_name` в `adaptive/models.py`), тоже обновляется вручную.
- Тесты-гейты: `tests/test_text_to_sql_contract_schema_sync.py`,
  `frontend/client/src/app/lib/__tests__/textToSqlContractsSync.test.ts`,
  `tests/test_text_to_sql_contracts_documented.py`.
- Явный шаг в `.github/workflows/text2sql-release.yml` (`Contract schema
  fixture is current`) прогоняет `--check` в релизном CI.

Процедура при изменении контракта (5 шагов) описана прямо в docstring
`scripts/text_to_sql_contract_schema.py`: править
`workflow/text_to_sql_contract.py` → перезаписать фикстуру → вручную
поправить `.ts` → поправить `docs/text_to_sql_contracts.md` → прогнать три
теста-гейта выше.

---

## 15. Диагностика / наблюдаемость

### 15.1 Логирование ответов модели (`retry_openai_model.py`)

| Env | Дефолт | Где | Эффект |
|---|---|---|---|
| `LLM_RESPONSE_LOGGING_ENABLED` | `"0"` (выключено) | `RetryOpenAIServerModel.__init__` (`self.debug_logging`) | Включает построчный (`.jsonl`) лог всех попыток вызова модели в `logs/llm_responses/responses.jsonl`. |
| `LLM_RESPONSE_LOG_MAX_BYTES` | `5242880` (5 МиБ) | `_get_response_log_handler` | Размер файла лога до ротации (`RotatingFileHandler`). |
| `LLM_RESPONSE_LOG_BACKUPS` | `3` | `_get_response_log_handler` | Сколько ротированных файлов лога хранить. |

Формат одной строки лога (`_write_response_log`): `timestamp`, `run_id`,
`step_name` (оба — из `llm_call_context.py`, contextvar, привязывается
вокруг вызова провайдера, например в `workflow/text_to_sql_typed_research.py`),
`status`, `model_id`, `attempt`, `current_model_index`, `latency_ms`, `usage`
(`prompt_tokens`/`completion_tokens`, если есть), `error`, `response`
(нормализованный текст ответа).

**Когда крутить:** нужно разобрать, что именно модель вернула на конкретном
шаге конкретного прогона (по `run_id`/`step_name`) — включить
`LLM_RESPONSE_LOGGING_ENABLED=1`. Имейте в виду: лог пишет **сырые ответы
модели** — потенциально с данными из БД, включайте прицельно, не постоянно
в проде.

### 15.2 Длительность шагов воркфлоу (`workflow/enhanced_engine.py`)

Каждый `StepResult` (включая пути через circuit breaker/loop detector и
верхнеуровневые исключения — везде помечено комментарием `W0-0.3`) содержит
`resource_usage["duration_seconds"]` — время выполнения шага в секундах.
Это не переменная окружения, а гарантированное поле каждого результата шага
— смотреть в артефактах прогона, чтобы найти самый медленный шаг пайплайна.

---

## 16. Известные ограничения и планы

- **Контекст солвера при переполнении лимита усекается, а не сразу падает**
  (см. раздел 3). Пять детерминированных шагов усечения (сброс похожих
  примеров → документы по содержимому → документы по количеству → история
  кандидатов → диагностика старых проверок), и только если не влезло даже
  неусекаемое ядро — `ValueError`.
- **`replay_contract.py::_validate_model_budget_records` не покрывает все
  типы вызовов.** Модульная константа `_MODEL_CALL_ID = re.compile(r"^research-model-(?P<revision>\d+)-(?P<attempt>\d+)$")`
  распознаёт только call-id вида `research-model-<revision>-<attempt>` — не
  знает про `research-stop-review-...`, `solver-generate-...` или
  `*:empty-retry:*` (раздел 1.3). Модуль `replay_contract.py`/`replay.py` —
  read-only сборка артефактов для диагностики/дебага, это не блокирующий
  гейт в проде — но если писать новый инструмент валидации бюджета поверх
  этого регэкспа, про ограничение нужно помнить.
- **Provenance разбирает CTE по имени, а не через полноценный scope-резолвер.**
  `_sql_provenance_tables_and_columns` (`custom_tools/text_to_sql/core/_terminal.py`,
  раздел 12) исключает имена `WITH cte AS (...)` из списка таблиц по
  простому совпадению имени; собственный комментарий в коде называет это
  «narrow, name-based fix» и указывает, что правильное решение — переиспользовать
  scope-резолвер `validators/schema_aware.py` (тот же, что использует
  валидация SQL), но это отдельная задача.
- **Память успешных SQL дедуплицирует по маскированному тексту** (раздел 9) —
  запросы, различающиеся только маскируемыми значениями, дают одну запись.
- **Память успешных SQL не имеет TTL и предела размера** (раздел 9) —
  ограничен только объём одного retrieval (`SUCCESSFUL_SQL_TOP_K`,
  `MAX_SUCCESSFUL_SQL_CONTEXT_BYTES`), не рост хранилища со временем.
- **Оценка «байт на токен» — фиксированная эвристика, не токенизация**
  (`APPROXIMATE_BYTES_PER_TOKEN = 4`, раздел 3) — на кириллице может как
  занижать, так и завышать реальное число токенов; стоит сверить на
  реальном трафике перед тем, как полагаться на неё для точного расчёта
  бюджета.
- **DSN-профиль не покрывает `tokenizer`/токенизацию** (раздел 7) —
  единственный потребитель токенизации вызывает резолвер конфига без
  `dsn`, поэтому DSN-профиль реально работает для алиасов/значимости
  колонок/intent-эвристик, но не для токенизации.
- **Замена глоссария DSN нетранзакционна** (раздел 8) — маркер снапшота и
  сами факты пишутся отдельными вызовами; крах между ними оставляет
  неполный снапшот до следующего успешного вызова.

---

## 17. Итог: сколько ручек в каталоге

Считаем переменные окружения и код-константы, которые реально можно
покрутить, не трогая логику. Не считаем как «ручки» механизмы, которые
всегда включены и не параметризуются (provenance, докс-гейт, применение
глоссария DSN как таковое) — они описаны в каталоге для полноты, но не
входят в счётчик.

- 11 — выбор модели по шагам (`step_models.*` в `config/text_to_sql/llm_models.yaml`, включая `research_stop_review`; `schema_research`/`sql_solver` — зеркала agent-профилей) — раздел 1;
- 1 — переключатель профиля моделей `TEXT_TO_SQL_LLM_MODELS_PROFILE` (`default`/`experiment`) — раздел 1.3;
- 5 — бюджет модельных вызовов (`model_budget.*` + перекрёстная проверка с `resource_limits.model_tokens`) — раздел 3;
- 4 — усечение контекста солвера/ревью (`APPROXIMATE_BYTES_PER_TOKEN`, `DOC_CONTENT_CHAR_CAP`, `HISTORY_KEEP`, `DIAG_CHAR_CAP`) — раздел 3;
- 8 — остальные лимиты `adaptive.yaml` (wall clock, db probes, объём результата, sample rows) — раздел 4;
- 5 — лимиты токенов по шагам в `llm_models.yaml` — раздел 5;
- 4 — переменные окружения LLM safety audit — раздел 6;
- 1 — `TEXT_TO_SQL_DSN_PROFILE_STRICT` — раздел 7;
- 3 — память успешных SQL (`TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED`, `SUCCESSFUL_SQL_TOP_K`, `MAX_SUCCESSFUL_SQL_CONTEXT_BYTES`) — раздел 9;
- 3 — RRF каталога схемы (`RRF_K`, `FK_ANCHOR_CANDIDATE_COUNT`, `_RELATIONSHIP_EDGES_CACHE_MAXSIZE`) — раздел 10;
- 2 — подсказка код↔метка (`TEXT_TO_SQL_CODE_LABEL_CASCADE_HINT`, `_TOP_N`) — раздел 11;
- 3 — уточняющий вопрос (`TEXT_TO_SQL_CLARIFYING_QUESTIONS`, `MAX_CLARIFICATION_OPTIONS`, `MAX_CLARIFICATION_QUESTION_CHARS`) — раздел 13;
- 3 — переменные окружения логирования ответов модели — раздел 15.

**Итого: 53 отдельных настраиваемых точки** (было 36 на момент прошлой
консолидации; +17 за счёт W1–W5).
