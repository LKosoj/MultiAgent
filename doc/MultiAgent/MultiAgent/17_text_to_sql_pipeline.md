# Глава 17: Пайплайн Text-to-SQL

Конвейер, превращающий вопрос на естественном языке в безопасный SQL и итоговый ответ. Реализован поверх Workflow Engine: каждый этап — отдельный шаг (`tool`/`agent`) с явными `depends_on`, `condition`, `metadata` и собственной `retry_policy`.

## Зачем
- Жёсткая декомпозиция этапов NLU, schema linking, SQL generation, верификации и аудита.
- Каждый шаг отвечает за одну функцию, ошибки локализуются, retry применяются точечно.
- Поведение пайплайна полностью описывается YAML; код не хардкодит шаги.

## Источник истины
- YAML: [`workflow_pipelines/text_to_sql_pipeline.yaml`](../../../workflow_pipelines/text_to_sql_pipeline.yaml).
- Все таблицы и mermaid-схема в этой главе должны соответствовать актуальному YAML. При расхождении доверяем YAML.

## Inputs

| Имя | Тип | Default | Описание |
|---|---|---|---|
| `query` | string | `""` | Пользовательский запрос на естественном языке. |
| `dsn` | string | `""` | Database DSN для подключения. |
| `max_rows` | int | `100` | Лимит строк, который `db_audit_agent` обязан передать в `secure_db_executor` как `row_limit={max_rows}`. |
| `session_id` | string | `""` | Стабильный идентификатор сессии/схемы. |
| `run_id` | string | `""` | Уникальный идентификатор конкретного запуска (см. 6.14). |
| `safety_level` | string | `strict` | Уровень безопасности SQL-валидатора. |
| `include_explanation` | bool | `true` | Включать ли пояснение в финальный отчёт. |
| `validate_schema` | bool | `true` | Запускать ли валидацию по схеме (используется `sql_verifier_agent`). |
| `dry_run_only` | bool | `false` | Если `true`, `db_audit` не выполняет SQL в БД и возвращает `executed=false`, `status="skipped"`. |
| `use_schema_suggestions` | bool | `true` | Если `false`, `schema_linking_step` пропускается и подставляется `skip_output`; AG-UI `TextToSqlGenerateRequest` отклоняет `use_schema_suggestions=false` вместе с `validate_schema=true`, потому что schema validation требует источника схемы. |
| `allow_enhanced_fallback` | bool | `false` | Прокидывается в metadata `db_audit` для downstream-логики. |

## Outputs

| Имя | from_step | field | format | Источник |
|---|---|---|---|---|
| `final` | `db_audit` | `output` | markdown | Финальный отчёт исполнения SQL. |
| `sql_generation` | `sql_generation` | `output` | json | JSON `{sql, description}` от `sql_generator_agent`. |
| `sql_verification` | `sql_verification` | `output` | json | JSON `{verification_status, safety_check, performance_check, recommendations}`. |
| `schema_linking` | `schema_linking_step` | `output` | json | Результат `schema_linking` (либо `skip_output`). |
| `intent` | `intent_extraction_step` | `output` | json | Извлечённые интенты/сущности. |
| `nlu` | `nlu_processing` | `output` | json | Токены/POS-разбор. |

## Шаги пайплайна

После декомпозиции god-manager (см. EPIC 6.3) ранее единый `sql_pipeline` разбит на три самостоятельных шага. Каждый агент — отдельный step с собственным `agent_type`, `depends_on`, `retry_policy` и `metadata`. Раздел `preload_agents` больше не используется: оркестрация выражена через граф `depends_on`.

| ID шага | Тип | Агент/инструмент | depends_on | Назначение |
|---|---|---|---|---|
| `nlu_processing` | tool | `natural_language_processing` | — | Токенизация и POS-разбор `{query}`. |
| `intent_extraction_step` | tool | `intent_extraction` | — | Извлечение интентов и сущностей из `{query}`. |
| `schema_linking_step` | tool | `schema_linking` | `nlu_processing`, `intent_extraction_step` | Связывание сущностей со схемой БД через RAG. Имеет `condition: '{use_schema_suggestions}'`. |
| `sql_generation` | agent | `sql_generator_agent` | `schema_linking_step` | Генерация SQL по `{query}` + NLU + schema linking. Возвращает JSON `{sql, description}`. |
| `sql_verification` | agent | `sql_verifier_agent` | `sql_generation` | Безопасность и корректность SQL. Возвращает JSON c `verification_status` (Approved/Rejected) и `recommendations`. |
| `db_audit` | agent | `db_audit_agent` | `sql_verification` | Выполнение SQL и аудит. `condition: '{sql_verification.verification_status} == "Approved"'`. |

### Шаги-инструменты (NLU и schema linking)

`nlu_processing` и `intent_extraction_step` запускаются параллельно (`parallel_execution: true`, `max_parallel_steps: 2`).

```yaml
- id: nlu_processing
  step_type: tool
  tool_name: natural_language_processing
  tool_params:
    text: '{query}'

- id: intent_extraction_step
  step_type: tool
  tool_name: intent_extraction
  tool_params:
    text: '{query}'

- id: schema_linking_step
  step_type: tool
  tool_name: schema_linking
  condition: '{use_schema_suggestions}'
  tool_params:
    entities: '{intent_extraction_step.entities}'
    dsn: '{dsn}'
  depends_on:
    - nlu_processing
    - intent_extraction_step
  # DSN передается параметром инструмента, а не текстом prompt/ответа.
  metadata:
    skip_output:
      status: "skipped_disabled"
      reason: "use_schema_suggestions=false"
      linked_entities:
        metrics: []
        dimensions: []
        filters: {}
      joins: []
      join_success: false
      unlinked_entities: []
      schema_info: {}
```

Существенно:
- `tool_params.entities = '{intent_extraction_step.entities}'` — точечная ссылка на поле `entities` результата предыдущего шага (не на весь output).
- `depends_on` для `schema_linking_step` — оба NLU-шага (раньше зависимости были перепутаны).
- `condition: '{use_schema_suggestions}'` управляет skip-логикой: при `false` step пропускается, и в `step_outputs` подставляется `skip_output` с `status: "skipped_disabled"` (см. EPIC 6.9). Раньше использовался ключ `disabled: true` — он удалён.

### Шаги-агенты (sql_generation → sql_verification → db_audit)

```yaml
- id: sql_generation
  step_type: agent
  agent_type: sql_generator_agent
  depends_on:
    - schema_linking_step
  retry_policy:
    max_retries: 2
    backoff_strategy: exponential

- id: sql_verification
  step_type: agent
  agent_type: sql_verifier_agent
  depends_on:
    - sql_generation
  output_retry_policy:
    condition: '{sql_verification.verification_status} == "Rejected"'
    rerun_step: sql_generation
    max_iterations: 2
    feedback_field: sql_safety_check_feedback

- id: db_audit
  step_type: agent
  agent_type: db_audit_agent
  condition: '{sql_verification.verification_status} == "Approved"'
  depends_on:
    - sql_verification
  metadata:
    skip_output:
      status: skipped_rejected_by_verifier
      reason: SQL was rejected by verifier; db_audit not executed
      executed: false
```

### Cross-step retry (verification → generation feedback loop)

`sql_verification` использует поле `output_retry_policy` (вынесено из `metadata`, потому что условие ссылается на собственный output шага и должно вычисляться после его исполнения):

- `condition: '{sql_verification.verification_status} == "Rejected"'` — триггер на повтор.
- `rerun_step: sql_generation` — какой шаг перезапускать.
- `feedback_field: sql_safety_check_feedback` — имя переменной, в которой движок прокидывает output отклонённой верификации в `context.variables`. `sql_generator_agent` подставляет её через `{sql_safety_check_feedback}` в своём prompt и учитывает `recommendations`.
- `max_iterations: 2` — loop guard. При превышении движок останавливается, `verification_status` остаётся `Rejected`, и `db_audit` автоматически скипается по своему `condition`.

Реализация: `workflow.engine._maybe_run_output_retry` (см. тесты `tests/test_workflow_engine_epic6.py::test_verifier_reject_triggers_generator_retry` и `::test_output_retry_policy_respects_max_iterations`).

### condition + skip_output для `db_audit`

`db_audit` выполняется только при одобренной верификации. Если `sql_verification.verification_status != "Approved"`, движок:
1. Не запускает агент `db_audit_agent`.
2. Подставляет в `step_outputs["db_audit"]` объект `skip_output` (`status: skipped_rejected_by_verifier`, `executed: false`).
3. Финальный output пайплайна берётся из `db_audit.output` (см. `outputs.final`) — клиент видит явный signal, что выполнение было заблокировано на этапе верификации.

## Поток выполнения

```mermaid
flowchart TD
    Q([Пользовательский запрос])
    Q --> NLU[nlu_processing\n(tool)]
    Q --> INT[intent_extraction_step\n(tool)]
    NLU --> LINK{schema_linking_step\n(tool)\ncondition: use_schema_suggestions}
    INT --> LINK
    LINK -- skipped_disabled --> GEN
    LINK -- linked_entities --> GEN[sql_generation\n(agent: sql_generator_agent)]
    GEN --> VER[sql_verification\n(agent: sql_verifier_agent)]
    VER -- Rejected\n(output_retry_policy,\nfeedback → sql_safety_check_feedback,\nmax_iterations=2) --> GEN
    VER -- Approved --> AUD[db_audit\n(agent: db_audit_agent)\ncondition: verification_status == Approved]
    VER -- Rejected/loop_guard --> SKIP[(db_audit skipped:\nstatus=skipped_rejected_by_verifier)]
    AUD --> OUT([final markdown])
    SKIP --> OUT
```

## Безопасность
- `sql_verifier_agent` использует `sql_safety_check` и блокирует DDL/DML и опасные команды (см. конфигурацию `config/text_to_sql/safety.yaml`).
- `db_audit_agent` обязан передавать `row_limit={max_rows}` при вызове `secure_db_executor` (см. 6.4).
- При `dry_run_only=true` `db_audit_agent` не обращается к БД и возвращает `executed=false`, `status="skipped"`.
- `output_retry_policy` ограничен `max_iterations`, чтобы verifier↔generator не зациклились.

## Связанные изменения EPIC 6
- 6.1: подстановка переменных в `metadata` с fail-fast на unresolved placeholders.
- 6.2: `json.dumps` для dict/list при подстановке в `task.format`.
- 6.3: декомпозиция god-manager `sql_pipeline` на три отдельных шага (источник этой главы).
- 6.4: `db_audit_agent` обязан передавать `row_limit={max_rows}`.
- 6.5: `sql_verifier_agent` возвращает фиксированный JSON-контракт.
- 6.9: `schema_linking_step.skip_output.status = "skipped_disabled"` вместо `disabled: true`.
- 6.14: `run_id` добавлен в `inputs` и в подстановку переменных.

## Вывод
Пайплайн Text-to-SQL — декомпозированный YAML-flow: параллельные NLU-инструменты, опциональный schema linking, три отдельных агента (generation → verification → audit) и cross-step retry для feedback-loop между verifier и generator. Любые правки поведения должны идти через YAML, а не через код движка.
