# Контракты Text-to-SQL

«Контракт» здесь — это неизменяемая (immutable) структура данных с жёсткой
проверкой формы (какие поля обязательны, какие значения допустимы), которую
разные части пайплайна text-to-sql передают друг другу вместо произвольных
словарей. Это нужно, чтобы этап A не мог случайно прислать этапу B данные
неправильной формы — ошибка всплывёт сразу при создании объекта, а не где-то
глубже по цепочке.

Ниже — два разных семейства контрактов, которые не стоит путать:

1. **Терминальный контракт исполнения** (`workflow/text_to_sql_contract.py`,
   класс `TextToSqlTerminalResult`) — финальный результат прогона пайплайна
   (успех/отказ/ошибка, SQL, данные, причина). Он зеркалится во фронтенд
   (`frontend/client/src/app/lib/textToSqlContracts.ts`), потому что UI должен
   понимать те же статусы и причины, что и бэкенд.
2. **Контракты состояния исследования и решателя** (`custom_tools/text_to_sql/
   adaptive/models.py`) — промежуточные, внутренние структуры adaptive-пайплайна
   (описание запроса, свидетельства из БД, состояние поиска, состояние решателя
   SQL-кандидатов). Они не идут во фронтенд, у них нет TS-зеркала.

## 1. Терминальный контракт (`workflow/text_to_sql_contract.py`)

Источник истины — перечисления `TextToSqlTerminalStatus`,
`TextToSqlTerminalReasonCode` и множество полей `TextToSqlTerminalResult._FIELDS`
(экспортировано как `TEXT_TO_SQL_TERMINAL_REQUIRED_FIELDS`).

Их точный список **не дублируется в этом документе руками** — единственный
источник истины для документации и для TS-зеркала — сгенерированная фикстура
[`tests/fixtures/text_to_sql_contract_schema.json`](../tests/fixtures/text_to_sql_contract_schema.json)
(строит её `scripts/text_to_sql_contract_schema.py`, поле `build_schema()`).
Смотрите в неё, чтобы увидеть актуальный список `terminal_statuses`,
`reason_codes`, `required_fields`.

Зеркало во фронтенде: `frontend/client/src/app/lib/textToSqlContracts.ts`
(`TEXT_TO_SQL_TERMINAL_OUTCOME_STATUSES`, `TEXT_TO_SQL_REASON_CODES`,
`terminalFields`).

## 2. Контракты состояния adaptive-пайплайна (`custom_tools/text_to_sql/adaptive/models.py`)

Каждый из этих контрактов — pydantic-модель с полем-меткой
`contract_name: Literal["..."]`, которое фиксирует тип объекта в
сериализованном виде (полезно, когда объекты разных контрактов лежат вместе,
например в логах или в истории шагов).

- **`query_spec`** (класс `QuerySpec`) — разобранный пользовательский запрос:
  исходный текст, извлечённые смысловые элементы (`semantic_items`), какие
  источники данных запрошены на выходе, ожидаемая форма результата и
  глобальные ограничения (`global_constraints`).
- **`evidence_record`** (класс `EvidenceRecord`) — одно «свидетельство»,
  полученное в ходе исследования схемы/данных (например, результат пробы
  к БД): откуда оно получено, что именно наблюдалось, в какой области
  действия (`validity_scope`) оно остаётся достоверным.
- **`research_state`** (класс `ResearchState`) — снимок состояния этапа
  исследования схемы: накопленные гипотезы, свидетельства, привязки
  (`bindings`), кандидаты на join, ещё не разрешённые смысловые элементы,
  история действий и причина остановки исследования (`stop_reason`), если
  оно завершилось.
- **`missing_evidence_request`** (класс `MissingEvidenceRequest`) — запрос на
  недостающее свидетельство: какой смысловой элемент не удалось разрешить,
  какой вопрос нужно закрыть, какие цели-кандидаты рассмотреть и почему
  потребовался повторный заход в исследование.
- **`research_reentry_record`** (класс `ResearchReentryRecord`) — запись об
  одном повторном заходе в исследование по конкретному
  `missing_evidence_request`: с какой базовой ревизии он стартовал, чем
  закончился (`status`) и какие новые свидетельства принёс.
- **`solver_state`** (класс `SolverState`) — снимок состояния этапа решения
  (генерации и проверки SQL-кандидатов): сами кандидаты, результаты проверок
  и исполнения, запросы на недостающие свидетельства, повторные заходы в
  исследование, история действий решателя и выбранный итоговый кандидат.

## 3. Процедура при изменении контракта

Если меняется состав `TextToSqlTerminalStatus` / `TextToSqlTerminalReasonCode`
/ обязательных полей `TextToSqlTerminalResult` в
`workflow/text_to_sql_contract.py` (или, для раздела 2, состав/назначение
моделей в `custom_tools/text_to_sql/adaptive/models.py`), выполните по порядку:

1. Внесите изменение в `workflow/text_to_sql_contract.py` (и/или
   `custom_tools/text_to_sql/adaptive/models.py`).
2. Перегенерируйте фикстуру: `python3 scripts/text_to_sql_contract_schema.py`
   (без `--check` — эта команда переписывает файл).
3. Синхронизируйте `frontend/client/src/app/lib/textToSqlContracts.ts`
   (`TEXT_TO_SQL_REASON_CODES`, `TEXT_TO_SQL_TERMINAL_OUTCOME_STATUSES`,
   `terminalFields`) вручную по новому содержимому фикстуры.
4. Обновите этот документ (раздел 2 — при изменении состава/назначения
   `contract_name` в `models.py`; раздел 1 не требует ручной правки списков,
   но проверьте, не устарело ли описание).
5. Прогоните гейты, которые ловят рассинхрон:
   - `tests/test_text_to_sql_contract_schema_sync.py` — фикстура соответствует
     `workflow/text_to_sql_contract.py`;
   - `frontend/client/src/app/lib/__tests__/textToSqlContractsSync.test.ts` —
     TS-константы соответствуют фикстуре;
   - `tests/test_text_to_sql_contracts_documented.py` — каждый `contract_name`
     из `custom_tools/text_to_sql/adaptive/models.py` упомянут в этом
     документе (и наоборот — в документе нет несуществующих имён).

## 4. Контракт действий редактирования метаданных (`text_to_sql.metadata.*`)

Четыре стандартных AG-UI service actions
(`backend/fastapi_app/agui/service.py::handle_service_action`, тот же
транспорт, что у `text_to_sql.schema.load`). Ошибки — обычные исключения
(`ValueError`/`PermissionError`/подклассы), их заворачивает существующий слой
AG-UI в конверт `{ok: false, error: str(exc)}`.

Реализация — `custom_tools/text_to_sql/metadata_editor.py`. Права доступа и
самообслуживаемая инвалидация кэшей описаны в
`docs/operations/text2sql-tuning-knobs.md`, раздел «8а. Редактор метаданных
(UI)».

### 4.1 `text_to_sql.metadata.load`

Payload: `{ "connection_ref": "conn-..." }` (обязательно; «сырой» dsn не
поддерживается).

Ответ:
```jsonc
{
  "connection_ref": "conn-...",
  "dsn_dialect": "postgresql",
  "schema_digest": "sha256hex" | null,
  "editable_file_enabled": true | false | null,
  "tables": {
    "<table_fqn>": {
      "description": "...",
      "description_source": "file" | "none",
      "columns": {
        "<column_name>": {
          "type": "text",
          "description": "...",
          "description_source": "file" | "none",
          "examples": ["..."],
          "examples_source": "file" | "none"
        }
      }
    }
  },
  "glossary": {
    "digest": "sha256hex",
    "profile_exists": true | false,
    "dsn_fingerprint": "postgresql://host:5432/db" | null,
    "schema_namespace_version": "sha256hex" | null,
    "entries": [
      { "term": "...", "synonyms": ["..."], "table": "...", "column": "..." | null,
        "kind": "dimension" | "measure" | "filter_value" | "entity" | null,
        "note": "..." | null }
    ]
  },
  "facts": [
    { "fact_key": "text2sql-semantic-fact-v1-...", "subject": "table" | "column",
      "table_fqn": "...", "column": "..." | null,
      "fact_kind": "description" | "example" | "glossary_term",
      "value": "...", "status": "approved" | "rejected" }
  ]  // только source == "typed_probe"
}
```

Права: любой принципал с доступом к `connection_ref` (та же проверка, что и
`text_to_sql.schema.load`).

### 4.2 `text_to_sql.metadata.save_descriptions`

Payload:
```jsonc
{
  "connection_ref": "conn-...",
  "expected_schema_digest": "sha256hex" | null,
  "tables": [
    {
      "table_fqn": "public.orders",
      "description": "..." | null,     // null/отсутствует = не менять, "" = очистить
      "columns": [
        { "column": "status", "description": "..." | null, "examples": ["..."] | null }
      ]
    }
  ]
}
```

Ответ: `{ "saved": true, "schema_digest": "sha256hex" }`.

Права: только роль `admin`. Частичное обновление (read-modify-write):
меняются только присланные поля, остальное содержимое файла — без изменений.
Лимитов на размер нет: таблицы и колонки проверяются только на существование
в живой схеме, длину описаний под промпт усекает пайплайн (`SCHEMA_DESC_LIMIT`).
Несовпадение `expected_schema_digest` с текущим digest файла →
`SchemaMetadataConflictError` (подкласс `ValueError`).

### 4.3 `text_to_sql.metadata.save_glossary`

Payload:
```jsonc
{
  "connection_ref": "conn-...",
  "expected_glossary_digest": "sha256hex",   // для отсутствующего профиля — sha256([])
  "entries": [
    { "term": "выручка", "synonyms": ["revenue", "оборот"], "table": "public.orders",
      "column": "amount" | null, "kind": "measure" | null, "note": "..." | null }
  ]
}
```

Ответ: `{ "saved": true, "glossary_digest": "sha256hex", "entries": [...] }` —
`entries` возвращаются в том виде, в каком сохранены (обрезанные пробелы,
дедуплицированные синонимы), чтобы UI показывал сохранённое состояние без
повторной загрузки.

Права: только роль `admin`. Семантика — **полная замена** списка `glossary`
целиком (не патч по одной записи). Лимитов на число записей и длину полей
нет; `table`/`column` проверяются по живой схеме. Несовпадение
`expected_glossary_digest` → `SchemaMetadataConflictError`.

### 4.4 `text_to_sql.metadata.set_fact_status`

Payload: `{ "connection_ref": "conn-...", "fact_key": "text2sql-semantic-fact-v1-...", "status": "approved" | "rejected" }`.

Ответ: `{ "saved": true, "fact_key": "...", "status": "rejected" }`.

Права: только роль `admin`. Действует только на факты `source ==
"typed_probe"` (описания/примеры из файла и термины глоссария уже
редактируются через 4.2/4.3). Неизвестный `fact_key` или факт не
`typed_probe` → `ValueError`. Без токена оптимистической блокировки — это
идемпотентный тумблер.
