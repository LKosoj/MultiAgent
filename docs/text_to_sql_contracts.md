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
