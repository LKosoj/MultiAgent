# Аудит проекта MultiAgent — полный отчёт

Дата: 2026-05-29. Метод: 24 ревью-агента (Sonnet) по модулям + адверсариальная верификация critical/high (Opus). Из 259 сырых находок отброшено 19 ложных, 47 скорректированы по severity. Итог: **240** находок.

- 🔴 critical: 5
- 🟠 high: 30
- 🟡 medium: 143
- ⚪ low: 62


## 🔴 CRITICAL (5)

### C1. resume_workflow() всегда выбрасывает исключение — функция восстановления сломана
- **Модуль:** Workflow Engine: ядро
- **Категория:** correctness | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `workflow/engine.py:374-384`
- **Проблема:** После вызова `await self.state_manager.resume_workflow(workflow_id)` (строка 376) следует безусловный `raise WorkflowExecutionError(...)` (строка 378). Любой вызывающий код, включая внешних клиентов, получает исключение независимо от того, существует ли checkpoint. Код в pipeline_runner.py обходит это, вызывая state_manager.save_checkpoint напрямую, что подтверждает: функция нерабочая.
- **Влияние:** Весь декларируемый функционал восстановления workflow после сбоя недоступен через публичный API движка. Любой пайплайн, полагающийся на `engine.resume_workflow()`, упадёт с WorkflowExecutionError.
- **Исправление:** Либо реализовать resume (загрузить определение workflow из checkpoint/БД и перезапустить с незавершённых шагов), либо убрать метод и явно пометить как TODO. В текущем виде — нерабочий stub, скрытый за рабочим API.
- **Что даст:** Восстановление после сбоев станет реально работающим; устранится ложное ощущение надёжности.
- **Заметка верификатора:** engine.py:374-384 подтверждён: после await self.state_manager.resume_workflow(workflow_id) идёт безусловный raise WorkflowExecutionError, и любой путь упирается в except->raise. state_manager.resume_workflow (строки 705-719) реально возвращает context, но движок его игнорирует. Прод действительно обходит метод: StoryBookManager/core/pipeline_runner.py:557 (resume_workflow_from_checkpoint) и :678 (resume_pipeline) восстанавливают через from_yaml + execute_workflow / save_checkpoint напрямую, никогда не вызывая engine.resume_workflow(). Публичный API восстановления нерабочий.

### C2. Перезапись live-профиля при неудачном бэкапе
- **Модуль:** prompt_optimizer + telemetry
- **Категория:** correctness | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `prompt_optimizer/prompt_optimizer.py:418`
- **Проблема:** update_profile() вызывает backup_profile() и игнорирует возвращаемый bool. Если бэкап завершился с ошибкой (нет места, права доступа), функция продолжает перезаписывать боевой профиль агента строками от LLM без какого-либо восстановления.
- **Влияние:** Безвозвратная потеря исходного промпта агента при первом же сбое дискового ввода-вывода или нехватке прав на backup_dir. Восстановить будет нечем.
- **Исправление:** Проверить возврат backup_profile(): `if not self.backup_profile(...): return False`. Аналогично в fallback-ветке (строка 450).
- **Что даст:** Гарантия: профиль перезаписывается только при наличии подтверждённого бэкапа.
- **Заметка верификатора:** Подтверждено. prompt_optimizer.py:418 self.backup_profile(agent_name, dict(profile_data)) игнорирует возвращаемый bool. backup_profile (381-396) ловит любое исключение и возвращает False, но update_profile продолжает: строки 421/436-437 перезаписывают боевой профиль выводом LLM без проверки результата бэкапа. Тот же дефект во fallback-ветке (450). Защитный замысел backup-before-overwrite не работает при сбое I/O или прав на backup_dir; в agent_profiles_backup не появится копии, и restore_agents.py восстанавливать будет нечем. Митигирующий контекст (не меняет severity по сути): весь оптимизатор за gate OPTIMIZE_AGENTS=true (491, по умолчанию off), а agent_profiles/ обычно под git. Но необратимая перезапись source-of-truth профиля при первом же сбое диска реальна — critical оставлен.

### C3. CodeAgent получает additional_authorized_imports='*' — неограниченный RCE
- **Модуль:** Ядро: оркестрация агентов
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `agent_factory.py:336`
- **Проблема:** CodeAgent создаётся с `additional_authorized_imports='*'`, что разрешает LLM-генерируемому коду импортировать любой модуль Python: subprocess, os, socket, shutil и т.д. Одновременно константа AUTHORIZED_IMPORTS (строки 88-128) не используется нигде — она мёртвый код, replace которого и было это изменение.
- **Влияние:** Злоумышленник, управляющий входной задачей (через AG-UI endpoint или Streamlit), может через prompt injection заставить CodeAgent выполнить произвольный shell-код: скачать и запустить бинарник, прочитать ~/.ssh/id_rsa, вытащить DB_DSN из env и т.д. Поверхность атаки особенно широка потому что input_guard_agent закомментирован (agent_system.py:157-178).
- **Исправление:** Заменить `additional_authorized_imports='*'` на список только необходимых модулей. Вернуть использование AUTHORIZED_IMPORTS или удалить константу и использовать явный список. Параллельно раскомментировать input_guard_agent.
- **Что даст:** Устраняет возможность RCE через prompt-injection в LLM-генерируемый код.
- **Заметка верификатора:** Подтверждено в agent_factory.py:336 — CodeAgent создаётся с additional_authorized_imports='*'. grep по всему репозиторию: это единственное использование флага, а константа AUTHORIZED_IMPORTS (строки 88-128) не упоминается больше нигде — действительно мёртвый код. '*' в smolagents отключает белый список импортов, разрешая os/subprocess/socket и т.д. LLM-генерируемому коду. CodeAgent (manager и общие агенты) исполняет сгенерированный Python в том же процессе, без песочницы. RCE-поверхность реальна. Critical адекватна.

### C4. Input-Guard-Agent полностью закомментирован — нет валидации входа
- **Модуль:** Ядро: оркестрация агентов
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `agent_system.py:156-178`
- **Проблема:** Весь блок 'Этап 1: Проверка безопасности на входе' закомментирован. Любая задача, включая инструкции типа 'ignore previous instructions and...', передаётся напрямую менеджер-агенту без фильтрации.
- **Влияние:** Отсутствие input-guard в production-системе с публичным AG-UI endpoint означает, что любой пользователь может попытаться промпт-инжектировать менеджера или sub-агентов. Особенно опасно в сочетании с `additional_authorized_imports='*'`.
- **Исправление:** Раскомментировать или переработать input_guard_agent. Если гард отключён намеренно на период разработки — добавить явный feature-flag, чтобы это было видно, а не скрыто в комментарии.
- **Что даст:** Первый рубеж защиты от prompt injection и abuse.
- **Заметка верификатора:** Подтверждено: agent_system.py:156-178 — весь блок 'Этап 1: Проверка безопасности на входе' закомментирован построчно, initial_task идёт прямо в Этап 2 (создание агентов и manager_agent.run). Задача достижима через AG-UI singleton (service.py:2727 run_manager_with_team -> coordinate) и Streamlit. В сочетании с imports='*' (находка 1) отсутствие любой фильтрации входа делает prompt-injection -> RCE реалистичным. Critical оправдана как часть той же цепочки.

### C5. exec() без изоляции — полный обход любых security-проверок
- **Модуль:** Ядро: LLM-модель + codeinterpreter
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `codeinterpreter.py:365-368`
- **Проблема:** Единственная «защита» перед exec() — строковая проверка `if "rm -r" in code or "os.system" in code`. Это тривиально обходится: `__import__('os').system('rm -rf /')`, `getattr(__builtins__, 'eval')('...')`, `subprocess.Popen(...)`, `open('/etc/passwd').read()`, `import socket; socket.connect(...)`. После проверки выполняется `exec(code, exec_globals, exec_globals)`, где exec_globals содержит реальные os, sys, plt, pd, logging — то есть атакующий получает полный доступ к файловой системе, сети и процессам хоста.
- **Влияние:** Произвольное выполнение кода на сервере: RCE, кража секретов из окружения, уничтожение данных, pivot во внутреннюю сеть.
- **Исправление:** Использовать реальную изолированную среду: subprocess с seccomp/chroot/Docker/gVisor, либо RestrictedPython/pyodide. Минимум — передавать exec() словарь без доступа к builtins: `exec(code, {'__builtins__': {}}, sandbox_locals)`. Строковая фильтрация — не защита.
- **Что даст:** Устранение критической дыры RCE, которая делает остальные защитные меры бессмысленными.
- **Заметка верификатора:** Подтверждено. codeinterpreter.py:363-368: единственная защита перед exec(code, exec_globals, exec_globals) — substring-проверка `if "rm -r" in code or "os.system" in code`. exec_globals (строки 352-362) содержит реальные os, pd, np, plt, matplotlib, logging. Обходы тривиальны: `__import__('os').system(...)`, `__import__('subprocess')...`, `open('/etc/passwd').read()`, `__import__('socket')`. data_path/code_prompt — внешне управляемые параметры (get_spec + @tool code_execution/data_analysis/code_interpreter_tool). Это полноценный RCE в исполнителе LLM-кода. Severity critical адекватна. Оговорка: модуль codeinterpreter.py в текущем репозитории нигде не импортируется боевым кодом (ссылка только в .cli-proxy логе), но это плагин с @tool, явно предназначенный для регистрации и исполнения сгенерированного кода — уязвимость по существу реальна.


## 🟠 HIGH (30)

### H1. Мутация lru_cache-кэшированного AGENT_PROFILES — гонка данных при параллельных запросах
- **Модуль:** Ядро: оркестрация агентов
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `agent_streamlit_api.py:1094 / agent_command.py:171`
- **Проблема:** `__getattr__('AGENT_PROFILES')` возвращает сам внутренний dict `_load_agent_profiles()` без копирования. В `agent_streamlit_api.py:1094` этот dict мутируется: `AGENT_PROFILES[temp_profile_name] = ...` и удаляется в `finally`-блоке (строка 1122). При двух параллельных запросах возникает race: один поток добавляет ключ, другой итерирует профили (например, в `get_available_agents`), а `finally`-удаление может удалить ключ, который другой поток ещё не прочитал.
- **Влияние:** В многопоточном/многозадачном (asyncio) окружении: KeyError, IterationError при изменении словаря во время итерации, или создание агента с невалидным профилем.
- **Исправление:** В `__getattr__` вернуть копию: `return dict(_load_agent_profiles())`. Для потокобезопасного временного профиля использовать threading.Lock или отдельный реестр `_dynamic_profiles`, не совмещённый с кэшированным.
- **Что даст:** Thread-safety при динамическом создании агентов.
- **Заметка верификатора:** Подтверждено. agent_command.py:167-172 __getattr__('AGENT_PROFILES') возвращает _load_agent_profiles() (lru_cache maxsize=1) БЕЗ копии; копирует только публичная load_agent_profiles() (161). agent_factory.py:9 и agent_streamlit_api.py:29 делают 'from ... import AGENT_PROFILES', связывая один и тот же мутабельный кэш-объект во всех модулях. create_dynamic_agent (agent_streamlit_api.py:1094) мутирует AGENT_PROFILES[temp]=... и удаляет в finally (1122). Этот метод вызывается в AG-UI in-process через action 'agents.dynamic.create' (service.py:2713) на singleton _AGENT_MANAGER, а FastAPI обрабатывает запросы конкурентно. Параллельный create + итерация профилей в get_available_agents (agent_system.py:51) / list_agents (586,622) даёт классический 'dict changed size during iteration' / KeyError. Локов вокруг нет. Окно узкое (нужна одновременная динамическая регистрация), но баг реальный и достижимый — high оправдана.

### H2. Гонка данных на _GLOBAL_ACTIVE_RUNS и _GLOBAL_AGENT_PROCESSES — нет блокировок
- **Модуль:** Ядро: configuration & streamlit API
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `agent_streamlit_api.py:433-436, 720-732, 1208, 1483-1486`
- **Проблема:** _GLOBAL_ACTIVE_RUNS и _GLOBAL_AGENT_PROCESSES — обычные dict Python, доступные из нескольких потоков одновременно (основной поток Streamlit, watchdog-потоки каждого запуска, поток cleanup_completed_runs). Dict в CPython не атомарен для составных операций: строки 912-913 делают check-then-write без блокировки, строки 1513 делают del без блокировки пока watchdog может читать тот же ключ. cancel_agent_run (строки 1397-1491) читает run_data['status'] и затем обновляет — классический TOCTOU.
- **Влияние:** Возможны KeyError при параллельных отменах и cleanup, потеря статуса запуска, дублирование записей или неполная очистка при высокой нагрузке.
- **Исправление:** Добавить threading.RLock для _GLOBAL_ACTIVE_RUNS и _GLOBAL_AGENT_PROCESSES; обернуть все check+modify операции в with lock.
- **Что даст:** Предотвращает неопределённое поведение и крэши при нескольких параллельных агентах.
- **Заметка верификатора:** Подтверждено: _GLOBAL_ACTIVE_RUNS/_GLOBAL_AGENT_PROCESSES — обычные dict (строки 433-435), self.active_runs ссылается прямо на них (строка 568). Единственные Lock в файле (112/259) — это log_lock для файлов логов, реестры запусков ничем не защищены. Несколько daemon-watchdog потоков (766, 1209) мутируют те же ключи, а cleanup_completed_runs (1494, del на 1513) и cancel_agent_run (1397-1492, check-then-act на 1397/1400/1402 + update 1477) вызываются из основного потока Streamlit (03_Agents.py:569, 02_Workflows.py:692) и из FastAPI (service.py:2661). Это реальные TOCTOU/гонки. GIL делает отдельные операции атомарными, но составные (check-then-write на 911-913, del на 1513 при параллельном чтении) — нет. high адекватна.

### H3. Monkey-patching self.factory.create_agent в потоке без защиты от конкурентности
- **Модуль:** Workflow Engine: ядро
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `workflow/enhanced_engine.py:984-999`
- **Проблема:** `_execute_enhanced_agent_step` (строки 984-999) заменяет `self.factory.create_agent` на `create_enhanced_agent`, затем вызывает `super()._execute_agent_step()`, который запускает `_execute_agent_sync()` в thread pool (engine.py:1063). Если несколько шагов выполняются параллельно (parallel_execution=True или несколько workflow одновременно), второй поток может увидеть `create_enhanced_agent` вместо оригинала или, наоборот, `original_create_agent` будет восстановлен до того, как первый поток завершит создание агента.
- **Влияние:** В параллельном режиме агенты создаются с неправильным pipeline_type; возможно создание агентов с чужим оригинальным методом. Крайне хрупкий паттерн — race condition, воспроизводимый при parallel_execution=True.
- **Исправление:** Не мутировать общий объект factory. Передавать pipeline_type как параметр или создать локальный враппер, не затрагивающий общее состояние.
- **Что даст:** Устранение скрытого race condition при параллельном выполнении; предсказуемое создание агентов.
- **Заметка верификатора:** Подтверждено. enhanced_engine.py:984-999 мутирует общий self.factory.create_agent и восстанавливает в finally. При parallel_execution=True путь _execute_enhanced_steps_parallel (468-490) -> ParallelWorkflowExecutor.execute_steps_parallel запускает шаги через asyncio.create_task (parallel_executor.py:225) конкурентно на одном инстансе движка/фабрики. Точка await super()._execute_agent_step (995) уступает event loop -> второй шаг успевает перепатчить/восстановить create_agent; реальный вызов create_agent происходит позже в _execute_agent_sync в thread pool (engine.py:1063,1090). Возможно создание агента с чужим pipeline_type ('workflow' вместо 'enhanced_workflow' и наоборот) или вложенный патч original_create_agent. Реальный race на разделяемом мутабельном состоянии в поддерживаемом режиме. Импакт — неверная конфигурация агента, не катастрофа, но корректностный дефект; high обоснован.

### H4. Blocking poll loop freezes Streamlit UI thread
- **Модуль:** Streamlit UI
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `streamlit_app/pages/05_Text_to_SQL.py:816`
- **Проблема:** В `generate_sql_query()` используется `while True: ... time.sleep(0.5)` внутри `with st.spinner(...)`. Весь Streamlit UI-поток блокируется до завершения workflow. Нет таймаута — если workflow завис, цикл не завершится никогда.
- **Влияние:** При зависшем workflow страница намертво зависает: пользователь не может перейти на другую вкладку, прервать операцию или перезагрузить без принудительного перезапуска сервера. Это главный блокирующий путь для основного use-case продукта.
- **Исправление:** Запускать workflow асинхронно (background job), возвращать run_id немедленно, опрашивать статус при st.rerun() через session_state — так же, как сделано в run_agents_text_to_sql. Или добавить таймаут polling-а (например, 5 минут) с явным st.error при выходе.
- **Что даст:** UI остаётся отзывчивым; пользователь видит прогресс и может отменить операцию.
- **Заметка верификатора:** Подтверждено на 05_Text_to_SQL.py:816-820. В живом основном пути generate_sql_query() действительно `while True: status_obj = wf_manager.get_workflow_status(run_id); if ... break; time.sleep(0.5)` внутри `with st.spinner(...)`. Нет ни таймаута, ни ограничения итераций, ни проверки на None как условия выхода. Streamlit выполняет скрипт сессии в одном потоке, поэтому при зависшем workflow вкладка пользователя блокируется бессрочно. Это основной (не deprecated) путь продукта. severity high адекватна.

### H5. Мутирующее накопление RAG-контекста в system_prompt при каждом цикле планирования
- **Модуль:** Ядро: оркестрация агентов
- **Категория:** correctness | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `agent_factory.py:622-626`
- **Проблема:** В `_wrap_write_memory_to_messages` при каждом вызове с `summary_mode=True` (т.е. при каждом шаге планирования менеджер-агента) строка 624 делает `enhanced_prompt = original_prompt + '\n\n' + rag_summary` и записывает результат обратно в `agent.memory.system_prompt.system_prompt`. При следующем шаге `original_prompt` уже содержит предыдущий `rag_summary`, и он добавляется снова.
- **Влияние:** Системный промпт менеджера растёт экспоненциально с числом шагов планирования. При 50 шагах (default max_steps для manager) и 10 записях RAG по 200 токенов каждая — добавляется ~2000 токенов на шаг, итого ~100K лишних токенов. Приведёт к превышению контекстного окна модели, деградации качества и резкому росту стоимости.
- **Исправление:** Не мутировать `system_prompt` in-place. Вместо этого добавлять RAG-контекст как отдельное сообщение в возвращаемый список, либо добавлять его только один раз (на первом шаге). Проверять, не был ли RAG-контекст уже добавлен в текущем run.
- **Что даст:** Предотвращает взрывной рост контекста и деградацию менеджера на длинных задачах.
- **Заметка верификатора:** Подтверждено в agent_factory.py:622-626: original_prompt читается из персистентного agent.memory.system_prompt.system_prompt и записывается обратно как original_prompt + rag_summary. На следующем вызове original_prompt уже содержит прошлый rag_summary -> накопление. Путь срабатывает при summary_mode=True для manager (гейт 579-584; не зависит от provide_run_summary). Профиль manager.yaml имеет planning_interval: 2, значит write_memory_to_messages(summary_mode=True) вызывается на каждом шаге планирования (до ~25 раз при max_steps=50), каждый раз дописывая до 10 RAG-записей в персистентный system_prompt. Рост контекста/стоимости реален. Формулировка 'экспоненциально' неточна (рост кумулятивно-суперлинейный, не удвоение), но это не меняет суть — неограниченное раздувание system_prompt. Severity high сохранена.

### H6. cleanup_old_state пытается удалить из несуществующей таблицы workflow_events
- **Модуль:** Workflow Engine: ядро
- **Категория:** correctness | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `workflow/state_manager.py:783-789`
- **Проблема:** `cleanup_old_state` выполняет `DELETE FROM workflow_events` (строка 785), но таблица `workflow_events` не создаётся в `SQLiteWorkflowStore.init_database()` (строки 337-377) — она создаётся в другом модуле (`workflow/events/store.py`). Если `state_manager` обращается к своей копии БД `workflow_state.db`, а events-store использует другой файл, запрос упадёт с `sqlite3.OperationalError: no such table: workflow_events`. Исключение проглатывается в `_maybe_cleanup_state` (строка 760-761).
- **Влияние:** Автоочистка молча падает каждый раз — warning в лог, но данные не чистятся. Возможен неограниченный рост `workflow_state.db` в продакшне.
- **Исправление:** Убрать DELETE workflow_events из `cleanup_old_state` в state_manager, либо создать таблицу в `init_database` с `CREATE TABLE IF NOT EXISTS workflow_events`.
- **Что даст:** Автоочистка заработает корректно; устранится рост БД.
- **Заметка верификатора:** Подтверждено: init_database (state_manager.py:337-377) создаёт только workflow_checkpoints и workflow_metadata, не workflow_events. Таблицу workflow_events создаёт лишь workflow/events/store.py:EventStore (db_path по умолчанию 'workflow_state.db'), который инстанцируется только в EventDrivenWorkflowEngine (event_driven/engine.py:34). Основной прод-путь — EnhancedWorkflowEngine (main.py:121, pipeline_runner.py:42, streamlit_api.py:675), а не event-driven, поэтому в workflow_state.db таблицы нет. _maybe_cleanup_state вызывается прямо в WorkflowStateManager.__init__ (state_manager.py:609) и при DELETE FROM workflow_events (785) бросает OperationalError: no such table, которая проглатывается (760-761). Исключение прерывает транзакцию до завершения всех трёх DELETE, очистка не происходит -> неограниченный рост workflow_state.db. Реальная операционная проблема; high оставлен (зависит от того, не трогал ли тот же файл event-driven движок ранее).

### H7. RunManager._runs растёт неограниченно — утечка памяти
- **Модуль:** Backend FastAPI / AG-UI
- **Категория:** correctness | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `backend/fastapi_app/agui/run_manager.py:79`
- **Проблема:** self._runs: dict[str, RunInfo] никогда не очищается. RunInfo хранит events: list[tuple[int, BaseEvent]] — весь payload всех событий в памяти на каждый run. При длительной работе под нагрузкой это неограниченный рост: каждый запрос /agent добавляет запись, которая никогда не удаляется. В самом run_manager нет метода prune/evict. В events список хранятся полные Pydantic-объекты событий.
- **Влияние:** OOM при длительной работе. В продакшен-окружении с активным стримингом каждый завершённый run занимает десятки KB.
- **Исправление:** Добавить TTL-based eviction: периодически удалять RunInfo со статусом в _TERMINAL_STATUSES, если finished_at_ms старше N минут. Либо не хранить events в памяти, читать их только из store при replay. info.events используется только в stream_live и cancel — его можно заменить на store.list_after.
- **Что даст:** Предотвращает OOM при длительной работе.
- **Заметка верификатора:** Подтверждено. run_manager.py:79 self._runs: dict[str, RunInfo]; start_run добавляет запись (94), и нигде нет pop/clear/del/prune/evict (grep по agui/ не нашёл ни одной очистки _runs — все *.cleanup относятся к другим менеджерам в service.py). RunInfo.events: list[tuple[int, BaseEvent]] (70) накапливает полные redacted Pydantic-объекты всех событий на весь жизненный цикл процесса. Каждый /agent или /v1/runs -> новая неудаляемая запись. Под стримингом это безграничный рост -> OOM. High обоснована для прод-окружения с активным трафиком (хотя без прямого эксплойта это availability-риск; high на верхней границе, но допустимо).

### H8. Seed перезаписывается в None после инициализации из brief.json
- **Модуль:** Storybook: инструменты генерации
- **Категория:** correctness | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `custom_tools/storybook/artist_batch_edit.py:2137`
- **Проблема:** В `artist_agent_batch_edit_tool` seed сначала читается из brief.json или items_obj (строки 1818–1836), затем на строке 2137 безусловно перезаписывается: `seed = items_obj.get('seed', None)`. Если items_obj не содержит ключ 'seed', seed становится None, игнорируя ранее загруженное значение из brief.json. В результате seed, прочитанный из файла проекта, всегда теряется при отсутствии seed в items_obj.
- **Влияние:** Воспроизводимость генерации изображений нарушена: при повторном запуске с тем же проектом генерация даёт разные результаты, хотя seed явно сохранён в brief.json.
- **Исправление:** Строку 2137 заменить на `seed = items_obj.get('seed', seed)` (сохранить ранее загруженное значение как fallback).
- **Что даст:** Восстанавливает детерминированность генерации при повторных запусках.
- **Заметка верификатора:** Подтверждено. seed грузится из brief.json/items_obj на строках 1818-1836 и корректно используется в _preprocess_canon_references (1841) и _ensure_references_exist (1865). Но на строке 2137 безусловно `seed = items_obj.get('seed', None)`. Именно этот seed передаётся в основные воркеры рендера (2151, 2214, 2227 -> _worker -> edit_image_vse_tool seed=). Если items_obj не содержит ключ 'seed', но brief.json содержит — строка 1836 сохраняет seed из brief, а строка 2137 затирает его в None (random). Воспроизводимость основной генерации теряется. Default для 2137 должен был быть `seed` (ранее загруженное значение), а не None. Реальный баг, severity high адекватна.

### H9. locals()[var_name] = ... не работает для присвоения переменных
- **Модуль:** Storybook: инструменты генерации
- **Категория:** correctness | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `custom_tools/storybook/story_editor.py:86`
- **Проблема:** Код пытается динамически присвоить значения локальным переменным через `locals()[var_name] = json.load(f)`. В Python изменение словаря `locals()` не имеет эффекта на реальные локальные переменные (CPython реализует это через замену словаря, но сами переменные в frame не обновляются). В результате `characters_data`, `consistency_rules`, `locations_data` никогда не получают загруженные данные — они остаются пустыми словарями `{}`, объявленными выше.
- **Влияние:** Story editor всегда передаёт пустые контекстные данные персонажей/локаций/правил в LLM, игнорируя bible. LLM редактирует текст без знания о персонажах и консистентности.
- **Исправление:** Заменить на явные присвоения: `if filename == 'characters.json': characters_data = json.load(f)` и т.д. Или использовать dict вместо отдельных переменных.
- **Что даст:** Story editor начнёт реально использовать bible-контекст при редактировании.
- **Заметка верификатора:** Подтверждено однозначно. characters_data/consistency_rules/locations_data инициализируются {} (71-73), единственное присваивание — через `locals()[var_name] = json.load(f)` (86), которое в теле функции CPython не меняет реальные локальные переменные. Эти переменные затем читаются в payload_json (163-165), payload (172) и в single-chapter payload (219-221) — всегда пустыми. style_data и brief присваиваются напрямую и работают. Итог: редактор всегда отправляет LLM пустой контекст персонажей/локаций/правил консистентности, и это затрагивает оба пути (и default edit_all_chapters=False). Severity high оправдана — дефект тихо обнуляет ключевую consistency-функцию редактора.

### H10. API-ключ smithery передаётся в URL как query-параметр в открытом виде
- **Модуль:** Ядро: tool manager + MCP
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `mcp_tools.py:99`
- **Проблема:** `query["api_key"] = [smithery_api_key]` добавляет API-ключ в строку запроса SSE-URL. URL с секретом попадает в логи aiohttp/httpx, в логи прокси, в заголовок Referer браузера, в telemetry. Кроме того, `smithery_api_key` кодируется в base64 внутри `config_b64` (строка 95) — это не шифрование.
- **Влияние:** Утечка API-ключа в логи приложения, сетевые логи, telemetry traces.
- **Исправление:** Передавать ключ через заголовок Authorization, а не через query-строку. Удалить `query["api_key"]` из URL.
- **Что даст:** Предотвращает утечку токена в логи и HTTP-инфраструктуру.
- **Заметка верификатора:** mcp_tools.py:99 query['api_key'] = [smithery_api_key] кладёт ключ в query-строку SSE-URL; стр.95 config_b64 — base64, не шифрование (верно). url затем передаётся в MCPClient (стр.161) и уходит на SSE-транспорт (smolagents/httpx), где может попасть в логи соединений/ошибок и telemetry. Локальный print (стр.105/164/173) печатает только имя/тип сервера, не URL, так что прямой утечки в stdout этого модуля нет — но secret-in-URL остаётся стандартной high-проблемой из-за транспортных логов/трейсов. Severity адекватна.

### H11. SSRF + Path Traversal через data_path: загрузка произвольных файлов без валидации
- **Модуль:** Ядро: LLM-модель + codeinterpreter
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `codeinterpreter.py:601-614`
- **Проблема:** В `run_code()` параметр `data_path` принимается извне (через плагин-вызов или `@tool`) и используется без валидации. Если значение начинается с 'http' — файл скачивается по произвольному URL (SSRF: атакующий может указать `http://169.254.169.254/latest/meta-data/` или внутренний сервис). Если это локальный путь — `shutil.copy2(data_path, new_data_path)` копирует произвольный файл хоста в папку `data/`, после чего `load_data()` читает его через pandas. Через `file_name, file_ext = os.path.splitext(data_path)` с path типа `../../../etc/passwd` и `.csv` — файл окажется в data-директории.
- **Влияние:** Утечка внутренних файлов сервера, SSRF против облачного metadata-сервиса, чтение credentials из файловой системы.
- **Исправление:** Валидировать data_path: разрешать только пути внутри белого списка каталогов (os.path.realpath + проверка prefix); для URL — DNS/IP белый список или блокировка приватных диапазонов; проверять расширение файла по allowlist.
- **Что даст:** Устранение SSRF и path traversal, защита файловой системы и внутренней инфраструктуры.
- **Заметка верификатора:** Подтверждено частично с уточнением. run_code (601-615): data_path внешне управляем; при префиксе 'http' идёт download_file() с голым httpx.get(url) без allowlist — реальный SSRF (169.254.169.254/metadata, внутренние сервисы). Иначе shutil.copy2(data_path, ...) копирует произвольный локальный файл. Уточнение по path traversal в data/: new_data_path проходит через os.path.basename(), так что назначение всегда внутри data/ — записи вне data/ нет. Чтение `../../../etc/passwd` как описано не сработает: splitext('/etc/passwd') даёт ext='', а load_data (419-427) принимает только .csv/.xlsx/.json и иное отклоняет; для чтения нужен валидный csv/xlsx/json по произвольному пути. SSRF — главный реальный вектор. Снижаю до high (path-traversal-into-data часть и точное цитирование `.csv`+passwd неверны, но SSRF и копирование произвольных локальных файлов реальны).

### H12. install_package принимает имя пакета из LLM-ответа/exec — инъекция через pip
- **Модуль:** Ядро: LLM-модель + codeinterpreter
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `codeinterpreter.py:272-278`
- **Проблема:** В `_execute_code()` при `ModuleNotFoundError` имя пакета извлекается из сообщения об ошибке: `str(e).split("'")[1]`. Это имя пакета передаётся в `install_package()`, который вызывает `asyncio.create_subprocess_exec(sys.executable, "-m", "pip", "install", package_name, ...)`. LLM-модель или код в exec() могут намеренно вызвать `import evil-package` или `import package; raise ModuleNotFoundError("No module named 'malicious-package'")`, чтобы вынудить систему установить произвольный пакет из PyPI.
- **Влияние:** Установка вредоносного пакета с supply-chain атакой; RCE через setup.py/postinstall-хуки.
- **Исправление:** Иметь allowlist разрешённых пакетов; не устанавливать пакеты автоматически в production. Как минимум — валидировать имя пакета regex `^[a-zA-Z0-9_\-\.]+$` и проверять по белому списку.
- **Что даст:** Защита от supply-chain атак через автоустановку пакетов.
- **Заметка верификатора:** Подтверждено. _execute_code (375-379): при ModuleNotFoundError missing_package = str(e).split("'")[1] передаётся в install_package(), который выполняет asyncio.create_subprocess_exec(sys.executable,'-m','pip','install',package_name). Имя контролируется содержимым ImportError, а его может задать любой код в exec() (`raise ModuleNotFoundError("No module named 'evil-pkg'")`) или просто `import evil_pkg`. Нет allowlist/проверки. Реальная supply-chain/RCE через setup.py. Хотя при уже существующем RCE из exec() это дополнительный вектор, сам по себе он валиден и требует только генерации импорта моделью; severity high адекватна.

### H13. API-ключ сохраняется в plaintext в YAML-файле конфигурации
- **Модуль:** Ядро: configuration & streamlit API
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. critical→high
- **Где:** `configuration_api.py:284-296`
- **Проблема:** _save_config() вызывает self._config.to_dict() и записывает результат через yaml.dump. LLMConfig.api_key (строка 56) попадает в словарь as-is через asdict(). Фильтрация секрета существует только в export_config() (строка 757-760), но не в _save_config(). Любой вызов update_llm_config/update_config/import_config/reset_to_defaults пишет ключ в config/streamlit_config.yaml открытым текстом.
- **Влияние:** API-ключ (OpenAI/Anthropic) оседает на диске в читаемом файле конфигурации. Если файл попадает в git, бэкап или к другому процессу — ключ скомпрометирован.
- **Исправление:** В _save_config() перед дампом создавать копию словаря и обнулять config_dict['llm']['api_key']. При загрузке брать ключ приоритетно из переменной окружения, fallback — из файла (но не наоборот).
- **Что даст:** Исключает сохранение секрета на диск; конфигурационный файл становится безопасным для коммита.
- **Заметка верификатора:** Подтверждено по коду: _save_config() (строка 291) пишет self._config.to_dict(), а to_dict() (строки 179-192) включает asdict(self.llm), где LLMConfig.api_key (строка 56). Фильтрация '***HIDDEN***' есть только в export_config() (строки 757-760), а update_config/update_llm_config/import_config/_load_config(default) все вызывают _save_config без неё. Так что ключ действительно ложится в config/streamlit_config.yaml открытым текстом. Понизил до high: по умолчанию api_key='' (строка 56), а основной путь LLM использует системные модели из model_mapping с подключениями через окружение (см. _create_default_config 254-268 и test_llm_connection 1023-1040); ключ попадает на диск только если пользователь сам ввёл его в конфиг, и это локальный файл, а не авто-сбор из env. Реальный secret-at-rest, но не auto-harvest, поэтому high, а не critical.

### H14. XSS: необработанный контент Mermaid и заголовки файлов напрямую вставляются в HTML
- **Модуль:** Ядро: utils + logging + html
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `html_utils.py:633`
- **Проблема:** В `_create_mermaid_container` переменные `title_str` и `creation_time_str` вставляются напрямую в строку HTML (`f'<h3 class="mermaid-title">{title_str} ...'`) без экранирования. `title_str` происходит из имени файла на диске (line 1149: `file_title = f"Файл: {mermaid_file.replace('.mermaid', '')}"`), а имена файлов могут содержать произвольные символы. Аналогично `mermaid_code` вставляется в `<pre>` (line 667) без `html.escape`. Стандартный модуль `html` импортирован (line 5), но нигде не используется — он немедленно затеняется локальной переменной `html = markdown2.markdown(...)` на line 166.
- **Влияние:** Если имя .mermaid-файла содержит `<script>alert(1)</script>`, то при открытии сгенерированного HTML в браузере выполнится произвольный JavaScript. В сценарии, где файлы создаются агентами, обрабатывающими внешние данные, это реальный вектор атаки.
- **Исправление:** Экранировать `title_str`, `creation_time_str` и `mermaid_code` через `html.escape()` перед вставкой в HTML-атрибуты и текстовые узлы. Переименовать локальную переменную `html` (например, `html_str`) чтобы не затенять импортированный модуль.
- **Что даст:** Устраняется XSS через имена файлов и контент диаграмм.
- **Заметка верификатора:** Подтверждено чтением кода. html_utils.py:633 — f'<h3 class="mermaid-title">{title_str} ...' без html.escape; title_str приходит из file_title = f"Файл: {mermaid_file.replace('.mermaid','')}" (1149), то есть из имени .mermaid-файла на диске. mermaid_code также вставляется сырым в <div class="mermaid"> (660) и <pre> (667), причём строка 599 ДЕэкранирует &lt;/&gt;/&amp; обратно в <,>,&. markdown2.markdown() вызывается без safe_mode — проверено эмпирически: <script> проходит насквозь. Модуль html импортирован (5), но затеняется локальной html = markdown2.markdown(...) на 166 и нигде не используется для экранирования. XSS реален при открытии отчёта в браузере. Предусловие: атакующий контролирует имя .mermaid-файла в plots_dir (создаются агентами). high адекватна.

### H15. SSRF: неограниченный fetch произвольных URL из img.src в markdown
- **Модуль:** Ядро: utils + logging + html
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `html_utils.py:332`
- **Проблема:** В `_convert_markdown` код итерирует по `soup.find_all('img')` и делает `requests.get(src, stream=True, timeout=20)` для любого URL, начинающегося с `http://` или `https://` (line 329-332). URL берётся напрямую из markdown-содержимого, которое может быть сформировано агентом или внешним источником. Нет whitelist допустимых хостов, нет проверки на private/loopback диапазоны IP (169.254.x.x, 10.x.x.x, 172.16.x.x, 127.x.x.x), нет ограничения числа запросов.
- **Влияние:** Позволяет отправлять запросы к внутренней инфраструктуре (metadata endpoints облачных провайдеров типа 169.254.169.254, внутренние сервисы), перечислять сети, потенциально получать токены IAM/учётные данные.
- **Исправление:** Добавить проверку URL через ipaddress — запретить private/loopback/link-local адреса. Ввести ограничение на количество встраиваемых изображений на документ. Рассмотреть whitelist разрешённых доменов.
- **Что даст:** Предотвращается SSRF-атака на внутреннюю инфраструктуру через markdown с изображениями.
- **Заметка верификатора:** Подтверждено. html_utils.py:329-347: для любого src, начинающегося с http://|https:// (взятого из <img> markdown-контента), выполняется requests.get(src, stream=True, timeout=20) без allowlist хостов, без проверки private/loopback диапазонов (169.254.169.254, 10/8, 172.16/12, 127/8), без лимита числа запросов. Тело ответа кодируется в base64 и встраивается в отчёт. Дополнительно ветка 365-391 строит absolute_url через urljoin(base_url, src) и тоже делает requests.get. Классический SSRF к metadata-эндпойнтам/внутренним сервисам. high адекватна.

### H16. Path traversal: local img.src читается с диска без ограничения пути
- **Модуль:** Ядро: utils + logging + html
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `html_utils.py:349`
- **Проблема:** Код `elif os.path.exists(src): ... open(src, 'rb')` (lines 349-360) читает локальный файл по пути `src`, взятому прямо из HTML-атрибута `<img>`. Нет проверки, что путь находится внутри разрешённого каталога. Строка `src` из markdown может быть `../../../../etc/passwd`, `/proc/self/environ` и т.д.
- **Влияние:** Произвольное чтение файловой системы сервера и встраивание их содержимого в base64 в HTML-отчёт, который затем может быть передан агенту или пользователю.
- **Исправление:** Проверять `os.path.realpath(src)` и убеждаться, что он начинается с разрешённого базового каталога (например, `plots_dir` или рабочей директории). Все остальные пути — отклонять.
- **Что даст:** Устраняется произвольное чтение файлов сервера через markdown-документ.
- **Заметка верификатора:** Подтверждено. html_utils.py:349-360: elif os.path.exists(src): open(src, 'rb') читает локальный файл по пути src прямо из атрибута <img> markdown-контента. Нет нормализации/проверки, что путь внутри разрешённого каталога — допустимы ../../../etc/passwd, /proc/self/environ и т.п. Содержимое base64-кодируется и встраивается в HTML-отчёт, который может уйти пользователю/агенту — то есть арбитрарное чтение ФС с эксфильтрацией. high адекватна.

### H17. file_system_tools: обход путевой защиты через session_id с os.sep
- **Модуль:** custom_tools (прочие инструменты)
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `custom_tools/file_system_tools.py:44`
- **Проблема:** Логика path-traversal защиты в `file_write`/`file_read` проверяет `normalized_rel.startswith(allowed_root + os.sep)` и отвергает пути с `os.sep` внутри (строки 27-31). Но если `session_id` содержит `../` (например, `../../etc`), то результирующий путь `plots/name_../../etc/passwd` после присвоения `filename_with_session` (строка 50) никак дополнительно не нормализуется и не проверяется. После `os.makedirs(dir_to_create)` файл открывается по пути с `../` без re-нормализации, выходя за пределы `plots/`. Проверка `session_id not in filename` (строка 44) — условие не защитное, оно лишь решает «добавлять ли session_id».
- **Влияние:** Агент с контролем над `session_id` (передаётся из runtime) может записывать/читать файлы произвольно за пределами `plots/`.
- **Исправление:** После формирования `filename_with_session` сделать `os.path.realpath(filename_with_session)` и убедиться, что результат начинается с `os.path.realpath('plots')`. Если нет — вернуть ошибку.
- **Что даст:** Надёжная sandbox-изоляция файловых операций внутри `plots/`.
- **Заметка верификатора:** Подтверждено и эксплуатируемо. session_id объявлен как required:true LLM-параметр в tool_definitions/file_write.yaml и file_read.yaml; agent_factory.load_tools() оборачивает функцию через tool(func), runtime НЕ инжектит session_id — его подаёт сама модель (а значит и prompt injection). Нормализация применяется к filename ДО подстановки session_id (строки 41-50), а filename_with_session не ре-нормализуется. Эмпирически: session_id='../../../../../../tmp/evil_poc' + filename='a.png' даёт путь plots/a_../../../../../../tmp/evil_poc.png, os.path.abspath -> /tmp/evil_poc.png, полный выход за plots/. То же для file_read (чтение произвольных файлов). Произвольная запись/чтение файлов — high обоснован.

### H18. web_tools / web_research: SSRF без ограничений
- **Модуль:** custom_tools (прочие инструменты)
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `custom_tools/web_tools.py:8`
- **Проблема:** `webpage_content(url, ...)` и `http_get(url, ...)` принимают произвольный URL и выполняют HTTP-запрос без валидации адреса назначения. Атакующий может передать `url='http://169.254.169.254/latest/meta-data/'` (AWS IMDSv1), `http://localhost:6379/` (Redis), `http://192.168.x.x/...` — внутренние сервисы. `extract_content_preview_async` в `web_research.py:272` аналогично работает с `allow_redirects=True`.
- **Влияние:** Чтение метаданных облачных инстансов (AWS/GCP/Azure IMDS), проброс запросов во внутреннюю сеть, потенциально секреты из IMDS.
- **Исправление:** Перед запросом проверять, что URL начинается с `https://` или `http://`, а IP адрес назначения не принадлежит RFC-1918 / link-local диапазонам. Либо использовать allowlist доменов, если применимо.
- **Что даст:** Устранение SSRF-вектора.
- **Заметка верификатора:** Подтверждено. webpage_content -> get_clean_text(url) (utils.py:160) делает requests на сырой url, единственная проверка url.startswith('http') — НЕ защищает от http://169.254.169.254/, http://localhost:6379/, internal-IP. http_get (web_tools.py:80) — requests.get(url,...) вовсе без проверки адреса. web_research.extract_content_preview_async (web_research.py:272-274) — aiohttp session.get с allow_redirects=True, без валидации. Все три — агентские тулы (http_get.yaml, webpage_content.yaml, web_research.yaml), url — LLM-контролируемый. Нет блок-листа internal/link-local/metadata-адресов, нет защиты от DNS-rebinding/redirect-to-internal. Классический SSRF, high обоснован.

### H19. image_tools.analyze_image_tool: SSRF через URL-изображения без валидации
- **Модуль:** custom_tools (прочие инструменты)
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `custom_tools/image_tools.py:869`
- **Проблема:** При `input_type='url'` (включая `auto`-детект по `startswith('http://', 'https://')`) URL передаётся напрямую в `call_openai_api(image_url=image_input)`. Это отправляет произвольный URL на серверы OpenAI-совместимого API, что является SSRF если API поддерживает fetching по URL. Более того, при `input_type='auto'` условие `elif os.path.exists(image_input)` (строка 827) выполняется до проверки на base64, что позволяет передать локальный файловый путь и прочитать его содержимое.
- **Влияние:** При `input_type='path'` агент читает произвольный файл (строки 840-842: `open(image_input, 'rb')`). Нет ограничения на допустимые пути — файл может быть `/etc/passwd`, `mcp_servers.json`, `.env`.
- **Исправление:** Для `input_type='path'` нормализовать путь через `os.path.realpath` и убедиться, что он находится внутри разрешённой директории. Для `input_type='url'` — применить те же SSRF-ограничения, что и в `web_tools`.
- **Что даст:** Устранение чтения произвольных файлов и SSRF через image analysis.
- **Заметка верификатора:** Реальная проблема — локальное чтение произвольного файла, не SSRF. SSRF-часть слабая: при input_type='url' URL уходит в call_openai_api как {'type':'image_url','image_url':{'url':...}} (utils.py:708-719) — фетчит его апстрим-LLM-провайдер на СВОЕЙ сети, не это приложение, так что это не SSRF самого сервиса. НО подтверждённый баг: при input_type='path' (и при 'auto', где os.path.exists() проверяется раньше base64, строки 824-832) код делает open(image_input,'rb') (строки 836-842) на произвольном пути без ограничения каталога, кодирует в base64 и отправляет наружу через vision-API. image_input — LLM-контролируемый параметр (analyze_image.yaml, service.py:3861/presets.image.analyze, 09_Tools.py). Чтение /etc/passwd, .env, mcp_servers.json с эксфильтрацией к внешнему API — high обоснован.

### H20. Профиль безопасности default имеет пустой forbidden_functions — обход движково-специфичных запретов
- **Модуль:** Text2SQL: ядро + NLU
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `/srv/git_projects/MultiAgent/config/text_to_sql/safety.yaml:71`
- **Проблема:** Дефолтный профиль (`TEXT_TO_SQL_SAFETY_PROFILE` не задан) загружает `forbidden_functions: []`. Функции типа `pg_sleep`, `lo_import`, `xp_cmdshell`, `load_file`, `information_schema.*` не заблокированы. AST-проверка `check_forbidden_functions_ast` в `_SqlglotValidator` (safety.py:656) полностью пропускается при пустом `self.forbidden_functions` (строка 673: `if not self.forbidden_functions: return False`). В `.env` нет `TEXT_TO_SQL_SAFETY_PROFILE=extended`, то есть production работает на профиле `default`.
- **Влияние:** Атакующий может вызвать `SELECT pg_sleep(30)` (DoS), `SELECT load_file('/etc/passwd')` (LFI), `SELECT * FROM information_schema.tables` (разведка схемы). Аудит 2026-05 уже зафиксировал эту проблему как critical (deny-list forbidden_functions мёртв при USE_SQLGLOT=1).
- **Исправление:** Выставить `TEXT_TO_SQL_SAFETY_PROFILE=extended` в production окружении. Добавить проверку в startup-валидацию приложения: если профиль `default` и `forbidden_functions` пуст — emit warning/fail-fast.
- **Что даст:** Активация движково-специфичных запретов на опасные функции.
- **Заметка верификатора:** Подтверждено фактически. safety.yaml:71 default-профиль имеет forbidden_functions: []; safety_config._resolve_profile_name возвращает 'default' при unset TEXT_TO_SQL_SAFETY_PROFILE (в .env его нет); safety.py:673 check_forbidden_functions_ast делает `if not self.forbidden_functions: return False` — т.е. AST-deny-list функций на default полностью неактивен. Смягчающие факторы: forbidden_keywords и ast_forbidden_stmt_classes на default ВСЁ ЕЩЁ блокируют DML/DDL (мутации не проходят), а часть примеров атак (pg_sleep/load_file/xp_cmdshell) — Postgres/MySQL/MSSQL-функции, отсутствующие в prod-движке DuckDB. НО реальный вектор остаётся: information_schema-разведка (DuckDB её поддерживает) и DoS/опасные нативные функции при USE_SQLGLOT=1 на SELECT-only поверхности проходят без запрета. Эта же проблема ранее фиксировалась как critical/high и формально 'закрыта' только AST-кодом, который не активируется на default-профиле. High обоснован (read-only поверхность ограничивает мутации, но recon/DoS реальны).

### H21. Профиль default не блокирует forbidden_functions — нулевая защита от file I/O / OS команд без явного выбора профиля
- **Модуль:** Text2SQL: валидаторы безопасности
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. critical→high
- **Где:** `config/text_to_sql/safety.yaml:71 / custom_tools/text_to_sql/validators/safety_config.py:33`
- **Проблема:** Дефолтный профиль (`_DEFAULT_PROFILE = "default"`) содержит `forbidden_functions: []`. Если `TEXT_TO_SQL_SAFETY_PROFILE` не выставлен явно, все инсталляции работают с пустым deny-list функций: `pg_sleep`, `load_file`, `xp_cmdshell`, `dblink`, `current_user`, ClickHouse-функции `url`/`s3`/`file` и прочие опасные конструкции не блокируются ни regex-, ни AST-маршрутом. Комментарий в yaml называет это «product-решением», но де-факто это fail-open: достаточно одного незаконфигурированного деплоя.
- **Влияние:** Запросы вида `SELECT load_file('/etc/passwd')` или `SELECT pg_read_file('/etc/passwd')` проходят статический валидатор и попадают на исполнение в БД. Атакующий получает file-read / OS-команды / утечку каталогов без каких-либо барьеров со стороны QA-слоя.
- **Исправление:** Сделать `extended` профилем по умолчанию: изменить `_DEFAULT_PROFILE = "default"` на `"extended"` в safety_config.py. Если по архитектурным соображениям нужен движко-нейтральный минимальный профиль, добавить в `default` хотя бы os-exec функции (`xp_cmdshell`, `load_file`, `lo_import`, `pg_read_file`) и конфигурационные чтения (`current_user`, `information_schema`). Документировать требование явной установки `TEXT_TO_SQL_SAFETY_PROFILE=extended` в checklists деплоя.
- **Что даст:** Закрывает основной вектор data-exfiltration и OS-escape для любого деплоя, не выставившего профиль вручную.
- **Заметка верификатора:** Факт подтверждён кодом. safety_config.py:33 _DEFAULT_PROFILE='default', и при невыставленном TEXT_TO_SQL_SAFETY_PROFILE _resolve_profile_name (строки 135-143) возвращает 'default'. В safety.yaml:71 у профиля default forbidden_functions: []. Оба маршрута проверки функций становятся no-op на пустом списке: regex check_forbidden_functions (safety.py:459 — цикл по пустому tuple) и AST check_forbidden_functions_ast (safety.py:673-674: `if not self.forbidden_functions: return False`). Значит SELECT load_file(...) / SELECT pg_read_file(...) не дают FORBIDDEN_FUNCTION при ЛЮБОМ USE_SQLGLOT, если профиль default. Это реальный fail-open для функционального deny-list. Понижаю critical->high: (1) это явно документированное product-решение (default = deny-by-default минимум, движко-агностичный; extended — рекомендуемый прод-профиль); (2) DML/DDL по-прежнему блокируются через forbidden_keywords и ast_forbidden_stmt_classes — валидатор не «полностью открыт», брешь только в function-level deny-list; (3) для эксплуатации нужен оператор, не выставивший рекомендованный профиль. Тем не менее экспозиция file-read/exfiltration на дефолтном деплое реальна, поэтому не ниже high.

### H22. SSRF через urllib.request.urlretrieve без ограничений
- **Модуль:** Backend FastAPI / AG-UI
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `backend/fastapi_app/agui/service.py:2462`
- **Проблема:** _download_url_to_file вызывает urllib.request.urlretrieve(url, dest) без какой-либо проверки схемы, хоста или диапазона IP. url приходит напрямую из payload (service actions presets.image.edit и presets.image.edit_batch), без валидации. Любой клиент может передать url=http://169.254.169.254/... или url=file:///etc/passwd, что даст SSRF/чтение локальных файлов.
- **Влияние:** Атакующий может получить данные из metadata-сервиса cloud-окружения (например, AWS instance metadata) или прочитать файлы с диска, если ОС не ограничивает file:// схему в urlretrieve.
- **Исправление:** Перед вызовом urlretrieve проверять parsed.scheme in {'http', 'https'}, parsed.hostname не в диапазонах RFC 1918/loopback/link-local. Использовать requests с allow_redirects=False и stream=True вместо urlretrieve.
- **Что даст:** Закрывает SSRF и чтение локальных файлов через URL-ввод.
- **Заметка верификатора:** Подтверждено. service.py:2462-2468 _download_url_to_file вызывает urllib.request.urlretrieve(url, dest) без проверки схемы/хоста/IP. url приходит из payload в presets.image.edit (service.py:3786-3787) и presets.image.edit_batch (3835-3837). Эмпирически проверил: urlretrieve успешно читает file:///etc/hostname (вернул содержимое) и поддерживает http(s) к любому хосту, включая 169.254.169.254. Путь достижим без авторизации: POST /agent -> run_agent -> handle_service_action, service_action/payload берутся напрямую из forwarded_props (runner.py:539,599). App слушает 0.0.0.0:8000 (run_dev.sh:8) -> доступно из сети. High корректна (SSRF + локальное чтение файлов через file://).

### H23. files.read и files.read_base64 дают произвольное чтение файлов внутри проекта
- **Модуль:** Backend FastAPI / AG-UI
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `backend/fastapi_app/agui/service.py:3992`
- **Проблема:** files.read / files.read_base64 принимают path из payload, конкатенируют с project_root и защищены только _ensure_within_root. Это позволяет прочитать logs/db_test_config_secrets.json (plaintext DSN-секреты), любой YAML агентского профиля с API-ключами, agui_events.db (все события всех сессий). Нет whitelist допустимых базовых директорий и нет авторизации.
- **Влияние:** Чтение файла db_test_config_secrets.json даёт полные DSN со всеми credentials для всех подключённых БД.
- **Исправление:** Добавить whitelist разрешённых поддиректорий (например только 'output', 'plots'). Либо ввести авторизацию для admin actions.
- **Что даст:** Предотвращает чтение секретов и произвольных файлов проекта.
- **Заметка верификатора:** Подтверждено (с уточнением границ). service.py:3992-4004: path из payload, file_path = _ensure_within_root(_project_root()/path), затем read_text/read_bytes. _ensure_within_root блокирует выход за корень (проверил '../../../etc/passwd' -> BLOCKED), поэтому 'любой файл на диске' преувеличено. Но в пределах корня /srv/git_projects/MultiAgent реально читаются: .env (содержит JINA_API_KEY, OPENAI_API_KEY_DB, VSEGPT_API_KEY, KLING_API_KEY, CLOUD_API_KEY и др.), workflow_state.db.secrets.json, logs/, data/agui_events.db. Авторизации нет, эндпоинт на 0.0.0.0. Чтение DSN/API-ключей -> компрометация credentials. High корректна.

### H24. Отсутствие авторизации на всех эндпоинтах и service actions
- **Модуль:** Backend FastAPI / AG-UI
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `backend/fastapi_app/main.py:60`
- **Проблема:** POST /agent, POST /v1/runs, GET /agent/{run_id}/events, DELETE через cancel — ни один эндпоинт не требует авторизации (никакого Bearer, API-key, session check). Через service_action любой клиент в сети может вызвать system.diagnostics, db.comprehensive_test (с реальным DSN), files.read, memory.full_cleanup, agents.run и т.п. CORS разрешает все локальные origins, но не защищает от прямых запросов (curl, Postman, другой сервер в той же сети).
- **Влияние:** Полный доступ к admin-операциям без credentials: очистка памяти, чтение файлов, запуск агентов, деструктивные операции с БД.
- **Исправление:** Добавить хотя бы static API-key middleware (X-API-Key header или Bearer), конфигурируемый через env. Для production — полноценный authz.
- **Что даст:** Базовая защита от несанкционированного доступа.
- **Заметка верификатора:** Подтверждено. main.py: ни один эндпоинт (/agent, /v1/runs, /agent/{id}/events, /cancel, /result) не имеет Depends с auth, нет HTTPBearer/api-key/token (grep пуст). service_action и service_payload берутся прямо из forwarded_props без проверок (runner.py:539,569,599) -> доступны system.diagnostics, db.comprehensive_test, files.read/list, memory.full_cleanup, agents.run и деструктивные операции. CORS (main.py:46-52) ограничивает только браузерные origins и не защищает от прямых curl/Postman. App слушает 0.0.0.0:8000 (run_dev.sh:8) -> любой хост в сети. Это корневой enabler находок 1-3. High корректна.

### H25. Impala/SAP IQ: SQL-инъекция через неэкранированные идентификаторы в нескольких местах
- **Модуль:** DB Plugins
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. critical→high
- **Где:** `db_plugins/impala.py:390,393`
- **Проблема:** В get_fk_preview() строки ref_schema и ref_table_only вставляются в SQL без quote_identifier: `cur.execute(f"USE {ref_schema}")` (строка 390) и `cur.execute(f"DESCRIBE {ref_table_only}")` (строка 393). ref_schema берётся напрямую из параметра вызова, а не из information_schema — никакой нейтрализации нет. Аналогичная незащищённая USE выполняется в estimate_row_count (строка 183) и get_basic_column_stats (строка 226).
- **Влияние:** Атакующий, контролирующий имя схемы или таблицы (например, через LLM-сгенерированный ref_table), может выполнить произвольный HiveQL/Impala DDL/DML командой вида `USE legitimate_db; DROP TABLE x --`.
- **Исправление:** Использовать self.quote_identifier() для всех идентификаторов: `cur.execute(f"USE {self.quote_identifier(ref_schema)}")` и `cur.execute(f"DESCRIBE {self.quote_identifier(ref_table_only)}")`. Аналогично исправить строки 183, 226.
- **Что даст:** Закрывает самый простой вектор инъекции в read-only-недоступных плагинах, где сессия и так неограниченная (fail_open).
- **Заметка верификатора:** Частично подтверждается, но находка существенно искажена. РЕАЛЬНО неэкранированы только impala.py:390 (cur.execute(f"USE {ref_schema}")) и :393 (cur.execute(f"DESCRIBE {ref_table_only}")) — здесь quote_identifier не вызывается, в отличие от join_sql ниже (строки 423-424), где он есть. НО: (1) утверждение про 'аналогичную незащищённую USE в строках 183 и 226' ЛОЖНО — обе используют self.quote_identifier(schema_name); (2) заголовок про 'SAP IQ ... в нескольких местах' ЛОЖЕН — sapiq.py квотирует всё (строки 261,325,360,481,482). Источник ref_schema/ref_table — поле references из метаданных схемы; для Impala introspect_schema всегда ставит references='' (Impala не отдаёт FK), поэтому _get_fk_previews для Impala в норме даже не дойдёт до этого кода. Достижимо лишь через подменённую/загруженную из конфига схему И при read_only_fail_open=true (иначе connect отказывает). Реальный баг (отступление от контракта quote_identifier), но не critical: понижено до high.

### H26. Impala: unquoted table_name в COUNT(*) и sample_sql создаёт SQL-инъекцию
- **Модуль:** DB Plugins
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. critical→high
- **Где:** `db_plugins/impala.py:200,265`
- **Проблема:** В estimate_row_count: `count_sql = f"SELECT COUNT(*) FROM {table_name} LIMIT 1"` — table_name не квотируется (строка 200). В get_basic_column_stats: `sample_sql = f"SELECT DISTINCT {q_col} FROM {table_name} WHERE {q_col} IS NOT NULL LIMIT {sample_limit}"` — table_name тоже не квотируется (строка 265), хотя q_col правильно квотирован. В sample_rows_smart последний fallback: `sql = f"SELECT * FROM {table_name} LIMIT {max_rows}"` (строка 345) — то же самое.
- **Влияние:** Если table_name содержит `; DROP TABLE ... --` или подобное, запрос выполнится, так как Impala не имеет read-only enforcement по умолчанию (сессия writable при fail_open=true).
- **Исправление:** Заменить на `self.quote_identifier(table_name)` во всех трёх местах.
- **Что даст:** Устраняет SQL-инъекцию в наиболее критичном плагине без read-only enforcement.
- **Заметка верификатора:** Подтверждается фактически: impala.py:200 (SELECT COUNT(*) FROM {table_name}), :254/:265 (stats_sql/sample_sql FROM {table_name} — q_col квотирован, а table_name нет), :345 (fallback SELECT * FROM {table_name}) — везде table_name вставляется без quote_identifier, что нарушает установленный в проекте контракт безопасности идентификаторов (ср. schema_enricher.py:440, где явно делается quote_identifier 'cannot safely build SQL with identifier'). НО table_name приходит из ключей schema_obj, т.е. из каталога БД/конфига схемы, а не из свободного пользовательского ввода; эксплуатация требует контроля над именами в каталоге/схеме И read_only_fail_open=true (иначе Impala-соединение отклоняется). Реальный баг, но это second-order вектор, не прямой пользовательский — critical завышен, корректно high.

### H27. Hardcoded GCP project ID в исходном коде
- **Модуль:** Storybook: видео/shots-генераторы
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `custom_tools/storybook/video_generator_veo_tool.py:147`
- **Проблема:** Дефолтное значение `os.getenv('GOOGLE_CLOUD_PROJECT', 'gen-lang-client-0611452273')` содержит реальный project ID в исходном коде. Если переменная GOOGLE_CLOUD_PROJECT не задана, код молча использует захардкоженный project. В строке 156 условие `if project_id_vertex:` всегда True (строка из getenv с дефолтом никогда не вернёт None/пустую строку при наличии дефолта), что означает: даже без какой-либо конфигурации код пытается поднять Vertex AI клиент с чужим project ID.
- **Влияние:** Утечка идентификатора GCP-проекта в репозиторий; при развёртывании без явного GOOGLE_CLOUD_PROJECT все запросы идут на захардкоженный project — непредсказуемые ошибки авторизации или биллинга.
- **Исправление:** Убрать дефолт из getenv: `os.getenv('GOOGLE_CLOUD_PROJECT')`. Добавить явную проверку и ранний возврат ошибки, если ни GOOGLE_CLOUD_PROJECT, ни GEMINI_API_KEY не заданы.
- **Что даст:** Устранение утечки credentials в код, корректный fallback на API-key режим.
- **Заметка верификатора:** Подтверждено чтением кода. video_generator_veo_tool.py:147 — `project_id_vertex = os.getenv('GOOGLE_CLOUD_PROJECT', 'gen-lang-client-0611452273')`, формат `gen-lang-client-*` это реальный GCP project ID, выдаваемый Google AI Studio/Vertex, захардкожен как дефолт и закоммичен в репо (grep подтвердил единственное вхождение в исходниках). Логика тоже подтверждена: на строке 156 `if project_id_vertex:` всегда True при незаданном GOOGLE_CLOUD_PROJECT (getenv с непустым дефолтом возвращает truthy-строку), из-за чего ветки `elif api_key`/`else` мертвы, выставляется use_vertex=True, и значение напрямую утекает в genai.Client(vertexai=True, project=project_id, location=...) на строках 280-284 через _generate_single_video_veo. Severity high адекватна: идентификатор GCP-проекта в исходном коде — это утечка инфраструктурного идентификатора и сбивающий с толку control flow. Единственный нюанс (не понижающий вердикт): сам по себе project ID без ambient GCP credentials (ADC) не приведёт к молчаливому биллингу — клиент/вызов упадёт на этапе авторизации; часть impact про 'все запросы идут на чужой project' реализуется только если у деплоя есть валидные ADC. Тем не менее находка реальна и severity сохраняю high.

### H28. iframe с backend-HTML без атрибута sandbox
- **Модуль:** Frontend (Next.js / React AG-UI)
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `frontend/client/src/app/components/sections/TextToSqlSection.tsx:1178`
- **Проблема:** HTML-отчёт, полученный с backend-а, декодируется из base64/gzip и монтируется в blob-URL, который затем загружается в <iframe> без атрибута sandbox. То же — в AgentsSection.tsx:949, WorkflowsSection.tsx:1186, DynamicAgentsSection.tsx:1277. Blob-URL наследует origin страницы (`blob:http://localhost:3000/...`), поэтому скрипты внутри iframe выполняются в том же origin, имеют полный доступ к window.parent, localStorage и могут вызывать runServiceAction через parent, если backend вернёт вредоносный HTML.
- **Влияние:** Если backend скомпрометирован или workflow возвращает HTML с <script>, злоумышленник получает полный доступ к странице-хосту: кража localStorage (шаблоны с DSN-строками), вызов любого service-action, включая db.test_configs.delete и config.update_section.
- **Исправление:** Добавить sandbox="allow-same-origin" (или более строгий — без allow-same-origin, с allow-popups для скачивания) на все четыре iframe. Рассмотреть генерацию blob-URL без inherit-origin: использовать URL.createObjectURL с type: 'text/html' и выставить Content-Security-Policy на основном приложении.
- **Что даст:** Блокирует выполнение скриптов из backend-HTML в origin хост-страницы.
- **Заметка верификатора:** Подтверждено. В TextToSqlSection.tsx:476-485 HTML декодируется из base64/gzip (decodeGzipBase64), оборачивается в Blob с mime_type по умолчанию 'text/html' и URL.createObjectURL → reportPreviewUrl, который монтируется в <iframe src={reportPreviewUrl}> на строке 1178 БЕЗ атрибута sandbox. Тот же паттерн без sandbox проверен в AgentsSection.tsx:949-953, WorkflowsSection.tsx:1186-1190, DynamicAgentsSection.tsx:1277-1281. Blob-URL наследует origin страницы, поэтому <script> внутри HTML исполняется same-origin и имеет доступ к window.parent, document, localStorage, cookies. Реальный XSS-sink при вредоносном HTML от backend/workflow. Одна мелкая неточность в формулировке находки: runServiceAction — module-scoped функция, не висит на window, поэтому напрямую её вызвать нельзя; но кража localStorage и манипуляция DOM родителя (в т.ч. триггер деструктивных UI-действий) полностью реальны. High обоснован — это межсервисная граница доверия в multi-agent системе.

### H29. DSN (включая пароль БД) попадает в историю Text-to-SQL в браузере
- **Модуль:** Frontend (Next.js / React AG-UI)
- **Категория:** security | **Уверенность:** high | **Верификация:** подтверждено
- **Где:** `frontend/client/src/app/components/sections/TextToSqlSection.tsx:402`
- **Проблема:** В loadRunStatus при сохранении записи истории поле dsn берётся из runMeta или из state-переменной dsn (строка ~409: `dsn: runMeta?.dsn ?? dsn`). Это полная DSN-строка, введённая пользователем (вида `postgresql://user:password@host/db`). Эта строка отправляется в `text_to_sql.history.append` и также отображается напрямую в history-карточке (строка 1074: `{item.dsn ?? '—'}`). Пароль оседает как в backend-хранилище истории, так и в отображаемом UI.
- **Влияние:** Компрометация backend или перехват трафика раскрывает credentials БД всех пользовавшихся функцией. UI так же показывает DSN всем, кто смотрит на экран.
- **Исправление:** Перед сохранением в историю маскировать пароль в DSN (regex замена `://user:pass@` → `://user:***@`). В UI показывать masked_dsn, если доступен. Проверить, что backend не логирует DSN целиком.
- **Что даст:** Credentials базы данных не будут сохранены в истории запросов.
- **Заметка верификатора:** Подтверждено. Поле dsn вводится пользователем целиком с паролем — placeholder 'scheme://user:pass@host/db' (строки 639, 904, 971). runMeta.dsn устанавливается из сырого state dsn (строка 187), без маскирования. В loadRunStatus на строке 409 сырая DSN (runMeta?.dsn ?? dsn) отправляется в text_to_sql.history.append, и на строке 1074 рендерится открытым текстом в карточке истории {item.dsn ?? '—'}. Маскирование (masked_dsn, проверки на '***'/'<redacted>') существует для saved connections (строки 265, 930, 939-943), но к history-append и history-рендеру НЕ применяется. Пароль БД оседает в backend-истории и виден в UI. High обоснован.

### H30. XSS: необработанный processed_result вставляется в HTML-тело напрямую
- **Модуль:** Ядро: utils + logging + html
- **Категория:** security | **Уверенность:** medium | **Верификация:** подтверждено
- **Где:** `html_utils.py:1121`
- **Проблема:** В `advanced_visualization` строка `processed_result` (результат workflow-агента, потенциально содержащий данные из внешних источников) вставляется как `f'        {processed_result}'` внутрь `<div class="markdown-body">` без какой-либо санитизации (line 1119-1123). Хотя перед этим `_detect_and_save_mermaid` вызывает `_convert_markdown`, который проходит через BeautifulSoup, итоговый результат — строка HTML, прошедшая через несколько преобразований с частичным восстановлением «оригинального» содержимого контейнеров Mermaid, что позволяет обходить фильтрацию через плейсхолдеры.
- **Влияние:** Вредоносный LLM-ответ или входные данные агента могут внедрить JavaScript в сохраняемый HTML-файл.
- **Исправление:** Если предполагается доверенный контент, задокументировать это явно. Если нет — использовать bleach/nh3 для санитизации или отдельный слой авторизации при отдаче файла.
- **Что даст:** Снижается риск stored XSS в генерируемых отчётах.
- **Заметка верификатора:** Подтверждено. html_utils.py:1119-1123 вставляет f'        {processed_result}' в <div class="markdown-body"> без санитизации. processed_result = _detect_and_save_mermaid(result, ...) (830), который прогоняет текст через _convert_markdown (markdown2 + BeautifulSoup), но markdown2 БЕЗ safe_mode не экранирует сырой HTML (проверено: '<script>alert(1)</script>' проходит дословно). Более того, _convert_markdown на 408-428 после str(soup) делает простые строковые replace, восстанавливая сырые mermaid-контейнеры/плейсхолдеры в обход парсера. Нигде в конвейере нет html.escape или bleach. Вредоносный ответ агента/входные данные внедряют JS в сохраняемый HTML. high адекватна.


## 🟡 MEDIUM (143)

### M1. Небезопасное использование globals() как хранилища состояния в mcp_tools.py
- **Модуль:** Ядро: tool manager + MCP
- **Категория:** architecture | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `mcp_tools.py:116`
- **Проблема:** `globals()['mcp_server_metadata'] = server_metadata` использует глобальное пространство имён модуля как замену нормальной переменной или класса. Функции `get_server_info()` и `list_active_servers()` читают через `globals().get('mcp_server_metadata', {})`. Это антипаттерн: при reload модуля или в тестах состояние непредсказуемо. `import random` в начале файла не используется нигде.
- **Влияние:** Хрупкость при тестировании, трудность трассировки состояния, скрытые зависимости.
- **Исправление:** Объявить `mcp_server_metadata: dict = {}` как модульную переменную и использовать её напрямую. Удалить неиспользуемый `import random`.
- **Что даст:** Читаемый и предсказуемый код без магии через globals().

### M2. mcp_tools.py выполняет подключение к MCP-серверам при импорте модуля
- **Модуль:** Ядро: tool manager + MCP
- **Категория:** architecture | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `mcp_tools.py:153-173`
- **Проблема:** Код в строках 153-173 выполняется на уровне модуля: `load_mcp_servers_from_json("mcp_servers.json")` и цикл `MCPClient(server)` запускаются при каждом `import mcp_tools`. Это означает: (1) subprocess запускается во время импорта; (2) если `mcp_servers.json` недоступен — импорт падает с необработанным исключением на строке 153 (нет try/except вокруг `load_mcp_servers_from_json`); (3) путь `"mcp_servers.json"` жёстко задан как относительный — зависит от текущего каталога процесса.
- **Влияние:** Падение при импорте модуля если конфиг не найден. Сложность тестирования. Subprocess при импорте замедляет запуск.
- **Исправление:** Обернуть модульный код в `try/except` или в функцию `initialize_mcp_tools()` с явным вызовом. Принимать путь к конфигу как параметр или через переменную окружения.
- **Что даст:** Предсказуемая инициализация, testability, graceful degradation при отсутствии конфига.

### M3. Monkey-patch logging.getLogger без возможности отмены и без защиты от повторного применения
- **Модуль:** Ядро: utils + logging + html
- **Категория:** architecture | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `unified_logging.py:725`
- **Проблема:** `_setup_log_handler` патчит `logging.getLogger` глобально (`logging.getLogger = _patched_get_logger`) и выставляет флаг `logging._unified_logging_patched` на самом модуле `logging`. Это: (1) не поддаётся откату (`shutdown()` не восстанавливает оригинальный `logging.getLogger`), (2) ломает изоляцию тестов, (3) `_original_get_logger` сохраняется на уровне модуля `unified_logging.py` (line 1022), что означает: при импорте модуля до вызова `get_logging_manager`, `_original_get_logger` захватывает оригинал, но если модуль импортируется после другого патча — цепочка ломается.
- **Влияние:** Тесты, импортирующие любой модуль, который тянет unified_logging, получают поломанный `logging.getLogger` без возможности восстановления. В продакшне `shutdown()` оставляет патч активным.
- **Исправление:** Хранить ссылку на оригинал внутри `UnifiedLoggingManager` и восстанавливать её в `shutdown()`. Рассмотреть использование `logging.setLogRecordFactory` или кастомного `logging.Filter` вместо патча функции.
- **Что даст:** Улучшается изолируемость тестов и предсказуемость поведения при завершении работы.

### M4. Дублирование логики сбора video_items в трёх файлах
- **Модуль:** Storybook: видео/shots-генераторы
- **Категория:** architecture | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/video_generator.py:145-205 / video_generator_mm_tool.py:133-218 / video_generator_veo_tool.py:166-220`
- **Проблема:** Логика фильтрации и сбора кадров для генерации видео (проверка shot_type=start, поиск start/end изображений по шаблону img_final_{start|end}_{NN}_{NN}.png, дедупликация по scene-shot ключу) продублирована с небольшими вариациями в трёх файлах. В video_generator_aitunnel_tool.py она вынесена в `_collect_video_items` — правильный подход, остальные его не используют. В video_generator.py вдобавок отсутствует дедупликация по seen_shots (есть в mm/veo/aitunnel), что может привести к двойной генерации.
- **Влияние:** Правка бага в логике поиска изображений требует изменений в 3 местах. Отсутствие дедупликации в video_generator.py — потенциальный баг при дублирующихся items.
- **Исправление:** Перенести `_collect_video_items` из aitunnel_tool в video_generator_common и переиспользовать во всех инструментах.
- **Что даст:** Единое место для правок, устранение потенциального двойного запуска генерации.

### M5. Вся бизнес-логика и состояние приложения в одном компоненте AguiStudio (god-component)
- **Модуль:** Frontend (Next.js / React AG-UI)
- **Категория:** architecture | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `frontend/client/src/app/page.tsx:160`
- **Проблема:** Компонент AguiStudio содержит 1650+ строк: весь стейт агентов, воркфлоу, БД, памяти, все callback-хандлеры, логику опроса, очередь service actions. Все дочерние секции получают >40 пропсов каждая (AgentsSection — более 40 пропсов на строке 1400-1446). При каждом обновлении любого state пересчитываются все memoized values и re-renders всех подключённых компонентов.
- **Влияние:** Сложно поддерживать, тестировать и отлаживать. Пропс-дриллинг на 40+ параметров делает рефакторинг трудоёмким. Производительность: любое изменение состояния агентов вызывает re-render всей страницы.
- **Исправление:** Вынести состояние в отдельные custom hooks (useAgentState, useWorkflowState) или контексты. Сечение по доменам уже существует в виде секций — каждая секция должна управлять своим состоянием самостоятельно, как DynamicAgentsSection (самодостаточный).
- **Что даст:** Сопровождаемость, изолированные re-renders, тестируемость.

### M6. LLM safety audit блокирует SQL при таймауте (fail-closed), но кэш не наполняется — повторные запросы всегда запускают LLM
- **Модуль:** Text2SQL: ядро + NLU
- **Категория:** architecture | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `/srv/git_projects/MultiAgent/custom_tools/text_to_sql/core/_sql_generation_api.py:637`
- **Проблема:** В `sql_safety_check()` при TimeoutError LLM-аудита запрос помечается `is_safe=False` (строка 643) и кэш не заполняется. При повторном запросе того же SQL в пул снова отправляется LLM-задача. Если LLM-эндпоинт деградирует (медленный, но не упавший), каждый запрос к популярному SQL будет timeout'иться: пользователь получает отказ бесконечно, LLM-пул остаётся перегруженным.
- **Влияние:** При деградации LLM-эндпоинта pipeline полностью останавливается — ни один SQL не будет выполнен, даже SELECT 1. Это не безопасный degraded mode, а полная остановка сервиса.
- **Исправление:** Добавить отдельный negative-TTL кэш для timeout-результатов (например, 60 с). За это время повторные запросы того же SQL не порождают новых LLM-задач. Альтернатива: circuit breaker поверх `_LLM_SAFETY_AUDIT_EXECUTOR` — при N timeout'ах подряд переходить в bypass-режим с явным advisory-предупреждением.
- **Что даст:** Устойчивость сервиса при деградации LLM-эндпоинта.

### M7. factory.agents и agent_pool не очищаются между вызовами coordinate() — утечка состояния сессий
- **Модуль:** Ядро: оркестрация агентов
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `agent_factory.py:187 / agent_system.py:38,214`
- **Проблема:** `AgentFactory.__init__` инициализирует `self.agents = []`, но список никогда не очищается между последовательными вызовами `coordinate()` на одном и том же экземпляре. В `agent_streamlit_api.py:563-564` создаётся один экземпляр `self.factory = AgentFactory()` и `self.agent_system = DynamicAgentSystem()`, и потом многократно вызывается `coordinate()`. При каждом новом вызове агенты из предыдущих запросов накапливаются в `factory.agents`, и менеджер при создании (`_get_managed_agents`) получает агентов всех предыдущих сессий как `managed_agents`.
- **Влияние:** Менеджер сессии N будет делегировать задачи агентам сессий 1..N-1 с чужими `session_id` и `rag_memory`, что приведёт к смешению данных разных пользователей, неопределённому поведению и утечке контекста. Также неограниченный рост памяти процесса.
- **Исправление:** Добавить сброс состояния фабрики перед каждым `coordinate()`: `self.factory.agents.clear(); self.agent_pool.clear()`. Либо создавать новый `AgentFactory()` и `DynamicAgentSystem()` на каждый запрос (как делается в `runner.py:684`).
- **Что даст:** Изоляция сессий, предотвращение утечки данных между пользователями.
- **Заметка верификатора:** Механизм частично реален: agent_factory.py:367 self.agents.append(agent) нигде не очищается, а _get_managed_agents (762-783) возвращает list(self.agents) — ВСЕХ накопленных агентов. НО заявленный прод-импакт (manager сессии N получает агентов сессий 1..N-1, смешение данных разных пользователей) НЕ воспроизводится на основном пути: run_manager_with_team (agent_streamlit_api.py:1167) и run_agent (718) запускают каждый прогон в ОТДЕЛЬНОМ multiprocessing.Process, а _manager_team_process_entry (404) и _agent_process_entry создают СВЕЖИЙ AgentManager() (стр. 413) -> свежий DynamicAgentSystem -> свежий AgentFactory на каждый запуск. Кросс-сессионная утечка возможна лишь в редком error-fallback в поток (1213-1216) на singleton либо при прямом переиспользовании одного DynamicAgentSystem. Это код-смелл и риск роста памяти при многократном coordinate() на одном инстансе, но не критичная межпользовательская утечка в проде. Понижено до medium.

### M8. SIGALRM timeout несовместим с async и ломает весь процесс
- **Модуль:** Ядро: LLM-модель + codeinterpreter
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `codeinterpreter.py:41-56`
- **Проблема:** Контекстный менеджер `timeout()` использует `signal.SIGALRM` и `signal.alarm()`. SIGALRM работает только в главном потоке. `_execute_code()` вызывается из async-контекста (через await). В asyncio-приложении главным потоком является event loop, но если `_execute_code` запущен в executor или из другого потока — `signal.signal()` поднимет `ValueError: signal only works in main thread`. Кроме того, `TimeoutException` из обработчика сигнала прервёт не только exec(), но и любую другую операцию, которая выполнялась в момент сигнала (например, IO операцию asyncio), что может привести к corrupted state.
- **Влияние:** Падение приложения при конкурентном использовании; непредсказуемое прерывание IO-операций asyncio; timeout фактически не работает в non-main thread.
- **Исправление:** Запускать exec() в отдельном процессе (multiprocessing) с join(timeout=...) и terminate(), или использовать asyncio.wait_for() + run_in_executor для CPU-bound кода. Не использовать SIGALRM в async-коде.
- **Что даст:** Надёжный timeout, совместимый с async; предотвращение hang при выполнении долгого кода.
- **Заметка верификатора:** Частично подтверждено. timeout() (41-56) использует signal.SIGALRM/alarm, который работает только в главном потоке. _execute_code — async; @tool-обёртки вызывают asyncio.run() (в потоке вызывающего). Если smolagents исполняет инструмент в worker-потоке ThreadPoolExecutor, signal.signal поднимет ValueError — реальная хрупкость. Но в боевом потоке (asyncio.run в main thread) SIGALRM работает, а exec() — синхронная CPU-работа, блокирующая луп, поэтому прерывание именно exec() — штатный кейс. Утверждение про 'corrupted state IO asyncio' и 'падение приложения' завышено для фактического однопоточного пути. Это корректностный/робастный баг, не security-critical — medium.

### M9. Глобальная мутация sys.stdout без lock — гонка в async-контексте
- **Модуль:** Ядро: LLM-модель + codeinterpreter
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `codeinterpreter.py:350-385`
- **Проблема:** В `_execute_code()` выполняется `sys.stdout = output_buffer` (глобальная мутация). Если два вызова `_execute_code()` выполняются конкурентно в asyncio (что возможно, т.к. метод async), второй вызов перезапишет sys.stdout первого. В `finally` оба восстановят `original_stdout`, но один из них восстановит уже перезаписанный буфер, а не оригинальный stdout. Итог: перемешанный вывод или потеря stdout процесса.
- **Влияние:** Потеря вывода при параллельных запросах; в худшем случае — разрушение глобального sys.stdout для всего процесса.
- **Исправление:** Передавать буфер в exec_globals['print'] через кастомный print-override вместо замены sys.stdout, или использовать contextlib.redirect_stdout() с threading.Lock вокруг критической секции.
- **Что даст:** Корректная работа при параллельных запросах.
- **Заметка верификатора:** Реальная гонка по дизайну: _execute_code (349-385) делает sys.stdout = output_buffer и восстанавливает original_stdout в finally; при истинно параллельном исполнении двух _execute_code финальное восстановление одного перепишет буфер другого. Однако в боевом потоке метод вызывается через asyncio.run внутри синхронного исполнения инструмента; внутри одного event loop корутины не выполняют exec() параллельно (это блокирующий sync-код, удерживающий луп), а сам модуль нигде не зарегистрирован боевым кодом. Реальная конкурентность двух _execute_code на одном экземпляре в этом репозитории не продемонстрирована. Латентный thread-safety дефект — medium, а не high.

### M10. RetryOpenAIServerModel — mutable shared state в __call__ без lock
- **Модуль:** Ядро: LLM-модель + codeinterpreter
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `retry_openai_model.py:114-115, 360-365, 400-434`
- **Проблема:** Атрибуты `self.current_model_index`, `self.connection_error_count` и `self.model` изменяются в `__call__()`, `_switch_to_fallback()` и `_should_fallback()` без какой-либо синхронизации. Если один объект `RetryOpenAIServerModel` используется несколькими агентами/корутинами (типичная ситуация — синглтон в DynamicAgentSystem), параллельные вызовы создадут гонку: два вызова одновременно могут переключить модель, increment connection_error_count даст неверный счётчик, self.model может быть заменён в середине вызова другой корутины.
- **Влияние:** Непредсказуемое поведение fallback; возможны бесконечные циклы переключений; потеря запросов.
- **Исправление:** Добавить threading.Lock (или asyncio.Lock если используется в async-контексте) вокруг всех операций с self.current_model_index, self.model, self.connection_error_count.
- **Что даст:** Корректная fallback-логика при конкурентном использовании.
- **Заметка верификатора:** Премиса 'синглтон' верна: agent_command.py:84-93 _get_model обёрнут @functools.lru_cache(maxsize=None), так что один экземпляр RetryOpenAIServerModel переиспользуется на имя модели. current_model_index/connection_error_count/self.model меняются в __call__/_switch_to_fallback/_should_fallback без локов. НО __call__ синхронный (def, time.sleep), smolagents исполняет модель блокирующе и последовательно (managed_agents идут по очереди в одном потоке агента); ThreadPoolExecutor в utils.py использует другой путь (call_openai_api), не retry-модель. Реальный параллельный вызов одного экземпляра из нескольких потоков в боевом потоке не показан. 'Бесконечные циклы переключений' дополнительно ограничены attempted_model_indices в __call__. Латентная гонка при будущем параллелизме — medium, не high.

### M11. Гонка данных: глобальный singleton UnifiedLoggingManager без защиты инициализации
- **Модуль:** Ядро: utils + logging + html
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `unified_logging.py:1034`
- **Проблема:** Функция `get_logging_manager` содержит классический double-check без блокировки: `if _logging_manager is None: _logging_manager = UnifiedLoggingManager(logs_dir)`. При параллельных вызовах из нескольких потоков (а сервис активно использует threading) возможно создание нескольких экземпляров `UnifiedLoggingManager`, каждый из которых: (a) добавит свой `RunIdLogHandler` к root logger (дублирование логов), (b) вызовет `logging.getLogger = _patched_get_logger` повторно, обернув уже обёрнутую функцию.
- **Влияние:** Дублирование записей в JSONL, двойная запись в файлы логов, потенциальная бесконечная рекурсия при цепочке патчей `logging.getLogger`. Возможны трудноотлаживаемые ошибки в продакшне.
- **Исправление:** Защитить инициализацию модульным `threading.Lock` с double-check: `with _init_lock: if _logging_manager is None: ...`. Альтернатива — использовать паттерн `functools.lru_cache(maxsize=None)` на `get_logging_manager` без аргументов-вариаций.
- **Что даст:** Устраняется гонка при создании singleton в многопоточной среде.
- **Заметка верификатора:** Гонка реальна, но severity завышена и часть заявленного impact ошибочна. unified_logging.py:1034-1037 — check-then-set БЕЗ блокировки (модульного lock для _logging_manager нет, grep подтвердил). При конкурентных вызовах из разных OS-потоков возможно создание двух менеджеров; _setup_log_handler на 694 делает root_logger.addHandler(event_handler) БЕЗУСЛОВНО → дубль RunIdLogHandler на root и дубль фоновых потоков EventBus (252-258) → дублирование JSONL/файловых записей. ОДНАКО заявленные 'повторное оборачивание logging.getLogger' и 'бесконечная рекурсия' — false: патч защищён флагом if not getattr(logging,'_unified_logging_patched',False) (708) и logging._unified_logging_patched=True (726), повторного wrap не происходит. Также console_handler переиспользуется из существующих (674-681), дубля консоли обычно нет. Главный документированный путь (service.py:_logging_manager) защищён собственным _LOGGING_MANAGER_LOCK с double-check, но runner.py:540 и agent_streamlit_api.py:89/236 зовут get_logging_manager напрямую без внешнего lock. Чисто стартовая гонка с узким окном и ограниченным эффектом (дубли логов), не критичный прод-баг — medium.

### M12. run_id_context пишет в os.environ["RUN_ID"] — не изолировано между потоками
- **Модуль:** Ядро: utils + logging + html
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `unified_logging.py:1102`
- **Проблема:** Контекстный менеджер `run_id_context` использует `threading.local` для потокобезопасной передачи run_id, но дополнительно пишет `os.environ["RUN_ID"] = run_id` (line 1102). `os.environ` является процессно-глобальным разделяемым состоянием: при нескольких параллельных `run_id_context` в разных потоках они перезаписывают одно и то же значение, так что `RunIdLogHandler.emit` может получить чужой `run_id` через fallback `os.environ.get('RUN_ID')` (line 525).
- **Влияние:** Логи одного запуска могут попасть в JSONL-файл другого запуска при конкурентных воркфлоу, что ломает трассируемость и диагностику.
- **Исправление:** Убрать запись в `os.environ` или чётко пометить, что `os.environ`-fallback в `RunIdLogHandler.emit` корректен только в однопоточном режиме. Приоритет должен быть исключительно за `threading.local`.
- **Что даст:** Логи не «утекают» между параллельными запусками воркфлоу.

### M13. Мутабельный `_SQLGLOT_METRICS` экспортирован публично — внешний код может изменять его без lock
- **Модуль:** Text2SQL: валидаторы безопасности
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/validators/__init__.py:16`
- **Проблема:** `_SQLGLOT_METRICS` (мутабельный `dict`) реэкспортирован из `__init__.py` и перечислен в комментарии как предназначенный для read-only доступа. Однако ни имя с одним подчёркиванием, ни комментарий не являются защитой на уровне кода. Любой импортирующий модуль может сделать `_SQLGLOT_METRICS["parse_attempts"] += 1` напрямую, минуя `_SQLGLOT_METRICS_LOCK`, что приведёт к race condition на счётчиках при конкурентных запросах.
- **Влияние:** Потеря инкрементов счётчиков в многопоточной среде; искажение метрик мониторинга. В худшем случае — TOCTOU при чтении/записи в сборщике метрик.
- **Исправление:** Убрать `_SQLGLOT_METRICS` из `__init__.py` (не должен быть частью публичного API). Для чтения метрик достаточно `get_sqlglot_metrics()`, которая уже есть и защищена lock. Убрать `_SQLGLOT_METRICS` из `__all__` и исправить `__init__.py`.
- **Что даст:** Гарантирует, что единственный способ мутировать счётчики — через `record_sqlglot_metric()`, которая берёт lock.

### M14. Гонка данных в _compiled_rules_cache без lock
- **Модуль:** Text2SQL: YAML/конфиги
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/core/_pii.py:50,152-156`
- **Проблема:** `_compiled_rules_cache` — обычный dict-синглтон на уровне модуля без threading.Lock. Проверка `if cache_key not in _compiled_rules_cache` и последующая запись `_compiled_rules_cache[cache_key] = [...]` не атомарны: два потока могут одновременно пройти проверку и оба записать в кэш. В CPython это безопасно с точки зрения целостности dict (GIL не допускает corrupt state), но ведёт к лишнему re-compile regex при первом вызове из разных потоков. Хуже другое: в отличие от `YamlConfigLoader` здесь нет `reset_cache()` — кэш нельзя сбросить ни в тестах, ни в проде при смене конфига.
- **Влияние:** В тестах, меняющих PII_JURISDICTION через monkeypatch, старые compiled-правила могут оставаться в кэше после сброса yaml-лоадера, если cache_key совпал. Не ведёт к утечке PII, но может замаскировать нарушения контракта в тестах.
- **Исправление:** Добавить `_compiled_rules_cache_lock = threading.Lock()` и взять lock перед проверкой/записью, либо использовать `functools.lru_cache` с `maxsize=8`. Добавить функцию `reset_compiled_rules_cache()` вызываемую из `pii_categories_config.reset_cache()`.
- **Что даст:** Корректная инвалидация кэша при смене конфигурации; устранение логической гонки при первом прогреве.

### M15. Гонка при мутации api_calls_window без лока в ResourcePool
- **Модуль:** Workflow Engine: ядро
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `workflow/resource_manager.py:91-93`
- **Проблема:** `record_api_call()` (строка 93) добавляет элемент в `self.api_calls_window` без захвата `self.lock`. При этом `acquire_resources()` (строки 41-47) читает и перезаписывает тот же список под `async with self.lock`. Параллельный вызов `record_api_call` из thread-pool (см. engine.py:1074, 1165, 1780) создаёт data race: `api_calls_window` может содержать неконсистентное состояние или потерять записи.
- **Влияние:** Неточный rate-limiting: реальное число API-вызовов может быть недосчитано или счётчик может corrupt под высокой нагрузкой.
- **Исправление:** Сделать `record_api_call` async и обернуть `self.api_calls_window.append(...)` в `async with self.lock`.
- **Что даст:** Корректный rate-limiting под параллельной нагрузкой.
- **Заметка верификатора:** Факт подтверждён: record_api_call (resource_manager.py:91-93) делает self.api_calls_window.append() без лока, тогда как acquire_resources (41-47) переприсваивает тот же список под async with self.lock. Вызовы идут из _execute_agent_sync (engine.py:1074, 1165, 1780), которые исполняются в thread pool через loop.run_in_executor (engine.py:1090) — а asyncio.Lock потоки не защищает, так что append к старому объекту-списку может потеряться при переприсваивании. Race реален. Но последствие — только неточность rate-limiting/мониторинга (счётчик может быть недосчитан), не порча состояния, восстановления или идемпотентности. high завышено -> medium.

### M16. _GLOBAL_WORKFLOW_ACTIVE_RUNS и _GLOBAL_WORKFLOW_RUN_CALLBACKS не защищены мьютексом
- **Модуль:** Workflow Engine: resilience/orchestration/intelligence
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `workflow/streamlit_api.py:437`
- **Проблема:** Глобальные словари `_GLOBAL_WORKFLOW_ACTIVE_RUNS` (строка 437) и `_GLOBAL_WORKFLOW_RUN_CALLBACKS` (строка 438) читаются и изменяются из нескольких потоков без синхронизации: main thread пишет в `self.active_runs[run_id]` (строки 852, 914), watchdog-поток читает и пишет в `run_data` (строки 876-908), вызовы `cancel_workflow` могут приходить из UI-потока (строки 1337-1475), `_notify_progress` читает `self.run_callbacks` (строка 1205). `_GLOBAL_WORKFLOW_PROCESSES_LOCK` защищает только `_GLOBAL_WORKFLOW_PROCESSES`, но не эти два словаря.
- **Влияние:** Race condition при одновременном завершении workflow (watchdog update) и вызове get_workflow_status/cancel из Streamlit UI. Возможны KeyError при удалении ключей, частичные обновления словаря.
- **Исправление:** Добавить отдельный RLock для `_GLOBAL_WORKFLOW_ACTIVE_RUNS` и `_GLOBAL_WORKFLOW_RUN_CALLBACKS`, использовать его во всех операциях записи/чтения этих структур. Или перейти на `threading.local` / thread-safe структуры.
- **Что даст:** Устраняет data races между watchdog, UI и callback потоками.
- **Заметка верификатора:** Кросс-поточный доступ реален: watchdog исполняется в настоящем OS-потоке родителя (streamlit_api.py:910) и мутирует run_data.update(...) (891-908), одновременно UI-поток читает/пишет тот же run_data в get_workflow_status (1255-1292) и cancel_workflow (1337-1456); _notify_progress итерирует run_callbacks (1205). Мьютекса нет. НО заявленные краши не подтверждаются: (1) ключи из ACTIVE_RUNS/RUN_CALLBACKS нигде не удаляются (grep по pop/del — только присваивания на 852/914/943/1046/1188/1198) → 'KeyError при удалении' невозможен; (2) monitoring.py:79 делает снапшот {**workflow_runs} перед итерацией, а _notify_progress итерирует per-run список → 'dict changed size during iteration' не воспроизводится; (3) GIL делает отдельные dict-операции атомарными. Реальный остаточный риск — логическая гонка на поле status/end_time (watchdog ставит 'failed', cancel — 'cancelled' = lost update), без падений. Severity понижена с high до medium.

### M17. Metrics: increment_counter не thread-safe, двойное чтение-запись без атомарности
- **Модуль:** Workflow Engine: resilience/orchestration/intelligence
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `workflow/monitoring/metrics.py:302`
- **Проблема:** `increment_counter` (строка 302-307) выполняет `get_current_value()` + `add_value(current + delta)` без захвата `self._lock`. Метод `add_value` захватывает `metric._lock`, но между `get_current_value` и `add_value` возникает TOCTOU-гонка: два параллельных вызова могут прочитать одинаковое значение и оба записать `current + 1`, потеряв один инкремент.
- **Влияние:** Потеря счётчиков событий (retry, circuit_breaker, step_executions) при параллельном выполнении. Мониторинг недосчитывает события.
- **Исправление:** Выполнять check-and-increment атомарно под `self._lock`, либо хранить счётчик как отдельный `threading.atomic`-паттерн (lock + internal int, не список значений).
- **Что даст:** Точные метрики при параллельном выполнении.

### M18. BudgetManager.consume_budget: race condition между check_budget и фактическим списанием
- **Модуль:** Workflow Engine: resilience/orchestration/intelligence
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `workflow/resilience/budget.py:156`
- **Проблема:** Метод `consume_budget` (строка 156) сначала вызывает `check_budget` (строка 162), а затем — отдельно — списывает `budget_limit.current_usage += amount` (строка 174). Между проверкой и списанием нет lock. При параллельном вызове двух шагов: оба пройдут `check_budget` с `current_usage=90, limit=100, amount=8`, оба спишут по 8, итоговый `current_usage=106 > limit=100`. Бюджет превышен без срабатывания защиты.
- **Влияние:** Перерасход токенов/cost/API_calls — реальный денежный риск при параллельном выполнении шагов.
- **Исправление:** Добавить threading.Lock в BudgetManager и оборачивать всю пару check+consume в одну критическую секцию.
- **Что даст:** Корректный контроль бюджета при параллельном выполнении.

### M19. Блокирующий subprocess.run внутри async-контекста service action (mermaid/plantuml)
- **Модуль:** Backend FastAPI / AG-UI
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `backend/fastapi_app/agui/service.py:651`
- **Проблема:** _render_mermaid_preview и _render_plantuml_preview вызывают subprocess.run(..., check=True) — синхронный блокирующий вызов. handle_service_action вызывается из run_agent (async), который является async generator, выполняющимся в asyncio event loop. Во время рендеринга диаграммы (может занимать секунды для mmdc/java) event loop заблокирован и не может обслуживать другие корутины.
- **Влияние:** Под нагрузкой один запрос на рендеринг диаграммы замораживает весь event loop, задерживая все прочие SSE-стримы и HTTP-запросы.
- **Исправление:** Использовать asyncio.get_running_loop().run_in_executor(None, ...) для subprocess.run, как уже сделано для cancel_workflow в runner.py:373.
- **Что даст:** Отзывчивость event loop при рендеринге диаграмм.

### M20. Race condition: monitoring.py мутирует _GLOBAL_ACTIVE_RUNS без лока
- **Модуль:** Streamlit UI
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `streamlit_app/monitoring.py:83`
- **Проблема:** Метод `_check_stale_runs()` итерируется по `all_runs.items()` (строка 83) и мутирует `run_data["status"]` (строка 138) без каких-либо блокировок. `_GLOBAL_ACTIVE_RUNS` — глобальный dict, который одновременно читается/записывается UI-потоками Streamlit и фоновыми рабочими потоками (в `run_agents_text_to_sql` используется `lock = get_job_registry_lock()`, но это другой лок для другого dict). Класс имеет `_lock` для singleton-паттерна, но не для защиты самих данных.
- **Влияние:** RuntimeError на `dict changed size during iteration` или потеря статуса запуска при конкурентном обновлении — особенно при активных фоновых агентских задачах.
- **Исправление:** Взять snapshot dict перед итерацией: `snapshot = dict(all_runs)`, или использовать общий `threading.Lock` из модуля `agent_streamlit_api`/`workflow.streamlit_api` при любом чтении/записи глобальных реестров.
- **Что даст:** Устранение нестабильных сбоев при параллельных запусках.
- **Заметка верификатора:** Заявленный механизм краша частично неверен. На строке 79 строится НОВЫЙ локальный dict: all_runs = {**agent_runs, **workflow_runs}. Итерация на строке 83 идёт по этой локальной копии, поэтому 'dict changed size during iteration' на строке 83 при конкурентной мутации глобалей возникнуть НЕ может (это копия). Реальные факты: глобали _GLOBAL_ACTIVE_RUNS / _GLOBAL_WORKFLOW_ACTIVE_RUNS действительно мутируются рабочими потоками без лока (вставки active_runs[run_id]={}, удаление del self.active_runs[run_id] в agent_streamlit_api.py:1513), а монитор — отдельный daemon-поток. Остаточные риски: (1) сам merge {**...} на строке 79 читает живые глобали и теоретически может бросить RuntimeError, если воркер вставит/удалит ключ ровно во время распаковки; (2) мутация run_data['status'] на строке 138 — это data-race, но присваивание элемента dict атомарно под GIL, без краша/коррупции. Монитор работает раз в 60с, мутации редкие (старт/финиш run) — вероятность низкая. Проблема реальна (lock-free доступ к shared globals), но severity high завышена. Понижаю до medium.

### M21. Race condition: несколько параллельных loadRunStatus для одного runId
- **Модуль:** Frontend (Next.js / React AG-UI)
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `frontend/client/src/app/components/sections/TextToSqlSection.tsx:521`
- **Проблема:** Интервал автообновления `window.setInterval(() => void loadRunStatus(runId), 3000)` запускается при каждом изменении `runId`. Функция `loadRunStatus` использует `runStatusInFlightRef` как in-flight guard, но при быстром изменении `runId` или закрытии/открытии компонента возможно наличие нескольких интервалов одновременно (отсутствует cleanup по предыдущему runId при deps-изменении). Аналогичная проблема в DynamicAgentsSection.tsx:295 с логами, которые обновляются без in-flight guard (прямой вызов handleRunLogs в интервале).
- **Влияние:** Множественные одновременные запросы к backend перегружают очередь service actions, UI получает устаревшие ответы. В крайнем случае setCurrentWorkflowRunLogs может быть вызван с данными чужого runId.
- **Исправление:** В useEffect возвращать cleanup-функцию, которая явно отменяет интервал при изменении runId. Для loadRunStatus использовать AbortController или проверку актуальности runId в момент resolve.
- **Что даст:** Предотвращает гонки состояния при частой смене активного запуска.

### M22. Глобальный синглтон ToolManager не потокобезопасен (гонка при инициализации)
- **Модуль:** Ядро: tool manager + MCP
- **Категория:** concurrency | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `tool_manager.py:595-602`
- **Проблема:** Функция `get_tool_manager()` проверяет `if _tool_manager is None` и создаёт экземпляр без блокировки. При конкурентном вызове из нескольких потоков (FastAPI + Streamlit + workflow engine) возможно создание нескольких экземпляров, каждый со своим словарём `active_runs`. Также `active_runs` — обычный `dict`, его одновременная запись и итерация (`list_active_tools`, `cleanup_completed`) не защищены.
- **Влияние:** Потеря записей о запусках инструментов, частичная видимость в мониторинге, редкие `RuntimeError: dictionary changed size during iteration`.
- **Исправление:** Использовать `threading.Lock` для инициализации синглтона. Заменить `active_runs` на `threading.local()` или защитить доступ `Lock`-ом.
- **Что даст:** Корректная работа в многопоточном окружении.

### M23. Временная мутация глобального AGENT_PROFILES не защищена блокировкой
- **Модуль:** Ядро: configuration & streamlit API
- **Категория:** concurrency | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `agent_streamlit_api.py:1093-1122`
- **Проблема:** create_dynamic_agent() временно добавляет запись в глобальный AGENT_PROFILES (строка 1094), затем удаляет её в finally (строка 1121). Если несколько потоков вызывают create_dynamic_agent() одновременно, они читают/пишут AGENT_PROFILES без синхронизации. Кроме того, AgentFactory.create_agent() в строке 1098 читает AGENT_PROFILES, который в этот момент видит чужие temp-записи.
- **Влияние:** Агент может быть создан с профилем другого параллельного вызова. В лучшем случае — неожиданное поведение агента, в худшем — запуск с чужими инструкциями.
- **Исправление:** Добавить threading.Lock вокруг блока try/finally, или передавать profile_dict напрямую в фабрику без мутации глобального реестра.
- **Что даст:** Корректное создание динамических агентов при параллельных запросах.

### M24. web_research.py: глобальный синглтон WebResearchTool с requests.Session
- **Модуль:** custom_tools (прочие инструменты)
- **Категория:** concurrency | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/web_research.py:579`
- **Проблема:** `web_research_tool = WebResearchTool()` создаётся на уровне модуля. Объект содержит `self.session = requests.Session()` и `self._semaphore`/`self._semaphore_loop`. При использовании из разных потоков или при переиспользовании между разными asyncio event loops (что и происходит через `_safe_run_async` → `ThreadPoolExecutor` → `asyncio.run`) `_semaphore_loop` может устареть, а `requests.Session` небезопасен для concurrent использования без лока. Метод `close()` есть, но `__del__` отсутствует и `close()` нигде не вызывается.
- **Влияние:** Потенциальные гонки на `self._semaphore`/`self.session` в многопоточном контексте; утечка HTTP-соединений при долгой работе.
- **Исправление:** Либо создавать `WebResearchTool` per-request (без глобального синглтона), либо защитить `session` через `threading.Lock`, либо вообще убрать `requests.Session` из состояния и создавать его локально в каждом вызове.
- **Что даст:** Thread-safety и отсутствие утечки соединений.

### M25. Мутабельный глобальный singleton sql_validator читается без лока в validate() при concurrent reload()
- **Модуль:** Text2SQL: ядро + NLU
- **Категория:** concurrency | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `/srv/git_projects/MultiAgent/custom_tools/text_to_sql/validators/safety.py:1149`
- **Проблема:** `SQLSafetyValidator.reload()` обновляет `self._regex` и `self._sqlglot` под `self._reload_lock` (строка 1149), но `validate()` → `_validate_inner()` → `_validate_with_sqlglot()` читает `self._regex` и `self._sqlglot` БЕЗ какого-либо лока. Python GIL обеспечивает атомарность отдельных загрузок атрибутов, но между чтением `self._regex` и `self._sqlglot` внутри `_validate_with_sqlglot` может произойти reload — в итоге один вызов validate() будет использовать `_regex` от нового профиля и `_sqlglot` от старого. Это явно задокументировано как ограничение, но не задокументированы последствия.
- **Влияние:** При concurrent reload() (например, через admin API) + активный трафик: микс-профиль валидации на время смены конфига. Запрос, запрещённый новым профилем, может пройти старую проверку или наоборот. Кратковременная, но реальная уязвимость при горячей ротации конфига.
- **Исправление:** Либо сделать validate() copy-on-read (snapshot `_regex, _sqlglot = self._regex, self._sqlglot` в начале метода — два reads under GIL дают консистентную пару), либо документировать, что reload() не должен вызываться при наличии трафика (только при drain). Первый вариант дешевле.
- **Что даст:** Устранение race-окна при hot-reload конфига безопасности.

### M26. Частичная гонка данных в SQLSafetyValidator.reload() — validate() может видеть рассинхрон полей
- **Модуль:** Text2SQL: генерация SQL
- **Категория:** concurrency | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/validators/safety.py:1144`
- **Проблема:** Метод `reload()` обновляет поля `self._regex`, `self._sqlglot`, `self.forbidden_keywords` и пр. под `_reload_lock`. Однако `_validate_inner` читает эти поля БЕЗ lock. В Python GIL делает отдельные read/write атомарными на уровне байткода, но между чтением `self._regex` и чтением `self._sqlglot` может случиться reload, и потоки увидят `_regex` от нового профиля и `self.forbidden_keywords` от старого (или наоборот). Контракт в docstring честно признаёт это, но не отражает в имплементации (например, локального снапшота полей нет).
- **Влияние:** В сценарии одновременного reload и validate (admin endpoint + RPS) — редкий, но возможный рассинхрон профилей безопасности. Запрос может проходить валидацию по смешанному набору правил.
- **Исправление:** В начале `_validate_inner` атомарно снять снапшот `(regex, sqlglot_validator) = (self._regex, self._sqlglot)` и работать с ним. Это не требует lock в читателе и устраняет гонку.
- **Что даст:** Валидация всегда работает с согласованным профилем.

### M27. SchemaEnricher._cached_schema — разделяемый mutable state между вызовами без синхронизации
- **Модуль:** Text2SQL: schema (cache/loader/memory)
- **Категория:** concurrency | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/schema_enricher.py:94,176`
- **Проблема:** `self._cached_schema = schema_obj` устанавливается в `enrich_descriptions_with_llm` (строка 176) и читается в `_get_fk_previews` (строка 529). `SchemaEnricher` создаётся как инстанс внутри `_build_default_deps` и прикрепляется к `SchemaLinker`. Если один экземпляр `SchemaLinker` (а значит, и `SchemaEnricher`) используется из нескольких потоков, гонка на `_cached_schema` приведёт к тому, что `_get_fk_previews` может читать схему другого tenant'а. В FastAPI с thread-pool это реально.
- **Влияние:** Cross-tenant утечка схемы: FK-превью одного tenant'а могут попасть в LLM-промпт другого, что нарушает изоляцию данных.
- **Исправление:** Передавать `schema_obj` явным параметром в `_get_fk_previews` вместо сохранения в instance state. Либо создавать новый `SchemaEnricher` per-request.
- **Что даст:** Устранение гонки данных и потенциальной межтенантной утечки схемы.

### M28. Globally-mutable синглтон _CHROMA_POOL не защищён от race при чтении max_workers
- **Модуль:** Text2SQL: schema-linking + RAG
- **Категория:** concurrency | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/rag/retrieval.py:491`
- **Проблема:** `_CHROMA_POOL_MAX_WORKERS` — глобальная переменная, которая записывается внутри `_get_chroma_pool()` под `_CHROMA_POOL_LOCK`, но читается в `_search_chroma` (строка 492: `max_workers = _CHROMA_POOL_MAX_WORKERS or ...`) без лока. Если `_search_chroma` вызывается до того, как `_get_chroma_pool()` завершил инициализацию в другом потоке, `_CHROMA_POOL_MAX_WORKERS` может быть `None`.
- **Влияние:** При высокой конкурентности overload-guard вычисляет `max_workers` через повторный вызов `_chroma_pool_max_workers()` (fallback в строке 492), что корректно по значению, но нарушает единообразие — нельзя доверять, что guard использует тот же лимит, что и пул.
- **Исправление:** Читать `_CHROMA_POOL_MAX_WORKERS` только через `_get_chroma_pool()` (он гарантирует инициализацию) или вернуть его из `_get_chroma_pool` вместе с executor.
- **Что даст:** Overload-guard гарантированно использует тот же `max_workers`, что и пул; нет torn-read.

### M29. SharedIndexState._index_registry и _per_session_locks — глобальные ClassVar без очистки при fork/multiprocessing
- **Модуль:** Text2SQL: schema-linking + RAG
- **Категория:** concurrency | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/rag/_state.py:39`
- **Проблема:** `_index_registry` и `_per_session_locks` — ClassVar на `SharedIndexState`, что делает их process-global синглтонами. При использовании `os.fork()` или `multiprocessing` (spawn-режим) дочерний процесс наследует весь реестр индексации. RLock, скопированный через fork, может быть в заблокированном состоянии (если fork произошёл, пока родительский поток держал лок), что приведёт к deadlock в дочернем процессе.
- **Влияние:** Deadlock в worker-процессах gunicorn/uvicorn при использовании multiprocessing; состояние индексации может быть несогласованным между процессами.
- **Исправление:** Добавить `os.register_at_fork(after_in_child=SharedIndexState._reset_all)` и реализовать `_reset_all()` для очистки registry и locks. Или явно документировать, что `RAGSearcher` не fork-safe.
- **Что даст:** Предотвращение deadlock в production-деплоях с pre-fork моделью (gunicorn).

### M30. `reload()` не атомарен относительно конкурентных `validate()` — потенциальная гонка на конфиге
- **Модуль:** Text2SQL: валидаторы безопасности
- **Категория:** concurrency | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/validators/safety.py:1149-1158`
- **Проблема:** `reload()` создаёт новые объекты `_regex`/`_sqlglot` вне lock и затем записывает их под `_reload_lock`. Но конкурентные `validate()` не берут `_reload_lock` при чтении `self._regex`/`self._sqlglot`. Следовательно, поток, выполняющий `validate()`, может увидеть `_regex` от нового профиля и `_sqlglot` от старого (или наоборот), если произошло частичное обновление. Docstring честно описывает это ограничение, но не предлагает альтернативы на уровне кода — только organisational (admin/тесты).
- **Влияние:** В редком сценарии одновременного `reload()` и высокого rps: запрос может проверяться смешанным профилем (часть полей — новый, часть — старый). Для безопасности критично: например, новый `forbidden_functions` в `_regex`, но старый (пустой) `_sqlglot.forbidden_functions` — AST-проверка функций не применяется.
- **Исправление:** Использовать шаблон copy-on-write: хранить все вспомогательные объекты в одной иммутабельной структуре (`_SafetyState = dataclass`), которая атомарно заменяется одним `self._state = new_state` присваиванием (в CPython — атомарная операция). Либо использовать `threading.RLock` для `validate()` тоже, но это снизит throughput. Минимальный патч — заменить отдельные поля на `self._state = new_state` после создания под lock.
- **Что даст:** Устраняет window, при котором validate() видит частично обновлённый профиль.

### M31. Синхронный вызов save_memory в async _save_to_memory_system
- **Модуль:** Workflow Engine: ядро
- **Категория:** concurrency | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `workflow/state_manager.py:698-703`
- **Проблема:** `_save_to_memory_system` — async метод, но вызывает `save_memory(...)` (строка 699) синхронно без `await` или `run_in_executor`. Если `save_memory` выполняет IO (сохранение в файл, запрос к БД), это блокирует event loop на всё время выполнения. Вызывается при каждом checkpoint — то есть после каждого успешного шага.
- **Влияние:** Блокировка event loop при каждом сохранении checkpoint. В workflows с >3 шагами и включённым memory_manager деградирует throughput всего async приложения.
- **Исправление:** Обернуть синхронный `save_memory` в `await asyncio.get_event_loop().run_in_executor(None, lambda: save_memory(...))`, либо сделать save_memory async.
- **Что даст:** Отзывчивый event loop при сохранении checkpoint'ов.

### M32. SQLite + ChromaDB рассинхронизация: summary_agent_memory_step не защищён блокировкой
- **Модуль:** Memory / RAG
- **Категория:** concurrency | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `memory/tools.py:1561-1613`
- **Проблема:** Функция `summary_agent_memory_step` выполняет UPDATE + INSERT в SQLite и обновляет ChromaDB без захвата `memory_manager.db_handler.lock`. При конкурентном вызове из двух потоков — оба могут прочитать старую запись, оба деактивируют её (UPDATE), потом оба делают INSERT с одинаковым шагом. Аналогичная проблема: UPDATE делается вне транзакции с проверкой rowcount (в отличие от update_goal_status, который проверяет rowcount).
- **Влияние:** Дублированные или потерянные суммари при конкурентном использовании. В однопоточном коде — не проявляется.
- **Исправление:** Обернуть всю операцию SELECT/UPDATE/INSERT в `with memory_manager.db_handler.lock:`, как это сделано в `save_memory` и `save_goal`.
- **Что даст:** Устранение потенциальных дублей суммари при параллельной работе агентов.

### M33. Глобальный модульный кэш _AITUNNEL_MODELS_CACHE без защиты от гонки
- **Модуль:** Storybook: видео/shots-генераторы
- **Категория:** concurrency | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/video_generator_aitunnel_tool.py:48,712-728`
- **Проблема:** Переменная `_AITUNNEL_MODELS_CACHE` — глобальный мутабельный синглтон. Функция `_get_aitunnel_video_models` читает и пишет её без блокировки. При параллельных вызовах (несколько воркеров ThreadPoolExecutor) возможна гонка: несколько потоков одновременно проходят проверку `if _AITUNNEL_MODELS_CACHE is not None` (False), делают параллельные HTTP-запросы к models endpoint и одновременно присваивают результат. В CPython это безопасно только благодаря GIL при простом присвоении, но это неявная зависимость от реализации, а не гарантия.
- **Влияние:** Несколько параллельных HTTP-запросов к /models при первом вызове; в крайних случаях — неконсистентное состояние кэша.
- **Исправление:** Добавить threading.Lock() для защиты read-check-write паттерна, либо инициализировать кэш при импорте модуля (один раз).
- **Что даст:** Потокобезопасный кэш, один HTTP-запрос к /models вместо N параллельных.

### M34. Гонка на all_start_items / all_end_items при параллельной обработке сцен
- **Модуль:** Storybook: видео/shots-генераторы
- **Категория:** concurrency | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/screenplay_shots_generator.py:720-748`
- **Проблема:** `all_start_items` и `all_end_items` — обычные Python-списки, объявлены в замыкании на уровне функции screenplay_shots_generator_tool. Они используются внутри `_checkpoint_partial_progress` (строки 407-413), который вызывается из `_process_scene_worker` — воркера ThreadPoolExecutor. Несмотря на то, что `checkpoint_state_lock` защищает `checkpoint_scene_items`, сами `all_start_items.extend()` на строках 726-727 выполняются в основном потоке (в loop `for fut in as_completed`), что безопасно. Однако функция `_checkpoint_partial_progress` читает `all_start_items` и `all_end_items` напрямую (строки 407-413) из тела воркера без блокировки на эти списки, параллельно с тем, что другие воркеры тоже могут дёргать `_checkpoint_partial_progress`. Замечание: `_checkpoint_partial_progress` захватывает `checkpoint_state_lock`, но не общий лок на `all_start_items`/`all_end_items`, которые читаются внутри этой функции.
- **Влияние:** При max_workers > 1 (по умолчанию до 5) несколько воркеров могут одновременно читать `all_start_items` пока основной поток его не трогает — формально безопасно в CPython, но `_checkpoint_partial_progress` читает эти списки из контекста до того, как основной поток их заполнит, что делает checkpoint неполным и потенциально хаотичным.
- **Исправление:** Дать `_checkpoint_partial_progress` читать только локальные `scene_start_items`/`scene_end_items` (что она уже делает через `checkpoint_scene_items`), не обращаясь к внешним `all_start_items` — это уже реализовано в коде через `checkpoint_scene_items`. Код корректен, но неочевиден; добавить комментарий, что функция намеренно не читает `all_*_items`.
- **Что даст:** Повышение читаемости, предотвращение будущих регрессий при модификации.

### M35. Гонка данных: is_generating без блокировки при вызове из нескольких потоков
- **Модуль:** StoryBookManager (десктоп-приложение)
- **Категория:** concurrency | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `StoryBookManager/gui/generation_panel.py:1008`
- **Проблема:** Флаг `self.is_generating` читается в GUI-потоке (`run_full_pipeline`, `run_from_step`, `toggle_pause`, `stop_generation`) и устанавливается/сбрасывается в фоновом потоке через `self.after(0, _update_ui)` в `finish_generation`. Между проверкой `if self.is_generating` и фактическим стартом нового потока отсутствует atomics или lock. При быстром двойном нажатии «Запустить» оба вызова пройдут проверку `is_generating == False` до того, как первый поток успеет выставить флаг.
- **Влияние:** Возможен запуск двух параллельных pipeline, которые будут писать в один и тот же project_id, перетирая файлы и checkpoint'ы друг друга.
- **Исправление:** Добавить `threading.Lock` вокруг блока «проверка + установка `is_generating = True` + старт потока», либо дизейблить кнопки запуска синхронно в GUI-потоке до того, как поток стартует.
- **Что даст:** Исключает двойной запуск pipeline.

### M36. Глобальный синглтон get_telemetry_manager() не защищён от гонки
- **Модуль:** prompt_optimizer + telemetry
- **Категория:** concurrency | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `telemetry/smolagents_telemetry.py:1043-1051`
- **Проблема:** get_telemetry_manager() проверяет `if _telemetry_manager is None` и создаёт объект без блокировки. В многопоточном окружении (FastAPI/uvicorn с несколькими потоками) возможна гонка при первом обращении: два потока одновременно видят None и оба создают SmolagentsTelemetryManager, дважды регистрируя SmolagentsInstrumentor и дважды вызывая trace.set_tracer_provider(). Внешний caller (backend/fastapi_app/agui/service.py:2255) уже использует двойную проверку с Lock — но сама функция-фабрика в telemetry/ этого не делает.
- **Влияние:** Двойная инициализация OpenTelemetry может вызвать предупреждение 'Overriding of current TracerProvider' и дублирование спанов.
- **Исправление:** Добавить threading.Lock на уровне модуля и использовать double-checked locking в get_telemetry_manager(), аналогично тому, что делает service.py.
- **Что даст:** Безопасная инициализация синглтона в многопоточной среде.

### M37. analyze_task возвращает pipeline_type=None для 'general' задач, а вызывающий код ожидает строку
- **Модуль:** Ядро: оркестрация агентов
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `agent_system.py:441 / agent_factory.py:73`
- **Проблема:** Для non-sql задач `analyze_task` возвращает `(agent_types, None)` (строка 441). `pipeline_type=None` передаётся в `create_agent` (строка 213), а затем в `_build_composite_prompt` как второй аргумент. Внутри `_build_composite_prompt` вызывается `pipeline_prompts.get(None, '')`, что тихо возвращает `''` вместо ожидаемого pipeline-специфичного промпта. Функция имеет default `pipeline_type: str = "general"`, что создаёт ложное ощущение что `None` эквивалентен `"general"`.
- **Влияние:** Для менеджер-агента при 'general' задаче pipeline-специфичный промпт никогда не подставляется, даже если в YAML заведён ключ `pipeline_prompts.general`. Тип-аннотация `str` нарушается (передаётся `None`).
- **Исправление:** В `analyze_task` заменить `return ..., None` на `return ..., 'general'`. Либо добавить `pipeline_type = pipeline_type or 'general'` в начало `_build_composite_prompt`.
- **Что даст:** Корректное применение pipeline-промптов, устранение скрытого None-passing.

### M38. with_telemetry-декоратор передаёт positional args в run_tool некорректно
- **Модуль:** Ядро: tool manager + MCP
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `tool_manager.py:630-636`
- **Проблема:** `tool_manager.run_tool(tool_name=..., tool_function=..., task_description=..., session_id=..., *args, **kwargs)` — использование `*args` после именованных аргументов синтаксически допустимо в Python 3, но `run_tool` принимает только `**kwargs` после `session_id` (строка 219). Если декорируемая функция вызывается с positional arguments, они попадут в `**kwargs` как безымянные и вызовут `TypeError`. Функция `run_tool` при этом пытается передать их в `tool_function` через `_filter_kwargs_for_callable`, которая работает только с именованными параметрами.
- **Влияние:** Декоратор `@with_telemetry` неработоспособен для функций, вызываемых с positional args.
- **Исправление:** В wrapper собирать positional args в kwargs по именам параметров функции до вызова `run_tool`, либо удалить `*args` из вызова `run_tool` и передавать все аргументы через `**kwargs`.
- **Что даст:** Корректная работа декоратора @with_telemetry.

### M39. Бесконтрольная рекурсия в _execute_code при повторных ModuleNotFoundError
- **Модуль:** Ядро: LLM-модель + codeinterpreter
- **Категория:** correctness | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `codeinterpreter.py:375-379`
- **Проблема:** При `ModuleNotFoundError` код пытается установить пакет и затем рекурсивно вызывает `await self._execute_code(code)`. Если установка пакета прошла успешно, но при повторном выполнении кода возникает другой `ModuleNotFoundError` (другой пакет), рекурсия повторяется. Глубина рекурсии не ограничена. При n импортируемых отсутствующих пакетах или при ошибке установки (которая игнорируется, т.к. `install_package` возвращает False но код всё равно продолжает) это приведёт к `RecursionError` или бесконечному install-loop.
- **Влияние:** RecursionError / stack overflow; зависание при недоступном PyPI; непредсказуемое поведение.
- **Исправление:** Убрать рекурсию — вместо неё в `run_code` сначала вызвать `preinstall_required_packages`, потом однократно `_execute_code`; ограничить глубину рекурсии или превратить в итеративный цикл с счётчиком.
- **Что даст:** Предсказуемое поведение при отсутствующих зависимостях; устранение stack overflow.
- **Заметка верификатора:** Подтверждено как реальный, но уточнённый риск. _execute_code (375-379) рекурсивно вызывает себя после успешной install_package. Описанный 'другой пакет каждый раз' ограничен числом отсутствующих импортов (не бесконечно). Настоящий бесконечный/деградирующий цикл возникает в распространённом кейсе, когда pip-имя != import-имя (sklearn vs scikit-learn, cv2 vs opencv-python): install_package делает pip install, возвращает True (returncode==0), но __import__(того же имени) при следующем заходе всё равно падает -> та же ModuleNotFoundError -> снова install -> рекурсия до RecursionError или зависания при недоступном PyPI. SIGALRM-таймаут не ограничивает рекурсию между вызовами (новый timeout() на каждом уровне). Реально, но это DoS/робастность, требует специфичных импортов и не security-critical — medium вместо high.

### M40. clean_data вызывается дважды при успешном выполнении — двойное удаление файлов
- **Модуль:** Ядро: LLM-модель + codeinterpreter
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `codeinterpreter.py:647, 680`
- **Проблема:** В `run_code()` при успешном выполнении вызывается `self.clean_data(session_id)` в строке 647 (внутри цикла), а затем снова в `finally` блоке (строка 680). При первом вызове файлы удаляются, при втором `os.listdir()` проходит по уже пустым директориям (безвредно), но есть гонка: если между двумя вызовами другая корутина создаст файл с тем же session_id (маловероятно при 8-символьном UUID, но возможно) — его удалят ошибочно.
- **Влияние:** Потенциальная потеря файлов при коллизии session_id; в норме — лишний IO.
- **Исправление:** Убрать явный вызов clean_data из тела цикла (строка 647) и оставить только в finally.
- **Что даст:** Устранение двойного удаления и потенциальной гонки.

### M41. __getattr__ создаёт бесконечную рекурсию при обращении к self.model до инициализации
- **Модуль:** Ядро: LLM-модель + codeinterpreter
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `retry_openai_model.py:1360-1364`
- **Проблема:** `__getattr__` проксирует всё к `self.model`. Если в `__init__` до присвоения `self.model` возникнет исключение (например, `_create_custom_http_client` или `_create_model` упадут), то любой последующий доступ к атрибутам объекта вызовет `__getattr__`, который попытается получить `self.model`, что снова вызовет `__getattr__` (т.к. model не существует в `__dict__`), и так до `RecursionError`.
- **Влияние:** При ошибке инициализации — `RecursionError` вместо информативного исключения; маскировка реальной проблемы.
- **Исправление:** Добавить проверку: `if name == 'model': raise AttributeError('model not initialized')` в начало `__getattr__`.
- **Что даст:** Понятное сообщение об ошибке при проблемах инициализации.

### M42. test_llm_connection всегда возвращает success=True для openai/anthropic без реальной проверки
- **Модуль:** Ядро: configuration & streamlit API
- **Категория:** correctness | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `configuration_api.py:1055-1063`
- **Проблема:** Ветки для провайдеров openai и anthropic выставляют result['success'] = True с комментарием '(симулированный)' без единого сетевого вызова и без проверки наличия API-ключа. При этом для anthropic в строке 1049 уже есть проверка ключа, но для openai её нет, и провайдер openai при отсутствии ключа всё равно вернёт success=True.
- **Влияние:** UI показывает пользователю «соединение успешно» независимо от реального состояния. Неверный ключ, недоступный endpoint — всё замаскировано. Решения о конфигурации принимаются на основе ложного статуса.
- **Исправление:** Либо сделать реальный minimal API-вызов, либо честно переименовать метод в validate_config_structure() и убрать поле 'test_response', не имитирующее реальную проверку.
- **Что даст:** Устраняет ложное чувство безопасности; баги конфигурации обнаруживаются до запуска агента.
- **Заметка верификатора:** Подтверждено: ветка openai (строки 1055-1058) выставляет success=True без сетевого вызова и без проверки ключа; проверка ключа на строке 1049 покрывает только anthropic/local, для openai её нет. Обе ветки помечены '(симулированный)' — это явный stub, а не реальный коннект. Понизил до medium: это диагностический заглушечный метод, не влияющий на фактический запуск агентов (агенты используют реальные системные модели из model_mapping, путь 1023-1040 возвращается раньше для логических моделей). Вводит в заблуждение в UI, но не ломает прод-исполнение — high завышено.

### M43. run_id совпадает с session_id — коллизия при повторном использовании сессии
- **Модуль:** Ядро: configuration & streamlit API
- **Категория:** correctness | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `agent_streamlit_api.py:699-701, 1149-1150`
- **Проблема:** В run_agent() и run_manager_with_team() run_id = session_id. Если caller передаёт один и тот же session_id дважды (например, повторный запуск в той же UI-сессии), второй запуск перезаписывает active_runs[run_id] первого (строка 722, 1175), уничтожая состояние первого запуска. При этом первый watchdog-поток продолжает работать и записывает результат в тот же ключ, что принадлежит второму запуску.
- **Влияние:** Потеря статуса первого запуска; неверный финальный статус; watchdog второго запуска может пометить run как failed из-за exit_code первого процесса.
- **Исправление:** Генерировать run_id независимо (uuid.uuid4()), не приравнивать к session_id. Session_id передавать агенту отдельно.
- **Что даст:** Корректная работа при повторных запусках и нескольких параллельных запусках в одной сессии.
- **Заметка верификатора:** Подтверждено по коду: run_agent (699-701) и run_manager_with_team (1148-1150) делают run_id = session_id, а при повторном запуске self.active_runs[run_id] = {...} (722/1175) перезаписывает запись первого. UI позволяет ввести произвольный session_id (03_Agents.py:263-267) с дефолтом до секунды (%H%M%S), так что коллизия достижима. Watchdog первого (757) может записать статус в перезаписанный ключ. Понизил до medium: баг реален, но требует действия пользователя (повторное использование того же session_id; при None генерируется uuid и коллизии нет — строки 700/1149), а последствия ограничены статус-трекингом в UI, без порчи данных или выполнения. high завышено.

### M44. clean_data: полное удаление файлов по подстроке session_id без ограничения пути
- **Модуль:** Ядро: utils + logging + html
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `html_utils.py:1912`
- **Проблема:** `clean_data` удаляет все файлы в `plots/`, чьё имя содержит `f'_{session_id}'` (не начинается с, не оканчивается на — именно «содержит»). Если `session_id` — короткая или предсказуемая строка (например, UUID с первыми 8 символами совпадающими), можно случайно или намеренно удалить чужие файлы. Помимо этого, метод вызывается дважды из `advanced_visualization` — один раз в теле try (line 1715) и один раз в finally (line 1723) — что означает двойной обход директории.
- **Влияние:** Случайное или намеренное удаление файлов другого воркфлоу. При ошибке в первом `clean_data` finally всё равно удалит файлы, даже если они нужны для диагностики.
- **Исправление:** Использовать точное совпадение суффикса `_{session_id}.` или создавать поддиректорию `plots/{session_id}/` для изоляции. Убрать дублирование вызова в finally.
- **Что даст:** Предотвращается случайное удаление файлов других сессий и двойной обход директории.

### M45. brainstorm_tool: магическая строка-шаблон в system_prompt содержит незакрытую f-строку
- **Модуль:** custom_tools (прочие инструменты)
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/brainstorm_tool.py:369`
- **Проблема:** В `multi_model_brainstorm`, строка 369: `"{add_prompt}"` внутри обычной строки `system_prompt=\"...{add_prompt}\"` — это **не** f-строка (нет префикса `f`). Переменная `add_prompt` определена на строке 252 и содержит текущую дату, но она не подставляется в `system_prompt` — туда попадает литеральная строка `"{add_prompt}"`. Сравнение: строки 197-199 в `brainstorm_with_method` используют `call_openai_api(system_prompt=method['system_prompt'] + add_prompt)` правильно, добавляя `add_prompt` конкатенацией. В `multi_model_brainstorm` это не работает.
- **Влияние:** В системном промпте синтеза вместо реальной даты/времени агент видит буквальный текст `{add_prompt}` — minor, но логически неверно.
- **Исправление:** Изменить на `f"""...{add_prompt}"""` или конкатенировать `system_prompt + add_prompt` перед вызовом.
- **Что даст:** Корректная передача временного контекста в синтезирующий промпт.

### M46. web_research.py: синхронный DuckDuckGo вызов через asyncio.get_event_loop() (deprecated)
- **Модуль:** custom_tools (прочие инструменты)
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/web_research.py:236`
- **Проблема:** `asyncio.get_event_loop()` в строках 236, 314, 549 вызывается внутри coroutine. В Python 3.10+ этот вызов внутри coroutine возвращает running loop, но считается deprecated — следует использовать `asyncio.get_running_loop()`. При этом в `_safe_run_async` создаётся новый loop через `asyncio.run()` в executor, после чего `asyncio.get_event_loop()` может вернуть другой объект. Кроме того, `loop.run_in_executor(None, self.duckduckgo_search, ...)` при блокировке DuckDuckGo (3 попытки с `time.sleep`) держит thread pool занятым до 9+ секунд на каждый запрос.
- **Влияние:** Потенциальные DeprecationWarning → ошибки при обновлении Python; задержки в event loop при множественных запросах.
- **Исправление:** Заменить `asyncio.get_event_loop()` на `asyncio.get_running_loop()` везде внутри coroutine.
- **Что даст:** Совместимость с Python 3.12+ и корректная семантика.

### M47. Неограниченная рекурсия в filter_value_conditions через вложенный dict с ключом 'value'
- **Модуль:** Text2SQL: генерация SQL
- **Категория:** correctness | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `custom_tools/text_to_sql/sql_builder.py:438`
- **Проблема:** В ветке `isinstance(value, dict)` без ключа `operator` код обрабатывает ключ `value` рекурсивным вызовом `filter_value_conditions(expr, value.get("value"), filter_info, dsn=dsn)` (строки 438-444). Если вложенный `value["value"]` тоже является dict с ключом `value`, рекурсия продолжается без ограничения глубины. Контракт входных данных (dict от LLM) не гарантирует конечную вложенность.
- **Влияние:** Вредоносный или некорректно сформированный filter-объект от LLM вызовет RecursionError, что роняет весь worker-процесс в production.
- **Исправление:** Добавить параметр `max_depth: int = 10` и декрементировать его при рекурсии; при `max_depth <= 0` возвращать `None` (ошибка). Аналогично для вложенного `operator` (строка 406).
- **Что даст:** Защита от DoS через глубоко вложенный filter-объект.
- **Заметка верификатора:** Баг реален. На sql_builder.py:399-445 для dict БЕЗ ключа operator: ветка 400 пропускается, start/end/min/max/values отсутствуют, а ветка 438 'value' in value рекурсивно вызывает filter_value_conditions(expr, value.get("value"), filter_info) на строке 439-440. Если вложенный value снова dict без operator вида {"value": {"value": {...}}}, рекурсия повторяет тот же блок без какого-либо ограничения глубины — нет ни depth-параметра, ни sys.setrecursionlimit-обработки (grep по recursion пусто). Цепочка sql_generation_plugin -> generate_sql -> _generate_from_linked_entities -> build_sql_from_linked_entities -> build_filter_clauses -> filter_value_conditions не обёрнута в try/except, так что RecursionError пробрасывается. НО severity завышена: (1) RecursionError — ловимое исключение (подкласс RuntimeError), а не аппаратное падение процесса; формулировка "роняет весь worker-процесс" преувеличена — реалистичный эффект это упавший единичный запрос/tool-вызов, фреймворк агента обычно ловит per-request исключения инструмента. (2) Триггер требует ~1000 уровней вложенности в filter-объекте; для добросовестного LLM это неправдоподобно, эксплуатируемо лишь специально сформированным/битым входом. Это валидный robustness-баг (нужен depth-guard / валидация вложенности фильтра), но не high; medium.

### M48. JoinBuilder.build_joins квотирует идентификаторы без DSN — игнорирует диалект
- **Модуль:** Text2SQL: генерация SQL
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/join_builder.py:73`
- **Проблема:** В методе `build_joins` все вызовы `quote_identifier(b)`, `quote_identifier(a)`, `quote_identifier(a_col)`, `quote_identifier(b_col)` не передают `dsn`. Функция `quote_identifier` без `dsn` использует `get_current_dialect_name(None)` → `"sql"`, что даёт двойные кавычки (ANSI). Для MySQL/Impala нужны бэктики. Вызовы `infer_joins_by_convention` возвращают joins, которые используются через `build_joins`, и DSN в `JoinBuilder` есть — он передаётся в `__init__` через `db_schema`, но не как `dsn`.
- **Влияние:** На MySQL/Impala генерируются синтаксически некорректные JOIN-клаузы с двойными кавычками вместо бэктиков, что ведёт к ошибкам выполнения в production.
- **Исправление:** Добавить `dsn: str | None = None` в `__init__` и `build_joins`, передавать его в `quote_identifier(b, dsn=self.dsn)` и т.д.
- **Что даст:** Корректные JOIN для всех диалектов, включая MySQL/Impala.

### M49. `select_like` в `CTECollector.collect_cte_columns` не включает `Intersect`/`Except` (sqlglot 27+)
- **Модуль:** Text2SQL: валидаторы безопасности
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/validators/_schema_cte.py:45`
- **Проблема:** `select_like = (exp.Select, getattr(exp, "Union", ()))`. В sqlglot 27+ `exp.Intersect` и `exp.Except` НЕ наследуются от `exp.Union` (об этом явно написано в комментарии к `_set_operation_classes()` в safety.py). Поэтому CTE вида `WITH x AS (SELECT a INTERSECT SELECT b) SELECT * FROM x` не распознаётся как `select_like`: ветка `elif isinstance(select_expr, select_like)` не срабатывает, CTE регистрируется с `{"*"}` (строка 57). Это не критично (просто отключает проверку колонок для такого CTE), но логически некорректно.
- **Влияние:** Ложные пропуски UNKNOWN_COLUMN для CTE с INTERSECT/EXCEPT-телом; снижение качества schema-aware валидации.
- **Исправление:** Использовать тот же `_set_operation_classes()` из safety.py (или переместить его в shared-утилиту) для построения `select_like`: `select_like = (exp.Select,) + _set_operation_classes()`.
- **Что даст:** Унифицирует обработку set-операций в schema-aware валидаторе с логикой safety.py.

### M50. `enforce_row_limit` рендерит SQL через `target.sql()` — семантически небезопасная модификация
- **Модуль:** Text2SQL: валидаторы безопасности
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/validators/schema_limiter.py:373`
- **Проблема:** При добавлении LIMIT метод выполняет `rendered = target.sql(dialect=dialect)` и возвращает `f"{rendered.rstrip(';').rstrip()} LIMIT {default_limit}"`. `sqlglot.Expression.sql()` нормализует и переписывает запрос: может изменить регистр ключевых слов, кавычки идентификаторов, порядок аргументов в некоторых диалектных формах. Исходный SQL, прошедший валидацию, может стать отличным от отрендеренного — изменятся квалификаторы, алиасы, или кавычки потеряются, что может вызвать ошибку выполнения в строго-чувствительных к кавычкам СУБД.
- **Влияние:** Запрос с кастомными идентификаторами (`"My Column"`, `` `table-name` ``) после перерендера может стать синтаксически невалидным или семантически иным в целевой СУБД. Функция помечена как `UNUSED utility`, но её контрактные тесты существуют — при интеграции в db_exec этот эффект проявится.
- **Исправление:** Вместо полного перерендера дописывать LIMIT к хвосту ИСХОДНОЙ строки: `sql.rstrip(';').rstrip() + f' LIMIT {default_limit}'`. Это ровно то, что делает regex-fallback (строка 393) — унифицировать оба пути.
- **Что даст:** Сохраняет исходный текст запроса без нормализации; устраняет расхождение между AST-путём и regex-fallback.

### M51. step.timeout игнорируется — таймаут шага всегда 300 секунд
- **Модуль:** Workflow Engine: ядро
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `workflow/retry_engine.py:131-139`
- **Проблема:** `_execute_with_timeout` (строка 134) использует `timeout = timeout or 300` — жёстко зашитый дефолт 5 минут. `execute_with_retry` вызывает `_execute_with_timeout(step_func, context)` без передачи timeout (строка 80). `step.timeout` из WorkflowStep передаётся в `execute_with_retry`, но этот параметр в сигнатуре метода отсутствует. Таким образом, поле `timeout` шага фактически не используется.
- **Влияние:** Шаги с короткими таймаутами (например, 30 сек) будут ждать 300 сек. Долгие workflows не смогут быть принудительно завершены вовремя.
- **Исправление:** Добавить параметр `timeout` в `execute_with_retry`, передавать его из `_execute_workflow_step` (`step.timeout or ...`), пробрасывать в `_execute_with_timeout`.
- **Что даст:** Соблюдение задекларированных в YAML/WorkflowStep таймаутов.

### M52. success_rate в ParallelWorkflowExecutor включает SKIPPED шаги в числитель
- **Модуль:** Workflow Engine: resilience/orchestration/intelligence
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `workflow/orchestration/parallel_executor.py:328`
- **Проблема:** Формула `success_rate = (total - failed) / total * 100` (строка 328) считает SKIPPED шаги как успешные. Если workflow пропустил 90% шагов из-за stop_on_failure или failed deps, success_rate выдаст 90%, хотя реально выполнено 0 шагов успешно. Поле `sequential_executed` в `execution_stats` никогда не инкрементируется (только `parallel_executed`), что также делает `parallel_percentage` неверной.
- **Влияние:** Метрики качества выполнения workflow вводят в заблуждение: dashboard/monitoring показывает высокий success_rate при фактически провалившемся workflow. Отладка сбоев затрудняется.
- **Исправление:** Изменить формулу: `success_rate = parallel_executed / total * 100` или явно считать только COMPLETED шаги. Инкрементировать `sequential_executed` для шагов, выполненных после ожидания.
- **Что даст:** Корректные метрики для мониторинга и алертинга.

### M53. Дублирование синглтон-реестра: два независимых EventStore для одного файла
- **Модуль:** Backend FastAPI / AG-UI
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `backend/fastapi_app/agui/service.py:110`
- **Проблема:** _AGUI_EVENT_STORE — отдельный глобальный синглтон EventStore в service.py, указывающий на тот же файл data/agui_events.db, что и store = EventStore(str(DB_PATH)) в main.py (строка 34). Таким образом одновременно существуют два SQLite-соединения к одному файлу. WAL-режим снижает риск, но не устраняет: при одновременных BEGIN IMMEDIATE транзакциях возможен SQLITE_BUSY / контентион. Более серьёзно — _workflow_result_from_store в service.py и RunManager в main.py читают один файл через разные объекты, что затрудняет рефакторинг и добавляет неявную зависимость.
- **Влияние:** SQLITE_BUSY под нагрузкой. Непонятный ownership: непонятно, кто 'владеет' store.
- **Исправление:** Передавать store из main.py в handle_service_action через параметр или DI; убрать _AGUI_EVENT_STORE из service.py.
- **Что даст:** Единственный owner соединения с БД, упрощение кода.

### M54. _save_db_test_configs не использует атомарную запись (в отличие от secrets)
- **Модуль:** Backend FastAPI / AG-UI
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `backend/fastapi_app/agui/service.py:332`
- **Проблема:** _save_db_test_configs делает path.write_text(json.dumps(...)) напрямую — не через tempfile+os.replace, как это сделано в _save_db_test_config_secrets (строки 313–329). При сбое посередине записи (OOM, SIGKILL) файл db_test_configs.json будет повреждён или обрезан. Следующая загрузка вернёт пустой dict, что сотрёт все DSN-конфигурации.
- **Влияние:** Потеря конфигурации публичных DSN при нештатном завершении процесса.
- **Исправление:** Использовать ту же tempfile+os.replace логику, что в _save_db_test_config_secrets.
- **Что даст:** Атомарность записи конфига, защита от corruption.

### M55. Сохранение workflow YAML без валидации YAML-синтаксиса
- **Модуль:** Backend FastAPI / AG-UI
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `backend/fastapi_app/agui/service.py:2924`
- **Проблема:** workflows.save_yaml записывает yaml_content напрямую в файл (workflow_path.write_text(str(yaml_content))) без предварительного yaml.safe_load для проверки корректности. Некорректный YAML файл сломает WorkflowManager при следующем list_workflows() или start_workflow(). Бэкап создаётся, но он тоже может стать невалидным.
- **Влияние:** Невалидный YAML в workflow_pipelines/ вызовет неочевидные ошибки при попытке запуска пайплайна.
- **Исправление:** Добавить yaml.safe_load(yaml_content) перед записью; при ParseError — вернуть ошибку клиенту.
- **Что даст:** Fail-fast валидация до сохранения файла.

### M56. SQLite: connect() игнорирует :memory: путь из file: DSN и не нормализует его
- **Модуль:** DB Plugins
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `db_plugins/sqlite.py:29-41`
- **Проблема:** Когда DSN начинается с `file:`, код читает mode из query string (строка 34), но не проверяет случай `path == ':memory:'`. Если кто-то передаст `file::memory:?mode=memory`, функция пойдёт по ветке `else` (строки 39-41) и попытается открыть read-only файл `:memory:`, что упадёт с ошибкой (sqlite3 не поддерживает `file::memory:?mode=ro`). Только ветка `sqlite:///` (строки 23-28) явно обрабатывает `:memory:`.
- **Влияние:** Неожиданное исключение при использовании `file::memory:` DSN, хотя пользователь ожидает in-memory подключение.
- **Исправление:** После парсинга path в ветке `file:` добавить: `if path in {':memory:', '/:memory:'}: return sqlite3.connect(':memory:')`.
- **Что даст:** Консистентное поведение для in-memory DSN независимо от формата.

### M57. Некорректный filter в get_goals при семантическом поиске: ChromaDB не поддерживает AND-объединение через словарь верхнего уровня
- **Модуль:** Memory / RAG
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `memory/tools.py:1113`
- **Проблема:** Фильтр передан как `{"session_id": {"$eq": session_id}, "type": {"$eq": "goal"}}` — два условия на верхнем уровне одного словаря. ChromaDB поддерживает несколько условий на верхнем уровне как неявный AND только в некоторых версиях; правильный способ — `{"$and": [{...}, {...}]}`. Все остальные места в коде используют явный `$and` (manager.py:350, streamlit_api.py:338).
- **Влияние:** Семантический поиск целей может молча возвращать неотфильтрованные результаты или падать в fallback, игнорируя фильтр по session_id или type.
- **Исправление:** Заменить на `{"$and": [{"session_id": {"$eq": session_id}}, {"type": {"$eq": "goal"}}]}`.
- **Что даст:** Корректная фильтрация по session_id при семантическом поиске целей.

### M58. set_summary_model игнорирует переданную модель — всегда подставляет глобальный model_summary
- **Модуль:** Memory / RAG
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `memory/rag_memory.py:836`
- **Проблема:** Метод `set_summary_model(self, model)` принимает параметр `model`, но всегда записывает `self._summary_model = model_summary` (глобальная переменная). Строка `#self._summary_model = model` закомментирована. В `create_rag_memory` вызов `rag_memory.set_summary_model(profile_config['model'])` (строка 1088) при этом никогда не использует переданную модель из профиля.
- **Влияние:** Конфигурация модели суммаризации из YAML-профиля полностью игнорируется. Все агенты всегда используют одну глобальную модель суммаризации вне зависимости от профиля.
- **Исправление:** Раскомментировать `self._summary_model = model` и удалить строку с `model_summary`.
- **Что даст:** Модели суммаризации из профилей агентов начнут применяться.

### M59. Mutable default argument в публичной функции
- **Модуль:** Storybook: инструменты генерации
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/artist_batch_edit.py:1747`
- **Проблема:** `def artist_agent_batch_edit_tool(..., items_to_edit: Dict[int, Any] = {}, ...)` использует изменяемый словарь как default argument. В Python default аргументы создаются один раз при определении функции. Если где-то внутри тела функции объект `items_to_edit` будет изменён, это повредит последующие вызовы. Хотя в текущем коде `items_to_edit` не используется в теле функции (параметр объявлен, но нигде не читается), это создаёт риск при будущем рефакторинге.
- **Влияние:** При дальнейшем использовании параметра: state leakage между вызовами, нереvoducible баги при параллельных вызовах.
- **Исправление:** Заменить на `items_to_edit: Optional[Dict[int, Any]] = None` и обрабатывать None внутри функции.
- **Что даст:** Устраняет классический Python gotcha, документирует намерение через типы.

### M60. Параметр payload в _edit_chapters_batch передаётся как str, но json.dumps ожидает dict
- **Модуль:** Storybook: инструменты генерации
- **Категория:** correctness | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `custom_tools/storybook/story_editor.py:172-177`
- **Проблема:** На строке 172 формируется строка `payload` (конкатенация f-строк), а на строке 173 к ней прибавляется ещё строка с `{chapters_to_edit}` (не JSON). На строке 177 эта строка-payload передаётся в `_edit_chapters_batch`. Внутри `_edit_chapters_batch` (строка 272) вызывается `json.dumps(payload, ...)` — где payload уже является str, а не dict. `json.dumps` на строке превратит её в JSON-строку (с экранированием кавычек), а не в JSON-объект. Параллельно определён `payload_json` (строка 156), который является правильным dict, но не используется.
- **Влияние:** При `edit_all_chapters=True` в LLM отправляется некорректный payload: двойная JSON-сериализация строки вместо структурированного объекта. Качество редактирования деградирует или LLM возвращает ошибку.
- **Исправление:** Заменить строку-payload на использование уже определённого `payload_json` dict в `_edit_chapters_batch`.
- **Что даст:** Корректная передача контекста LLM при батч-редактировании глав.
- **Заметка верификатора:** Подтверждено по фактам: на 172 payload — это f-string (конкатенация), на 173 дописывается обычная строка с литералом `{chapters_to_edit}` (не f-string, плейсхолдер уходит как есть), правильный dict payload_json (157) не используется, а _edit_chapters_batch делает json.dumps(payload) над str -> двойная сериализация/экранирование. Но severity завышена: путь срабатывает только при edit_all_chapters=True (не default — default False идёт через _edit_single_chapter с корректным dict). Данные при этом всё равно доходят до LLM (интерполированы в f-string на 172), просто в плохом формате + лишний литерал; это деградация качества, а не полный отказ. К тому же поля characters/locations/consistency тут всё равно пусты из-за бага locals() (находка 3). Понижаю до medium.

### M61. Жёстко перезаписанная длительность видео: _parse_duration_from_timing вызывается впустую
- **Модуль:** Storybook: видео/shots-генераторы
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/video_generator.py:308,317 / video_generator_mm_tool.py:325,327`
- **Проблема:** В обоих файлах вычисленная из timing длительность немедленно перезаписывается константой: `duration = _parse_duration_from_timing(timing)` следует `duration = 5` (video_generator.py:317) и `duration = 6` (video_generator_mm_tool.py:327). Функция _parse_duration_from_timing вызывается, но её результат игнорируется. Аналогично в video_generator_aitunnel_tool.py:741 в default-блоке стоит `duration = 6` но это уже в `except`-ветке, это нормально.
- **Влияние:** Параметр timing из сценария полностью игнорируется. Все видео генерируются с фиксированной длительностью 5/6 секунд независимо от заданных тайминг-данных. Kling AI поддерживает 10 секунд, но эта возможность недостижима.
- **Исправление:** Убрать строки `duration = 5` (video_generator.py:317) и `duration = 6` (video_generator_mm_tool.py:327) либо явно закомментировать с объяснением.
- **Что даст:** Корректное использование timing из сценария; возможность генерировать 10-секундные ролики.

### M62. encode_jwt_token использует глобальные ak/sk, которые могут быть None
- **Модуль:** Storybook: видео/shots-генераторы
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/video_generator.py:42-43,310`
- **Проблема:** Глобальные переменные `ak = os.getenv('KLING_API_KEY')` и `sk = os.getenv('KLING_API_SECRET_KEY')` считываются при импорте модуля (до load_env_file на строке 24 — НАРУШЕНИЕ ПОРЯДКА: load_env_file вызывается на строке 24, а ak/sk присваиваются на строках 42-43, то есть ПОСЛЕ load_env_file, это нормально). Однако если ключи отсутствуют в .env, то ak=None, sk=None. В `_generate_single_video` (строка 310) вызывается `encode_jwt_token(ak, sk)` с None-значениями — jwt.encode с secret=None поднимет исключение внутри try-блока, что будет поймано, но запись в лог не раскроет причину.
- **Влияние:** При отсутствии KLING_API_SECRET_KEY функция упадёт с невнятным исключением вместо раннего возврата ошибки конфигурации.
- **Исправление:** Добавить проверку sk is not None перед вызовом encode_jwt_token, возвращать structured error. Переместить считывание ak/sk внутрь функции (аналогично тому, как AITUNNEL-tool передаёт api_key параметром).
- **Что даст:** Чёткое сообщение об ошибке конфигурации вместо непрозрачного исключения при JWT.

### M63. Захардкоденный project_id "ryaba" в шаблоне нового кадра
- **Модуль:** StoryBookManager (десктоп-приложение)
- **Категория:** correctness | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `StoryBookManager/gui/editor_panel.py:1131`
- **Проблема:** В методе `add_shot()` поле `"project_id"` нового кадра захардкодено строкой `"ryaba"` вместо `self.current_project.project_id`. Данный кадр попадает в `self.current_data["items"]` и затем сохраняется на диск, поэтому все вновь созданные кадры в любом проекте будут иметь `project_id: "ryaba"`.
- **Влияние:** Функциональный баг: shots.json любого проекта будет содержать невалидный project_id, что нарушит работу pipeline-шагов, опирающихся на это поле (artist_batch_shots, video_generator).
- **Исправление:** Заменить `"project_id": "ryaba"` на `"project_id": self.current_project.project_id if self.current_project else ""`.
- **Что даст:** Корректная привязка кадров к проекту, устраняет скрытую порчу данных.
- **Заметка верификатора:** Баг реален: editor_panel.py:1131 в add_shot() хардкодит "project_id": "ryaba". Поле сохраняется на диск: new_shot кладётся в current_data['items'] (стр.1151), затем sync_structured_to_raw() переносит form_data обратно в current_data['items'] сохраняя все поля элемента (стр.2020), а save_current_file() пишет current_data в shots.json (стр.2304-2314). Значение 'ryaba' (явно остаток отладки на тестовом проекте) персистится в каждый новый кадр. Однако заявленный impact завышен: в данной кодовой базе НИ ОДИН шаг не читает per-shot project_id из items — grep показывает, что project_id везде берётся из current_project.project_id и подаётся в pipeline как execution/context variable (pipeline_runner.py:361,508), а не из элемента кадра. Шаги artist_batch_shots/video_generator исполняются внешним движком и получают project_id из контекста. Поэтому это дефект чистоты данных (мусорное/неверное поле в shots.json), а не поломка pipeline. Понижаю до medium.

### M64. Утечка Blob URL при быстром закрытии/открытии модала отчёта
- **Модуль:** Frontend (Next.js / React AG-UI)
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `frontend/client/src/app/components/sections/TextToSqlSection.tsx:496`
- **Проблема:** В useEffect (строка 473-503) создаётся Blob URL и сохраняется в `reportPreviewUrlRef.current`. Cleanup-функция отзывает URL при unmount. Но если пользователь быстро закрывает и снова открывает модал, новый эффект может создать новый Blob URL до завершения предыдущего (asynchronous createPreview). Переменная `revoked` защищает от применения результата, но URL всё равно создаётся и не revoke-ится (строка 478: `if (revoked) return` — выход до revokeObjectURL). Та же логика в DynamicAgentsSection.tsx:260.
- **Влияние:** Утечка памяти: Blob URLs накапливаются в браузере. При большом количестве отчётов потребление памяти вкладки неконтролируемо растёт.
- **Исправление:** В ветке `if (revoked) return` добавить `URL.revokeObjectURL(url)` перед return.
- **Что даст:** Память браузера не утекает при быстрой навигации по модалам.

### M65. Мёртвый return внутри цикла optimize_prompt() делает retry-логику ненадёжной
- **Модуль:** prompt_optimizer + telemetry
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `prompt_optimizer/prompt_optimizer.py:379`
- **Проблема:** Строка `return original_prompt, False` (379) расположена внутри тела цикла `for attempt in range(max_retries)` после блока try/except, но не является частью ни одной ветви. Все пути через try-блок либо явно возвращают результат, либо вызывают continue/exception. Строка 379 недостижима — это мёртвый код. Симметричная ошибка отсутствует в optimize_description() (там цикл корректен), что указывает на ручное копирование с правкой.
- **Влияние:** Код работает корректно сейчас, но любая рефакторинг (добавление ветви без return) молча активирует некорректный ранний выход из цикла. Читаемость и поддерживаемость снижены.
- **Исправление:** Удалить строку 379. Добавить `return original_prompt, False` после цикла (вне его тела) как явный fallback.
- **Что даст:** Устраняет источник будущих ошибок и делает намерения очевидными.

### M66. Мутабельные поля dataclass с default=None — опасный паттерн
- **Модуль:** Ядро: configuration & streamlit API
- **Категория:** correctness | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `configuration_api.py:71-76`
- **Проблема:** SecurityConfig объявляет List-поля с аннотацией List[str] и default=None (строки 71-76). Это обходится через __post_init__, однако from_dict() (строка 195) может передать список из файла напрямую через **data.get('security', {}). Если YAML содержит секцию security без какого-либо из этих ключей, __post_init__ сработает и подставит дефолт — но если YAML передаёт пустой список [] вместо отсутствующего ключа, allowed_functions окажется пустым списком, что открывает путь для неожиданно разрешающего поведения в проверке SQL.
- **Влияние:** При неполном YAML allowed_sql_operations = [] вместо ['SELECT'], что может сломать SQL-валидацию на стороне потребителей конфига.
- **Исправление:** В from_dict() явно не передавать None-значения в конструктор или использовать dataclasses.field(default_factory=...) с обязательной валидацией непустого списка allowed_sql_operations.
- **Что даст:** Предотвращает тихое «пустое разрешение» SQL-операций при неполном конфигурационном файле.

### M67. from_table выбирается из первого entity без учёта приоритета — недетерминированный SQL
- **Модуль:** Text2SQL: генерация SQL
- **Категория:** correctness | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/sql_builder.py:61`
- **Проблема:** FROM-таблица выбирается первым `break` по `metrics + dimensions` (строки 61-64). Порядок metrics/dimensions определяется LLM-ответом. Если LLM вернул dimensions перед metrics — FROM будет из первой dimension, что может противоречить семантике запроса (например, для агрегата по fact-таблице FROM должна быть fact-таблица, а не dim). При этом остальные таблицы подключаются через JOIN, что меняет семантику LEFT JOIN (главная/подчинённая таблица).
- **Влияние:** Для запросов с несколькими таблицами возможен семантически неверный SQL: факты могут выпасть из результата при LEFT JOIN в неправильном направлении.
- **Исправление:** Добавить приоритет: FROM выбирается из `metrics` в первую очередь, только при их отсутствии — из `dimensions`. Или добавить явный ключ `primary_table` в контракт structured_context.
- **Что даст:** Детерминированная семантика FROM для смешанных запросов.

### M68. _FileLock.release(): os.close вызывается ПОСЛЕ сброса self._fd = None внутри finally — fd утекает при ошибке flock
- **Модуль:** Text2SQL: schema (cache/loader/memory)
- **Категория:** correctness | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/schema_memory_sqlite.py:162-168`
- **Проблема:** В `release()` порядок операций: `fcntl.flock(self._fd, LOCK_UN)` в `try`, затем в `finally`: `os.close(self._fd)` и `self._fd = None`. Если `fcntl.flock(LOCK_UN)` бросает (например, `EBADF` при закрытом fd), `finally` выполняется с тем же `self._fd` → `os.close` вызывается дважды (или на уже невалидном fd). Это отличается от паттерна в `acquire()`, где используется null-first (`fd = self._fd; self._fd = None; os.close(fd)`). В `release()` null-first не применён: `os.close(self._fd)` и `self._fd = None` в `finally` означают, что при исключении из `os.close` `self._fd` останется ненулевым и при повторном вызове `release()` fd закроется снова (двойной close — TOCTOU с чужим fd).
- **Влияние:** Двойной `os.close` в многопоточном окружении может закрыть fd, переоткрытый другим потоком, — нарушение изоляции. Проявляется редко (при EBADF в flock LOCK_UN), но потенциально катастрофично.
- **Исправление:** Применить null-first паттерн в `release()`, как в `acquire()`: `fd = self._fd; self._fd = None; try: fcntl.flock(fd, LOCK_UN) finally: os.close(fd)`.
- **Что даст:** Устранение TOCTOU с fd при ошибочном пути освобождения лока.

### M69. save_to_cache открывает SQLite-коннект без транзакции для deactivate-шага — нет rollback при частичном сбое
- **Модуль:** Text2SQL: schema (cache/loader/memory)
- **Категория:** correctness | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/schema_cache.py:492-518`
- **Проблема:** В `save_to_cache` деактивация старых записей выполняется через отдельный `conn` (строка 493), который закрывается в `finally` (строка 518) без явного `rollback` при ошибке. `SELECT step` — read-only, нет риска. Затем вызывается `memory_manager._deactivate_conflicting_records(conflicts)` (строка 525), который открывает **другой** коннект (через `db_handler._get_connection()`). Если `_deactivate_conflicting_records` деактивировал часть записей и упал, затем `save_memory` (строка 555) успешно вставляет новую запись — в БД сосуществуют: частично деактивированные старые записи (с `valid_to IS NULL`) и новая запись с тем же cache_key. Логика `load_from_cache` вернёт первую подходящую, но при `valid_to IS NULL` условии возможны дубликаты.
- **Влияние:** При сбоях в `_deactivate_conflicting_records` возникают дублированные активные записи с одинаковым cache_key, что приводит к непредсказуемому поведению кэша (возврат устаревшего результата).
- **Исправление:** Обернуть весь deactivate+save в одну транзакцию через `sqlite_chroma_transaction` из `_sqlite_chroma_tx.py`, либо добавить rollback-обработку при сбое `_deactivate_conflicting_records`.
- **Что даст:** Атомарность операции deactivate+insert; предотвращение cache inconsistency.

### M70. perform_linking: поле `error` формируется некорректно при частичном успехе с фильтрами
- **Модуль:** Text2SQL: schema-linking + RAG
- **Категория:** correctness | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/schema_linking/linking_orchestrator.py:379`
- **Проблема:** Выражение для поля `"error"` в итоговом return: `unlinked[0] if unlinked and not self._has_linked_entities({...}) and not has_linked_filters else None`. Если `unlinked` содержит диагностическое сообщение (например, `"LLM schema linking returned no result"`) от LLM-ошибки, а heuristic_fallback при этом УСПЕШНО заполнил хотя бы `filters`, то `has_linked_filters=True`, и `error=None` — хотя реальная LLM-ошибка была, и в `unlinked` она есть. Клиент видит `error=None`, `linking_strategy="heuristic"`, `unlinked_entities=["LLM..."]` — противоречие.
- **Влияние:** Мониторинг/аудит пропускают тихие деградации LLM-пути. Клиент может считать линковку успешной, когда LLM недоступна.
- **Исправление:** Разделить `unlinked_entities` на семантические имена и диагностические сообщения. Выставлять `error` на основе `linking_strategy` и источника `unlinked`, а не только наличия linked output.
- **Что даст:** Честная репортинг деградации; мониторинг видит реальную причину ошибки.

### M71. get_memory: early-return внутри семантической ветки обходит policy-фильтры и cache_kind routing
- **Модуль:** Memory / RAG
- **Категория:** correctness | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `memory/tools.py:575-602`
- **Проблема:** При пустом `step_filters` после порогового отсева (строка 575) и при пустом `semantic_search_results` (строки 600, 602) функция делает `return []` напрямую, не выполняя `_apply_default_cache_kind_routing` (строка 733) и `_apply_policy_filters` (строка 736). В нормальном сценарии эти фильтры не влияют на пустой список, но логически несогласованно: политика доступа должна применяться всегда, включая аудит-лог.
- **Влияние:** Технически для пустых результатов разницы нет. Но при изменении `_apply_policy_filters` (например, для логирования отказов доступа) ранние return обходят этот код.
- **Исправление:** Заменить ранние `return []` на `records = []; use_semantic_results = False` и дать коду дойти до общего блока обработки политик.
- **Что даст:** Единая точка применения политик доступа для всех путей выполнения.

### M72. Мёртвый код: _uninstall_step_hook отсутствует в finally блока run_full_pipeline при cancel
- **Модуль:** StoryBookManager (десктоп-приложение)
- **Категория:** correctness | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `StoryBookManager/core/pipeline_runner.py:280`
- **Проблема:** `run_full_pipeline` вызывает `_install_step_hook`, который переписывает `engine._on_step_completed` и `engine._execute_workflow_step`. При нормальном завершении и при исключении `_uninstall_step_hook()` корректно вызывается в `finally`. Однако в `resume_workflow_from_checkpoint` (строки 595–628) `_install_step_hook` вызывается *после* `checkpoint = await ...`, но если `execute_workflow` завершается со статусом `success` и в success-ветке (строка 611) бросается исключение при вызове `_build_runner_response`, `finally` всё равно вызовет `_uninstall_step_hook`. Это корректно. Реальная проблема: если `_install_step_hook` вызван, а затем `_install_step_hook` вызывается снова (параллельный запуск из двух потоков — см. гонку выше), `_original_on_step_completed` уже содержит hooked-версию, и при uninstall движок получит бесконечно зацикленный hook.
- **Влияние:** При параллельном запуске двух pipeline engine получает рекурсивно вложенные hooks, что приводит к stack overflow или бесконечному вызову.
- **Исправление:** Добавить проверку `if self._original_on_step_completed is not None: return` в начало `_install_step_hook`, либо защитить вызов lock'ом.
- **Что даст:** Исключает двойную установку hook и связанный stack overflow.

### M73. AUTHORIZED_IMPORTS — мёртвый код, вводящий в заблуждение
- **Модуль:** Ядро: оркестрация агентов
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `agent_factory.py:88-128`
- **Проблема:** Константа `AUTHORIZED_IMPORTS` определена на строках 88-128 со списком из ~30 модулей, но не используется нигде в файле и нигде в проекте (grep по всем .py). `CodeAgent` создаётся с `additional_authorized_imports='*'` (строка 336), полностью игнорируя эту константу.
- **Влияние:** Код вводит в заблуждение: читатель думает, что импорты ограничены, но на деле ограничений нет. Создаёт иллюзию контроля безопасности, которого не существует.
- **Исправление:** Удалить константу `AUTHORIZED_IMPORTS` или подключить её через `additional_authorized_imports=AUTHORIZED_IMPORTS` вместо `'*'`.
- **Что даст:** Устраняет ложное ощущение безопасности, улучшает читаемость кода.

### M74. SchemaRelevanceFilter подключён к prod-flow через SchemaContextBuilder несмотря на пометку «deprecated/unconnected»
- **Модуль:** Text2SQL: schema (cache/loader/memory)
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/schema_filtering.py:307-315`
- **Проблема:** Класс `SchemaRelevanceFilter` помечен docstring'ом: «класс не используется в prod-пайплайне... сохранён, но не подключён». Однако `SchemaContextBuilder.build_relevant_schema_context` (строка 255) вызывает `memory_manager.find_semantic_relevant_tables`, который является альтернативным search-путём. Сам `SchemaRelevanceFilter` (substring matching) не вызывается в prod, но его методы покрыты тестами — тесты могут давать ложное чувство безопасности при рефакторинге. Кроме того, `score_table_relevance` (строка 449): `score = score * (matching_columns / total_columns) * 100` при `matching_columns=0` всегда вернёт 0 из-за умножения на 0, даже если `score` был 100 (точное совпадение имени). Это логический баг в dead-code, но если класс когда-либо подключат — поведение будет неожиданным.
- **Влияние:** Логический баг в `score_table_relevance` (scoring=0 при exact name match с 0 matched columns) создаст неверное ранжирование при активации класса. Тесты на этот метод могут не покрывать edge case.
- **Исправление:** Либо удалить класс (если он действительно мёртв), либо исправить формулу: применять нормализацию `(matching_columns / total_columns) * 100` только если `matching_columns > 0`, иначе score не умножается. Обновить docstring, убрав «не подключён» если класс фактически используется.
- **Что даст:** Удаление мёртвого кода снижает cognitive overhead; исправление логического бага предотвращает регрессию при подключении.

### M75. _apply_decision_modifications мутирует WorkflowStep.task — мёртвый код с side effect
- **Модуль:** Workflow Engine: ядро
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `workflow/enhanced_engine.py:812-823`
- **Проблема:** `_apply_decision_modifications` (строки 812-823) определён, но нигде не вызывается в кодовой базе. Метод непосредственно мутирует `step.task` (`step.task += ...`), что изменит задачу для ВСЕХ последующих retry этого шага (step — один объект из workflow_def.steps, разделяемый между попытками). Если метод когда-либо будет вызван, эффект накопится через несколько итераций.
- **Влияние:** Мёртвый код — немедленного эффекта нет, но если кто-то вызовет метод, шаг получит exponentially growing task через несколько retry.
- **Исправление:** Удалить метод или реализовать через shallow-copy шага (как это сделано в `_step_with_substituted_metadata`).
- **Что даст:** Устранение потенциальной мутации разделяемого состояния; уменьшение мёртвого кода.

### M76. Мёртвый вызов несуществующего метода export_event()
- **Модуль:** prompt_optimizer + telemetry
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `telemetry/smolagents_telemetry.py:600`
- **Проблема:** finish_run_trace() создаёт новый LocalJSONLExporter и вызывает exporter.export_event(...) — метода с таким именем в классе не существует (есть только export(spans) и shutdown()). Исключение AttributeError съедается блоком `except Exception: pass` на строке 605. Служебный маркер завершения трассы не записывается никогда.
- **Влияние:** UI-логика определения завершённости трасс может работать некорректно (помечать активные трассы как незавершённые). Баг незаметен в логах.
- **Исправление:** Удалить мёртвый блок try/except (строки 597-606) или реализовать export_event() в LocalJSONLExporter. Если маркер нужен — писать вручную через _write_trace_event().
- **Что даст:** Устраняет молчаливый сбой и возможную путаницу в статусах трасс.
- **Заметка верификатора:** Метод export_event действительно отсутствует (в классе только export(spans):225 и shutdown():385), grep подтверждает единственное использование на :600. Вызов всегда бросает AttributeError, проглатываемый except Exception: pass (605), маркер run_finished не пишется никогда — это реальный мёртвый код. НО severity high завышена: непосредственно перед dead-вызовом span корректно завершается span.end() (595) с проставленным end_time (594), и завершённый корневой span штатно экспортируется. Логика is_trace_completed/check_and_mark_incomplete_traces определяет завершённость по наличию end_time у корневого agent_run_ span, который присутствует. Практический эффект на UI близок к нулю; это косметический мёртвый код, не функциональный сбой определения завершённости.

### M77. analyze_task: широкий except глушит ValueError валидации — невалидные типы агентов незаметно игнорируются
- **Модуль:** Ядро: оркестрация агентов
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `agent_system.py:443-447`
- **Проблема:** Блок `except Exception as e` на строке 443 перехватывает все исключения, включая `ValueError("Обнаружены недопустимые типы агентов")` (строка 437). Вместо propagation ошибки система молча откатывается к `['researcher', 'manager']`. Traceback не логируется — только `print(f"Ошибка при анализе задачи: {str(e)}")`.
- **Влияние:** Если LLM возвращает агентов, которых нет в AGENT_PROFILES (галлюцинация или prompt injection), ошибка скрывается. Пользователь получает неожиданный fallback-результат без индикации проблемы. Усложняет диагностику ошибок конфигурации.
- **Исправление:** Не проглатывать `ValueError` из валидации — либо re-raise, либо логировать с уровнем `logger.error` + traceback (`traceback.format_exc()`). Сохранить fallback только для сетевых/API ошибок (`except (ConnectionError, TimeoutError, ...)`), а не для `Exception` вообще.
- **Что даст:** Видимость ошибок конфигурации и LLM-галлюцинаций, более предсказуемое поведение системы.

### M78. Bare except в SpanAwareFormatter.format() скрывает KeyboardInterrupt и SystemExit
- **Модуль:** Ядро: configuration & streamlit API
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `configuration_api.py:418-419`
- **Проблема:** Строка 418: `except:` (без типа исключения) в методе format() логгера перехватывает любое исключение включая KeyboardInterrupt, SystemExit, GeneratorExit. Этот код выполняется при каждом вызове logging в runtime. Потенциально маскирует сигнал завершения во время логирования.
- **Влияние:** Ctrl+C или sys.exit() внутри функции format() будет поглощён. В реальных условиях это редко, но является нарушением базового правила обработки исключений Python.
- **Исправление:** Заменить на `except Exception: pass`.
- **Что даст:** Корректная обработка системных сигналов при логировании.

### M79. JSON-форматтер использует %-интерполяцию без LogRecord defaults для run_id/span_id
- **Модуль:** Ядро: configuration & streamlit API
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `configuration_api.py:447`
- **Проблема:** Форматная строка для json-формата (строка 447) содержит %(run_id)s и %(span_id)s, однако стандартный LogRecord не имеет этих полей. Если лог-запись не создана через специальный handler (RunIdLogHandler), форматирование упадёт с KeyError/ValueError. Исключение будет поглощено logging-фреймворком (lastResort), но логи перестанут писаться.
- **Влияние:** При json-формате логи от большинства модулей системы тихо теряются.
- **Исправление:** Добавить defaults={'run_id': '', 'span_id': ''} в конструктор Formatter или использовать SpanAwareFormatter с явным добавлением атрибутов через record.__dict__.setdefault().
- **Что даст:** JSON-формат логов работает корректно для всех источников.

### M80. memory_archivist_tools: bare except глотают ошибки ChromaDB
- **Модуль:** custom_tools (прочие инструменты)
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/memory_archivist_tools.py:341`
- **Проблема:** В `_global_search` и `_get_global_stats` используются голые `except:` (строки 341, 492, 588, 595) без логирования и без reraise. В `_global_search` ошибка при парсинге строки `data_json` молча пропускается через `continue`; ошибка получения ChromaDB-статистики молча преобразуется в `"N/A"`. Трейсбек теряется полностью.
- **Влияние:** Скрытые сбои ChromaDB или SQLite не диагностируются; корректность результатов поиска неизвестна при сбоях.
- **Исправление:** Заменить голые `except:` на `except Exception as e: logger.warning(...)` с сохранением трейсбека. В `_global_search` — логировать номер строки, вызвавшей ошибку.
- **Что даст:** Диагностируемость сбоев в production.

### M81. image_tools: утечка requests.Session в generate_image_tool и _edit_image_vse_post_edits_multipart
- **Модуль:** custom_tools (прочие инструменты)
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/image_tools.py:471`
- **Проблема:** В `generate_image_tool` создаётся `session = requests.Session()` (строка 471), но нигде не закрывается — нет `with session:` или `try/finally: session.close()`. Аналогично в `_post_generations_json` внутри `edit_image_vse_tool` (строка 1019). При исключении в `session.post()` или при декодировании `session` остаётся открытым.
- **Влияние:** Утечка TCP-соединений при долгой работе сервиса.
- **Исправление:** Использовать `with requests.Session() as session:` или добавить `finally: session.close()`.
- **Что даст:** Корректное освобождение ресурсов.

### M82. set_schema_ready_marker проглатывает все исключения — silent fallback
- **Модуль:** Text2SQL: schema (cache/loader/memory)
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/schema_memory_sqlite.py:1038-1039`
- **Проблема:** `except Exception as e: logger.warning(...)` — метод не реализует fail-fast, хотя все остальные методы `SchemaMemoryManager` (начиная с W2-T1) явно делают raise. Отсутствие маркера готовности невидимо для caller'а: `_get_database_schema` в `schema_linker.py:226` вызывает `set_schema_ready_marker` без проверки результата и без обработки исключения. Если `save_memory` сбоит именно здесь, индексация прошла успешно, но «готовность» не зафиксирована — любой код, полагающийся на `schema_ready`-маркер, будет ложно думать, что схема не готова.
- **Влияние:** Если downstream-код проверяет `schema_ready`-маркер для решения «запускать ли linking»: при систематическом сбое save_memory каждый запрос будет проходить полный re-linking вместо использования кэша.
- **Исправление:** По аналогии с остальными методами — пробрасывать исключение как `SchemaIndexingError` или хотя бы `logger.error`. Если маркер намеренно best-effort, добавить явный комментарий «намеренный soft-fail» для consistency с кодовой базой.
- **Что даст:** Консистентность обработки ошибок; диагностика сбоев memory-layer.

### M83. Bare `except Exception: continue` в _cleanup_orphaned_records проглатывает любые ошибки парсинга JSON
- **Модуль:** Text2SQL: schema-linking + RAG
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/rag/indexing.py:822`
- **Проблема:** Внутри цикла по строкам из agent_memory стоит `except Exception: continue`. Это поглощает не только ожидаемые json.JSONDecodeError (битый data_text), но и TypeError, AttributeError, KeyError и любые другие исключения — включая ошибки в логике работы с `file_path`. В результате реально осиротевшие записи могут тихо пропускаться без единого сообщения в логах.
- **Влияние:** Orphaned-записи остаются активными в agent_memory (valid_to IS NULL) и ChromaDB — RAG-поиск будет возвращать устаревшие SQL-примеры. Сбои маскируются полностью.
- **Исправление:** Сузить до `except (json.JSONDecodeError, TypeError, KeyError)` с `logger.debug(...)`. Остальные исключения — пробрасывать или логировать как warning.
- **Что даст:** Видимость реальных ошибок; orphaned-записи будут удаляться корректно.

### M84. Unclosed file handle при атомарной записи на ошибке flock
- **Модуль:** Storybook: инструменты генерации
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/shots_prompt_qa.py:1815-1843`
- **Проблема:** Файл открывается как `open(shots_path, 'a+', ...)` (не через context manager `with`) для получения flock, затем вручную разблокируется на строке 1843. Если `_write_json_atomic` или `_read_json` выбросят исключение между `flock(LOCK_EX)` и `flock(LOCK_UN)`, дескриптор файла останется заблокированным (EX lock не будет снят вручную). Хотя Python закроет fd при GC, это недетерминировано.
- **Влияние:** При исключении внутри блока lock файл shots.json может остаться заблокированным до GC, блокируя параллельные процессы или следующие вызовы инструмента.
- **Исправление:** Обернуть в `try/finally`: `try: fcntl.flock(lock_f, fcntl.LOCK_EX); ...; finally: fcntl.flock(lock_f, fcntl.LOCK_UN)`. Или использовать контекстный менеджер для блокировки.
- **Что даст:** Гарантированное снятие lock даже при исключениях.

### M85. prompt_engineer_tool открывает файлы без обработки ошибок
- **Модуль:** Storybook: инструменты генерации
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/prompt_engineer.py:1023-1052`
- **Проблема:** Серия `with open(...)` (7 файлов: beats.json, characters.json, locations.json, style_images.json, negative_prompt_list.txt, story.json, повторно beats.json) выполняется без try/except. При отсутствии любого из этих файлов функция упадёт с необработанным `FileNotFoundError`. Beats.json читается дважды: на строке 1023 для pre-check и на строке 1041 для реальной работы — лишний I/O.
- **Влияние:** Если хотя бы один из файлов отсутствует, весь pipeline падает с непонятным трейсбеком вместо информативного сообщения об ошибке.
- **Исправление:** Добавить проверку существования файлов с информативными `FileNotFoundError`, убрать дублирующее чтение beats.json.
- **Что даст:** Более понятная диагностика ошибок пайплайна.

### M86. Отсутствие timeout в requests.get при скачивании Veo-видео
- **Модуль:** Storybook: видео/shots-генераторы
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/video_generator_veo_tool.py:365`
- **Проблема:** `requests.get(download_url, stream=True)` вызывается без параметра timeout. Все остальные три реализации (Kling, MiniMax, AITUNNEL) передают явный timeout. При зависшем соединении с CDN/GCS поток будет заблокирован бесконечно, удерживая слот в ThreadPoolExecutor.
- **Влияние:** Бесконечное зависание потока при недоступном URL, блокировка всего пакета вызова.
- **Исправление:** Добавить `timeout=(30, 600)` по аналогии с остальными инструментами.
- **Что даст:** Предотвращение зависания потоков при сетевых сбоях.

### M87. Утечка ресурсов: cv2.VideoCapture не закрывается при исключении
- **Модуль:** StoryBookManager (десктоп-приложение)
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `StoryBookManager/core/media_processor.py:325`
- **Проблема:** В `get_video_thumbnail` объект `cap = cv2.VideoCapture(...)` открывается, затем `cap.release()` вызывается только на ветке успеха после `cap.read()`. Если `cv2.imwrite` или любой код между `cap.read()` и `cap.release()` выбрасывает исключение, `cap.release()` не будет вызван, и дескриптор видеофайла утечёт. Аналогичный паттерн в `_get_video_duration` (строки 276–291): `cap.release()` вызывается только в `if fps > 0`, пропуская вызов при `fps == 0`.
- **Влияние:** Накопление незакрытых файловых дескрипторов при просмотре большого каталога видео, что со временем приводит к EMFILE.
- **Исправление:** Обернуть `cap` в try/finally: `try: ... finally: cap.release()`. В `_get_video_duration` перенести `cap.release()` из ветки `if fps > 0` в finally.
- **Что даст:** Гарантированное освобождение видео-дескрипторов.

### M88. Отсутствие обработки ошибок в нескольких async-функциях ActionCardSections
- **Модуль:** Frontend (Next.js / React AG-UI)
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `frontend/client/src/app/components/sections/ActionCardSections.tsx:57`
- **Проблема:** Функции loadConfig, loadProviders, handleLlmTest, applyFilter, loadAnalytics, loadTraceDetails, loadTraceEvents, loadSpanLogs в TelemetrySection и ConfigSection вызываются без try/catch. Например, `const loadConfig = async () => { const resp = await runServiceAction(...); setConfig(...) }` — при сетевой ошибке unhandled rejection уходит в консоль, UI остаётся с null-состоянием без сообщения пользователю.
- **Влияние:** Молчаливые сбои: пользователь не видит что что-то пошло не так, UI остаётся в неопределённом состоянии (пустые формы конфига).
- **Исправление:** Обернуть все async-запросы в try/catch с установкой error-state. Для ConfigSection — добавить state `configError`.
- **Что даст:** Пользователь получает обратную связь при сбое загрузки конфигурации.

### M89. conftest.py: широкий except Exception проглатывает ошибки при импорте safety cache
- **Модуль:** Качество тестов
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `tests/conftest.py:54`
- **Проблема:** В autouse-фикстуре `_clear_text_to_sql_llm_safety_cache` блок `try: from ... import _clear_llm_safety_cache except Exception: yield; return` (строки 53–57) молча проглатывает любое исключение при импорте. Если модуль `custom_tools.text_to_sql.core._sql_generation_api` сломался — кэш не очищается, но тесты продолжают работать с устаревшим состоянием. Нет логирования, нет предупреждения.
- **Влияние:** При незаметном падении импорта LLM safety cache не сбрасывается между тестами, что приводит к "прорастанию" результатов monkeypatch'енных call_openai_api. Именно это является задокументированной причиной добавления этой фикстуры (комментарий строка 47).
- **Исправление:** Как минимум логировать warning при поглощении ImportError. Или использовать более узкое `except ImportError` вместо `except Exception`.
- **Что даст:** Видимость инфраструктурных проблем в тест-ранах; предотвращение скрытых flaky-ситуаций.

### M90. Отсутствие guard на None api_key при инициализации модели
- **Модуль:** Ядро: оркестрация агентов
- **Категория:** error-handling | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `agent_command.py:89-90`
- **Проблема:** `config.setdefault("api_key", os.getenv("OPENAI_API_KEY_DB"))` — если переменная окружения не установлена, в конфиг передаётся `api_key=None`. `RetryOpenAIServerModel` получает `None` и, вероятно, передаёт его в OpenAI-совместимый клиент. Аналогично для `api_base`.
- **Влияние:** При запуске без обязательных переменных окружения ошибка возникнет только при первом реальном вызове модели (lazy init через `lru_cache`), а не при старте. Сообщение об ошибке будет cryptic (401 или AttributeError в глубинах HTTP-клиента).
- **Исправление:** Добавить явную проверку: `if not os.getenv('OPENAI_API_KEY_DB'): raise EnvironmentError('OPENAI_API_KEY_DB is required')` — либо при старте приложения, либо внутри `_get_model`.
- **Что даст:** Быстрый fail при некорректной конфигурации вместо cryptic runtime error.

### M91. Неявная десериализация schema из памяти без проверки структуры в _get_schema_from_cache
- **Модуль:** Text2SQL: генерация SQL
- **Категория:** error-handling | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/sql_generator.py:155`
- **Проблема:** Метод `_get_schema_from_cache` строит `schema` из `table_info` без валидации типов колонок. Поле `columns` читается как список и строится dict; однако если memory возвращает вредоносный или повреждённый объект (например, `col["name"]` содержит `"; DROP TABLE`), он попадёт в schema-dict как ключ колонки. Эта schema затем передаётся в `schema_validator.validate_sql_against_schema`. Нет проверки, что значения `table_name`, `column["name"]` не содержат SQL-управляющих символов.
- **Влияние:** Если кэш памяти скомпрометирован или повреждён, вредоносные имена таблиц/колонок из кэша могут влиять на schema-validation logic или вызвать непредвиденное поведение downstream.
- **Исправление:** Добавить проверку `table_name` и `col["name"]` через `_SAFE_IDENTIFIER_RE` перед включением в schema-dict. Отклонять entries с невалидными именами с logging.warning.
- **Что даст:** Защита schema-кэша от отравления через memory store.

### M92. EventBus._process_events: задача не перезапускается после отмены, очередь теряет события
- **Модуль:** Workflow Engine: resilience/orchestration/intelligence
- **Категория:** error-handling | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `workflow/events/bus.py:128`
- **Проблема:** `_process_events` (строка 128) — единственный обработчик очереди. При отмене задачи через `stop()` (строка 59) и повторном вызове `start()` (строка 73, auto-start при publish), создаётся новая `_processor_task`, но события, попавшие в очередь между `_running.clear()` и созданием нового таска, не обрабатываются. Кроме того, если обработчик (`_dispatch`) бросает exception при `gather(..., return_exceptions=True)`, счётчик `failed` инкрементируется в `_call_handler`, но ошибка из `_dispatch` глотается на строке 138 (`except Exception`), и событие отмечается как обработанное (`processed += 1` на строке 137).
- **Влияние:** Потеря progress-событий при перезапуске bus. Ложный счётчик processed при реальных failures.
- **Исправление:** После `await self._process_events()` (в `start`) не сбрасывать `_running` до обработки очереди. Для счётчика: не инкрементировать `processed` если `_dispatch` выбросил исключение.
- **Что даст:** Корректный event sourcing, точная телеметрия.

### M93. Дублирование кода StreamToJsonl и _write_json_line в двух функциях
- **Модуль:** Ядро: configuration & streamlit API
- **Категория:** maintainability | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `agent_streamlit_api.py:86-187, 222-334`
- **Проблема:** Функции _setup_process_run_log_capture() и _agent_process_entry() содержат идентичные определения классов StreamToJsonl и вложенной функции _write_json_line (примерно 80 строк кода скопированы дословно). Отличие в _setup_process_run_log_capture: в flush() не вызывается _strip_ansi (строка 161), а в _agent_process_entry вызывается (строка 308) — рассинхрон поведения.
- **Влияние:** Логические исправления нужно вносить в двух местах одновременно; рассинхрон уже существует (разное поведение flush()). Высокий риск повторных расхождений.
- **Исправление:** Выделить StreamToJsonl и _write_json_line на уровень модуля. _agent_process_entry вызывает _setup_process_run_log_capture(run_id) вместо встроенной копии.
- **Что даст:** Единственное место изменения, исчезновение рассинхрона поведения flush().

### M94. load_llm_models_config() возвращает профиль, а не конфиг — несовпадение имени и типа
- **Модуль:** Text2SQL: YAML/конфиги
- **Категория:** maintainability | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/llm_models_config.py:167-173`
- **Проблема:** Функция называется `load_llm_models_config()` (по аналогии с остальными загрузчиками, возвращающими объект-конфиг), но её return-тип — `LLMModelsProfile`, а не `LLMModelsConfig`. Все вызывающие (`sql_generator.py:65`, `nlu.py:35`, `schema_linking/llm_linker.py:180`) вызывают `load_llm_models_config().get(section, key)` — метод `LLMModelsProfile`, которого нет у `LLMModelsConfig`. Контракт задокументирован верно в типе возврата строки 167, но сигнатура `-> LLMModelsProfile` — несоответствие смысловому названию функции и паттерну остальных loader'ов. Если кто-то попытается использовать результат как `LLMModelsConfig` (передать в функцию, ожидающую `.get_profile(...)`), получит AttributeError в рантайме.
- **Влияние:** Потенциальная путаница при рефакторинге; несоответствие публичного API конвенции пакета.
- **Исправление:** Переименовать в `load_llm_models_profile()` либо исправить docstring, явно указав что возвращается `LLMModelsProfile` активного профиля, и не вводить аналогию с `load_column_aliases_config()` / `load_type_categories_config()` которые возвращают полный объект-конфиг.
- **Что даст:** Устранение ловушки при code-reuse; единообразие API пакета.

### M95. DuckDB: estimate_row_count использует conn.execute() напрямую, а не _cursor() — несогласованность паттерна
- **Модуль:** DB Plugins
- **Категория:** maintainability | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `db_plugins/duckdb.py:227-232`
- **Проблема:** В estimate_row_count (и в get_basic_column_stats, строки 277-290) напрямую используется `conn.execute(sql).fetchone()` вместо `self._cursor(conn)`. DuckDB-cursor не требует явного закрытия, но это нарушает единообразный паттерн, используемый во всех остальных методах всех плагинов, и маскирует неожиданные ошибки: `except Exception: pass` (строка 231) без логирования полностью проглатывает все ошибки.
- **Влияние:** Silent failures в estimate_row_count возвращают магический fallback 1_000_000 без какого-либо сигнала — вызывающий код может принять неверное решение о стратегии семплирования.
- **Исправление:** Добавить логирование в `except Exception` как это сделано в других плагинах. Использовать `with self._cursor(conn) as cur:` для DuckDB тоже.
- **Что даст:** Видимость ошибок и единообразие кодовой базы.

### M96. Отладочные print() и debug-обработчики в продакшн-коде
- **Модуль:** StoryBookManager (десктоп-приложение)
- **Категория:** maintainability | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `StoryBookManager/gui/editor_panel.py:264`
- **Проблема:** Методы `handle_control_key` (строки 264–285), `handle_control_keycode` (289–309) и `debug_keypress` (311–315) содержат множество `print("🔥 DEBUG ...")` и `print("🎯 DEBUG ...")`. Обработчик `debug_keypress` привязан на `<KeyPress>` — каждое нажатие клавиши в raw-редакторе вызывает проверку и условный print. Параллельно в `universal_json_editor.py` (строки 2518, 2548, 2553, 2555, 2572, 2575) также живут print-ы в demo-коде. `config/settings.py` использует `print()` вместо `logger` для ошибок загрузки/сохранения.
- **Влияние:** Засорение stdout, снижение производительности при наборе текста (обработчик на каждую клавишу), утечка внутренней информации о keycodes в лог при продакшн-запуске.
- **Исправление:** Удалить все debug-print, метод `debug_keypress` и привязку `<KeyPress>`. Заменить `print()` в settings.py на вызовы `logger`.
- **Что даст:** Чистый вывод, нет лишней нагрузки на UI-поток.

### M97. Дублирование логики show_recent_activities между app.py и 01_Home.py
- **Модуль:** Streamlit UI
- **Категория:** maintainability | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `streamlit_app/app.py:388`
- **Проблема:** Функция `show_recent_activities()` практически идентична в `app.py` (строки 388–661) и `streamlit_app/pages/01_Home.py` (строки 203–422) — одинаковая трёхуровневая логика: active_runs → session_state fallback → telemetry fallback, одинаковый код определения статуса по spans. Единственное отличие — наличие кнопок отмены в app.py.
- **Влияние:** Баг в логике определения статуса нужно исправлять в двух местах. Исторически одно из мест отстаёт (в 01_Home.py нет topic-поля для workflow в fallback-ветке).
- **Исправление:** Вынести общую логику в `streamlit_app/utils.py` или `streamlit_app/components/recent_activities.py`, вызывать из обоих мест.
- **Что даст:** Одно место правки при изменении логики отображения активностей.

### M98. eslint ignoreDuringBuilds скрывает потенциальные ошибки типов и хуков
- **Модуль:** Frontend (Next.js / React AG-UI)
- **Категория:** maintainability | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `frontend/client/next.config.ts:7`
- **Проблема:** `eslint: { ignoreDuringBuilds: true }` полностью отключает ESLint на этапе сборки. Одновременно TextToSqlSection.tsx явно отключает проверки `react-hooks/exhaustive-deps` через `/* eslint-disable */` (строка 3). В итоге ни CI, ни build не сообщат о пропущенных зависимостях в useEffect.
- **Влияние:** Стабильно игнорируемые deps в useEffect создают staleclosure-баги. Например, в TextToSqlSection.tsx useEffect на строке 521-525 зависит от `loadRunStatus`, которая через closure захватывает `runId`, `dsn`, `workflowName` — при их обновлении интервал-функция работает со старыми значениями.
- **Исправление:** Убрать ignoreDuringBuilds. Убрать eslint-disable в TextToSqlSection.tsx, исправить зависимости useEffect.
- **Что даст:** Обнаружение staleclosure-багов на этапе сборки.

### M99. Утечка памяти: active_runs в ToolManager растёт неограниченно
- **Модуль:** Ядро: tool manager + MCP
- **Категория:** performance | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `tool_manager.py:576-591`
- **Проблема:** `cleanup_completed()` никогда не вызывается автоматически. При использовании в long-running процессе (FastAPI, Streamlit) `active_runs` накапливает все завершённые запросы бесконечно. `cleanup_completed` есть, но нет ни cron-задачи, ни вызова после завершения инструмента.
- **Влияние:** Постепенный рост потребления памяти в продакшне, потенциальный OOM.
- **Исправление:** Добавить автоматический вызов `cleanup_completed()` после каждого завершения инструмента (или по счётчику вызовов), либо использовать `collections.OrderedDict` с ограниченным размером (LRU).
- **Что даст:** Стабильный расход памяти при длительной работе.

### M100. Неограниченная загрузка удалённых изображений в память без проверки размера
- **Модуль:** Ядро: utils + logging + html
- **Категория:** performance | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `html_utils.py:344`
- **Проблема:** `response.content` (line 344) загружает всё содержимое ответа в память без проверки Content-Length или ограничения размера. Несмотря на `stream=True`, код затем делает `img_data = response.content` — что форсирует загрузку всего тела. Каждый markdown-документ может содержать много `<img>` тегов, и каждое изображение будет загружено полностью.
- **Влияние:** OOM при документе с множеством больших изображений (или при SSRF на endpoint, возвращающий бесконечный поток). В production может уронить сервис.
- **Исправление:** Проверять `Content-Length` заголовок; читать с лимитом (например, не более 10 МБ на изображение) через итерацию по `response.iter_content`. Добавить общий лимит на количество встраиваемых изображений.
- **Что даст:** Предотвращается OOM-падение при обработке вредоносных или аномально больших документов.

### M101. ThreadPoolExecutor создаётся на каждый вызов _parse_with_timeout в safety.py
- **Модуль:** Text2SQL: ядро + NLU
- **Категория:** performance | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `/srv/git_projects/MultiAgent/custom_tools/text_to_sql/validators/safety.py:152`
- **Проблема:** `_parse_with_timeout()` создаёт `concurrent.futures.ThreadPoolExecutor(max_workers=1)` на каждый вызов и вызывается из `_validate_with_sqlglot` и `is_valid_select_or_cte`. При высоком rps каждый запрос порождает новый поток OS. Существует и модульный `_SQLGLOT_PARSE_EXECUTOR` в `utils.py:303`, который используется в `parse_with_timeout()` (utils.py:352), но safety.py использует собственный per-call executor. Два разных механизма для одной задачи.
- **Влияние:** При рps > 50 — thread churn (создание/уничтожение потоков), накладные расходы на OS. При таймаутах воркер продолжает выполнять sqlglot.parse в фоне, неконтролируемо накапливая зомби-потоки.
- **Исправление:** Переиспользовать `parse_with_timeout()` из `utils.py` в `_validate_with_sqlglot` и `is_valid_select_or_cte` вместо per-call executor. Это уже сделано для `code_formatter` и `_classify_statement` в `_sql_generation_api.py` и `_db_exec.py` — привести safety.py к единому паттерну.
- **Что даст:** Снижение thread churn, единый пул и метрики parse_timeout для всех путей.

### M102. Unbounded in-memory кэш validation_results_cache в ContractRegistry
- **Модуль:** Workflow Engine: ядро
- **Категория:** performance | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `workflow/contracts/registry.py:20,152-154`
- **Проблема:** `validation_results_cache` — обычный `dict`, ключ формируется как `{contract_name}:{str(hash(artifact))}` (строки 153-154). Кэш никогда не очищается, не имеет TTL и лимита размера. При длительной работе в продакшне накопится неограниченное количество записей. Дополнительно: `hash(str(artifact))` нестабилен между запусками Python (PYTHONHASHSEED), поэтому кэш-хит после перезапуска процесса невозможен — кэш бесполезен как persistent cache, но растёт в памяти как in-process.
- **Влияние:** Утечка памяти при долгой работе процесса; возможный OOM при большом числе уникальных артефактов.
- **Исправление:** Использовать `functools.lru_cache` с ограничением, `cachetools.TTLCache`, или просто убрать кэш (ContractRegistry — синглтон, но кэш не даёт пользы при нестабильном hash).
- **Что даст:** Устранение утечки памяти; предсказуемое потребление ресурсов.

### M103. Жёсткий sleep(50) перед polling в Kling AI — заблокирует все потоки пакета
- **Модуль:** Storybook: видео/shots-генераторы
- **Категория:** performance | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/video_generator.py:417`
- **Проблема:** `time.sleep(50)` в самом начале `_wait_for_video_completion` выполняется в потоке ThreadPoolExecutor. Поскольку пакеты обрабатываются с `max_workers=len(batch_items)`, все потоки пакета заблокированы на 50 секунд. При batch_size=3 это просто 3 параллельных sleep(50), что нормально. Но если batch_size близок к системному лимиту тредов — это неэффективно. Главная проблема: max_wait_time=600 секунд, но sleep(50) уже съедает их — эффективный таймаут = 550 секунд, а не 600.
- **Влияние:** Фактический таймаут на 50 секунд меньше заявленного max_wait_time=600. Документация функции говорит про max_wait_time, реальное поведение другое.
- **Исправление:** Перенести sleep(50) внутрь цикла как первый шаг (после отправки запроса), убрать перед циклом. Либо использовать check_interval=50 для первой итерации.
- **Что даст:** Корректный таймаут, понятный поведенческий контракт.

### M104. time.sleep(2) + st.experimental_rerun() — устаревший API и блокировка UI
- **Модуль:** Streamlit UI
- **Категория:** performance | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `streamlit_app/pages/05_Text_to_SQL.py:1037`
- **Проблема:** В `show_agent_workflow_results()` при статусе задачи `running` выполняется `time.sleep(2)` затем `st.experimental_rerun()`. `st.experimental_rerun` устарел в Streamlit 1.27+ (заменён на `st.rerun()`). `time.sleep(2)` блокирует UI-поток на 2 секунды при каждом рендере.
- **Влияние:** UI лагает 2 секунды при каждом обновлении статуса; `st.experimental_rerun` может быть удалён в будущих версиях Streamlit.
- **Исправление:** Заменить на `st.rerun()` без `time.sleep`. Автообновление реализовать через `st.empty()` с таймером или через `time.sleep` только после финального рендера состояния.
- **Что даст:** Более отзывчивый UI, совместимость с актуальными версиями Streamlit.

### M105. N+1 загрузок trace-файлов на дашборде при каждом рендере
- **Модуль:** Streamlit UI
- **Категория:** performance | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `streamlit_app/app.py:419`
- **Проблема:** В `show_recent_activities()` для каждого run_id в fallback-ветке (строки 497–550) вызывается `tm.load_trace_file(run_id)` внутри цикла — по одному disk I/O на каждую трассу, без кэширования. Аналогично в 01_Home.py строки 224–362. При 10 активных трассах — 10 файловых чтений на каждый рендер дашборда.
- **Влияние:** Заметная задержка при открытии дашборда, особенно если трассы большие. Streamlit рендерит дашборд при каждом взаимодействии пользователя.
- **Исправление:** Кэшировать результаты через `@st.cache_data(ttl=5)` или хранить уже прочитанные трассы в `session_state` с timestamp инвалидации.
- **Что даст:** Снижение latency дашборда, уменьшение IO при активном использовании.

### M106. 08_Logs_Traces.py: динамический exec модуля 02_Workflows.py при каждом запуске страницы
- **Модуль:** Streamlit UI
- **Категория:** performance | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `streamlit_app/pages/08_Logs_Traces.py:73`
- **Проблема:** При загрузке страницы логов выполняется `spec.loader.exec_module(workflows_module)` — полное исполнение кода страницы 02_Workflows.py включая все импорты и инициализацию. Это происходит при каждом рендере страницы, так как нет кэширования.
- **Влияние:** Медленная загрузка страницы логов (повторная инициализация всех импортов workflow-страницы). Потенциальные side-effects от повторного исполнения модульного кода.
- **Исправление:** Вынести `show_workflow_artifacts` в отдельный shared-модуль (`streamlit_app/components/workflow_artifacts.py`) и импортировать напрямую.
- **Что даст:** Быстрая загрузка страницы логов, устранение хрупкой зависимости через importlib.

### M107. Утечка памяти: _trace_to_run растёт без ограничений
- **Модуль:** prompt_optimizer + telemetry
- **Категория:** performance | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `telemetry/smolagents_telemetry.py:221`
- **Проблема:** LocalJSONLExporter._trace_to_run — Dict[int, str], куда добавляются записи при каждом новом trace_id (строка 295). Записи никогда не удаляются. При долгосрочной работе процесса (сервер, долгие сессии) словарь будет накапливать записи за всё время жизни экспортера.
- **Влияние:** Постепенный рост потребления памяти. Для высоконагруженного сервиса с тысячами трасс в сутки — значимая утечка.
- **Исправление:** Использовать weakref или ограниченный LRU-кэш (например, collections.OrderedDict с ограничением по размеру, скажем 10 000 записей).
- **Что даст:** Стабильное потребление памяти при длительной работе.

### M108. ThreadPoolExecutor создаётся на каждый validate-вызов — утечка потоков при высоком RPS
- **Модуль:** Text2SQL: генерация SQL
- **Категория:** performance | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/validators/safety.py:152`
- **Проблема:** Функция `_parse_with_timeout` создаёт новый `ThreadPoolExecutor(max_workers=1)` при каждом вызове и завершает его через `shutdown(wait=False)`. При `wait=False` рабочий поток executor'а продолжает работать до завершения парсинга, а executor.shutdown не ждёт. При высоком RPS или при pathological SQL (зависший parse) накапливается большое число живых потоков, которые не прибиваются. Комментарий в коде признаёт проблему («cancel_futures отменяет только pending, но не running task»), но не ограничивает общее число параллельных потоков.
- **Влияние:** Memory и thread leak при нагрузке. На очень высоком RPS — OOM или thread exhaustion.
- **Исправление:** Использовать module-level пул с разумным `max_workers` (например, 4), защищённый семафором. При невозможности — добавить счётчик активных parse-потоков и rate-limit на входе.
- **Что даст:** Предотвращает накопление зависших потоков при нагрузке.

### M109. Глобальные in-process LLM-кэши без ограничения роста памяти
- **Модуль:** Storybook: инструменты генерации
- **Категория:** performance | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/shots_prompt_qa.py:58-61`
- **Проблема:** Четыре модульных словаря `_VIDEO_JUDGE_CACHE`, `_CLOSEUP_ROOM_CHECK_CACHE`, `_ENGLISH_SET_JUDGE_CACHE`, `_END_ENGLISH_JUDGE_CACHE` — неограниченные in-process кэши LLM-ответов. Ключи кэша включают полные тексты промптов. При обработке большого проекта (сотни шотов с длинными english_prompt) кэши могут потреблять значительный объём RAM. Кэши не очищаются между запусками инструмента и не имеют TTL или max-size.
- **Влияние:** В long-running процессах (агент обрабатывает несколько проектов подряд) возможно значительное потребление памяти и накопление устаревших данных.
- **Исправление:** Ограничить кэши через `functools.lru_cache` или ручной LRU с maxsize (например, 512 записей). Либо очищать кэши при старте каждого вызова shots_prompt_qa_tool.
- **Что даст:** Предсказуемое потребление памяти в long-running агентах.

### M110. Path traversal в edit_image_file: абсолютный output_path не ограничен базовым каталогом
- **Модуль:** Ядро: tool manager + MCP
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `mcp_image_edit.py:416-421`
- **Проблема:** Ветка `else` при `os.path.isabs(output_path)` передаёт путь как есть в `os.path.normpath(output_path)` без проверки, что он находится внутри IMG_SAVE_BASE_DIR. Аналогично для image_path (строка 384-386): если `image_path` абсолютный, он принимается без ограничений. Клиент MCP может передать `/etc/passwd` как `image_path` или `/root/.ssh/authorized_keys` как `output_path`.
- **Влияние:** Произвольное чтение файлов (через `image_path`) и запись в произвольные пути файловой системы (через `output_path`), включая перезапись системных файлов.
- **Исправление:** После нормализации пути выполнять проверку: `assert full_path.startswith(os.path.realpath(base_save_directory))`. Использовать `pathlib.Path.resolve()` и `Path.is_relative_to(base_dir)`. Применить ту же логику к mcp_img_gen.py:374-380 (параметр `directory`).
- **Что даст:** Устраняет возможность записи/чтения файлов за пределами разрешённого каталога.
- **Заметка верификатора:** Код подтверждён: mcp_image_edit.py:381-386 и 416-421 — при os.path.isabs() или при пустом IMG_SAVE_BASE_DIR (default '') путь идёт в os.path.normpath() как есть, без проверки вхождения в базовый каталог. Чтение (open full_image_path, стр.394) и запись (open full_output_path, стр.433) идут по неограниченному пути; makedirs создаёт каталоги. Однако edit_image_file (стр.232-294) — это инструмент локального stdio MCP-сервера, чьё документированное назначение именно 'путь к исходному файлу' / 'путь для сохранения'; вызывающий — сам агент/хост, а не недоверенный удалённый клиент. IMG_SAVE_BASE_DIR — опциональный префикс, а не заявленная песочница, которую обходят. Реальная нехватка confinement есть, но модель угроз (недоверенный вызывающий) не обоснована, поэтому high завышен — понижаю до medium.

### M111. Fallback prompt_injection_detector при ошибке LLM-Guard молча снижает порог защиты
- **Модуль:** Ядро: tool manager + MCP
- **Категория:** security | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/llm_guard_tools.py:82-87`
- **Проблема:** При любом исключении в сканере `_prompt_injection_scanner.scan()` код делает fallback к проверке по ключевым словам (строки 85-87) и возвращает `{"is_injection": is_injection}` без поля `risk_score` и без логирования severity исключения (только `logger.error`). Вызывающий код в `comprehensive_security_check` не знает, что защита деградировала до keyword-matching. Список ключевых слов не включает английских вариантов инъекций в fallback при ошибке (в отличие от fallback при `not LLM_GUARD_AVAILABLE`).
- **Влияние:** При сбое LLM-Guard защита от prompt injection резко ухудшается без уведомления вышестоящего кода.
- **Исправление:** В случае ошибки сканера логировать предупреждение с типом исключения, включить полный список ключевых слов (ru+en) в fallback, добавить поле `degraded: True` в возвращаемый словарь чтобы вызывающий код мог принять решение.
- **Что даст:** Прозрачность деградации защиты; возможность блокировки запроса при недоступности сканера.

### M112. XSS в сгенерированном HTML: неэкранированный вывод кода и результатов
- **Модуль:** Ядро: LLM-модель + codeinterpreter
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `codeinterpreter.py:519-520`
- **Проблема:** В `advanced_visualization()` строка `result_str` вставляется напрямую в HTML: `f'        <pre>{result_str}</pre>'`. `result_str` содержит сгенерированный LLM-кодом вывод, включая `__captured_print__` из `exec()`. Если атакующий управляет кодом/входными данными, он может внедрить `<script>alert(1)</script>` в вывод, который будет сохранён в HTML-файл и открыт пользователем через `webbrowser.open()` (см. fa.py:508).
- **Влияние:** Stored XSS при открытии результирующего HTML-файла в браузере; кража сессионных cookies, редирект, выполнение JS.
- **Исправление:** Экранировать result_str через `html.escape(result_str)` перед вставкой в HTML.
- **Что даст:** Предотвращение XSS при открытии результатов в браузере.
- **Заметка верификатора:** Инъекция реальна: advanced_visualization (515-520) вставляет result_str в `<pre>{result_str}</pre>` без html.escape; result_str = report из generate_report, содержащий __captured_print__ из exec(). Но цитирование fa.py:508 неверно: codeinterpreter.advanced_visualization НЕ вызывает webbrowser и возвращает None; fa.py:508 открывает HTML другого модуля (system.advanced_visualization -> html_visualizer с параметром show, agent_system.py:323/362), а не этого. В коде CodeInterpreterPlugin HTML отдаётся как direct_result file (174-192), типично в tgBot. Контекст — статический локальный/доставляемый файл (file://), без сессионных cookies; вдобавок при наличии RCE через ex() XSS вторичен. Реальная инъекция есть, но импакт и привязка к sink завышены — medium.

### M113. Жёстко захардкоженный api_base в codeinterpreter.py
- **Модуль:** Ядро: LLM-модель + codeinterpreter
- **Категория:** security | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `codeinterpreter.py:87`
- **Проблема:** `openai.api_base = 'https://api.vsegpt.ru/v1'` — hardcoded сторонний API endpoint, который переопределяет глобальный атрибут модуля `openai`. Во-первых, изменение глобального состояния модуля влияет на все другие компоненты в процессе, использующие openai. Во-вторых, трафик идёт через третью сторону (vsegpt.ru), что создаёт риск утечки промптов и данных.
- **Влияние:** Все LLM-запросы из codeinterpreter проксируются через внешний сервис; мутация глобального состояния может сломать другие компоненты, использующие openai SDK.
- **Исправление:** Передавать base_url через конструктор `AsyncOpenAI(base_url=...)` вместо мутации глобала; вынести URL в env-переменную.
- **Что даст:** Изоляция конфигурации, отсутствие побочных эффектов для других модулей.

### M114. file_list: слабая проверка разрешённых директорий (substring match)
- **Модуль:** custom_tools (прочие инструменты)
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `custom_tools/file_system_tools.py:121`
- **Проблема:** `any(allowed_dir in dir_name for allowed_dir in allowed_dirs)` проверяет вхождение строки `'plots'` как подстроки в `dir_name`. Путь `../../exploits_plots` пройдёт проверку (`'plots' in '../../exploits_plots'` == True). После этого `os.listdir(dir_name)` вызывается без нормализации.
- **Влияние:** Listing произвольных директорий, имена которых содержат 'plots' как подстроку.
- **Исправление:** Сравнивать `os.path.realpath(dir_name)` с `os.path.realpath('plots')` или его поддиректориями через `startswith`.
- **Что даст:** Устранение обхода через substring match.
- **Заметка верификатора:** Логика подтверждена: any(allowed_dir in dir_name ...) — substring-match. Эмпирически '../../exploits_plots' и '../../../etc/plots' проходят проверку, os.listdir вызывается без нормализации; dir_name — LLM-контролируемый параметр (file_list.yaml required:true). Однако дальше files фильтруются по session_id in file, и возвращаются только ИМЕНА файлов, не содержимое. Раскрытие имён в директориях, чей путь содержит подстроку 'plots' (либо .../plots/ как сегмент) — реальный, но ограниченный read-only-leak. Понижаю до medium: нет произвольного листинга /etc (substring 'plots' нужен где-то в пути) и нет утечки содержимого.

### M115. install_package: неполный deny-list DANGEROUS_PACKAGES
- **Модуль:** custom_tools (прочие инструменты)
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `custom_tools/file_system_tools.py:134`
- **Проблема:** Стратегия «запрещать известно-плохие» по своей природе хуже, чем allowlist. Из DANGEROUS_PACKAGES отсутствуют: `pwntools`, `impacket`, `pymetasploit`, `dill` (произвольная десериализация), `cloudpickle`, `rpdb`, `pyxdg`, `docker` (Python SDK), `kubernetes`. Нормализация `replace('_', '-')` не покрывает `PwNtOoLs` (lower() применяется, это ок) но не покрывает Unicode-омоглифы или пробелы в имени (`requests ` с trailing space через `strip()` ок).
- **Влияние:** Установка опасных пакетов, пропущенных в deny-list.
- **Исправление:** Инвертировать логику на allowlist: разрешать только пакеты из `SAFE_PACKAGES`; всё остальное — блокировать с требованием ручного одобрения.
- **Что даст:** Надёжная protection-by-default вместо fragile deny-list.
- **Заметка верификатора:** Слабость реальна: подход deny-list, а не allow-list (SAFE_PACKAGES существует, но проверка по нему закомментирована — строки 213-214, safety_status всегда '✅ БЕЗОПАСНЫЙ'). Перечисленные пропуски (pwntools, impacket, dill, cloudpickle, docker, kubernetes и т.п.) действительно отсутствуют в DANGEROUS_PACKAGES. install_package зарегистрирован как агентский тул. Но: эксплуатация — это установка pip-пакета (требует, чтобы пакет реально был зловредным в установке/импорте; arbitrary RCE не мгновенный), а сами 'опасные' пакеты — это инструменты, а не payload. Реальный риск supply-chain/инструментарий — medium, не high. Понижаю до medium.

### M116. PII_MASKING_ENABLED=0 в production .env — PII-маскирование отключено глобально
- **Модуль:** Text2SQL: ядро + NLU
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. critical→medium
- **Где:** `/srv/git_projects/MultiAgent/.env:25`
- **Проблема:** Переменная `PII_MASKING_ENABLED=0` закоммичена в .env. В `_pii.py:322` при этом значении `pii_masking()` немедленно возвращает `masked_data=data` без какой-либо маскировки. Все результаты запросов (потенциально содержащие персональные данные) отдаются клиенту в открытом виде.
- **Влияние:** Полное отключение PII-защиты в production. Результаты SQL-запросов с ФИО, ИНН, СНИЛС, email, телефонами, картами — отдаются без маскирования. Нарушение 152-ФЗ и GDPR.
- **Исправление:** Убрать PII_MASKING_ENABLED=0 из .env. Убедиться, что PII_MASK_SALT выставлен в production. Добавить мониторинг на значение переменной при старте приложения.
- **Что даст:** Восстановление PII-защиты данных пользователей.
- **Заметка верификатора:** Код в _pii.py:322 действительно коротко закорачивает pii_masking() при PII_MASKING_ENABLED=0 — это верно. НО impact завышен: (1) gated-функция pii_masking() вызывается ровно в ОДНОМ месте — schema_enricher.py:333, и только для маскировки schema sample data при интроспекции, а НЕ для выдачи результатов запроса клиенту; (2) audit/RAG-логирование использует pii_mask_sync/_sanitize_audit_obj (core/_audit.py:63,390), которые НЕ проверяют PII_MASKING_ENABLED и работают всегда (regex по config/pii/categories.yaml: email/inn/snils/card/phone). Утверждение «все результаты запросов отдаются клиенту в открытом виде» кодом не подтверждается: колоночное маскирование результатов вообще не подключено к delivery-пути (что и зафиксировал прошлый аудит: «колоночные PII не маскируются нигде»). Реально kill-switch отключает только один слой (маскирование schema-sample), audit/RAG-санитизация продолжает работать. Это намеренная env-настройка, ослабляющая один слой защиты — medium, не critical. Формулировка про тотальное нарушение 152-ФЗ/GDPR не обоснована.

### M117. save_successful_sql сохраняет execution_result с PII из БД в RAG-датасет через строку, не через dict
- **Модуль:** Text2SQL: ядро + NLU
- **Категория:** security | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `/srv/git_projects/MultiAgent/custom_tools/text_to_sql/core/_audit.py:379`
- **Проблема:** `execution_result` принимается как строка (`str`) и парсится через `json.loads`. Если caller передаёт уже распарсенный dict (что допускает isinstance-проверка), маскировка применяется к нему (строка 390). Но комментарий на строке 386 явно признаёт: «Колоночные PII без regex-паттерна (адрес, зарплата, произвольные ФИО) не маскируются». sqlrag/*.md файлы впоследствии включаются в LLM-промпты. Строка `_sanitize_audit_text(sql_clean)` маскирует regex-паттерны, но rows с фактическими данными (`result_data`) маскируются через `_sanitize_audit_obj`, который сам вызывает только regex-санитайзер (не LLM).
- **Влияние:** ФИО, адреса, суммы выплат, territory_id-связанные данные из муниципального датасета попадают в sqlrag-файлы и затем в LLM-контекст. Утечка PII удваивается: диск + LLM-контекст. Особенно критично при PII_MASKING_ENABLED=0 (текущее состояние .env).
- **Исправление:** Явно документировать, что caller обязан применить `pii_masking()` ДО вызова `save_successful_sql`. Добавить assertion или warning при `PII_MASKING_ENABLED=0`. Рассмотреть возможность не сохранять `execution_result` в sqlrag-файл вообще (только sql_query + user_query).
- **Что даст:** Снижение риска утечки PII в RAG-обучающий датасет.

### M118. Утечка DSN в cache_info: поле dsn возвращается клиентам и логируется
- **Модуль:** Text2SQL: schema-linking + RAG
- **Категория:** security | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/rag/retrieval.py:352`
- **Проблема:** `_prepare_cache_info` возвращает dict с полем `"dsn": dsn` (строка DB_DSN полностью, без маскировки). Этот dict передаётся в `_save_to_cache`, откуда через `save_memory` попадает в agent_memory. При наличии в DSN пароля (postgres://user:pass@host/db) он сохраняется в базе памяти в открытом виде. Кроме того, `cache_info` может залогироваться debug-методами.
- **Влияние:** Учётные данные БД попадают в тактическую память, потенциально доступную через API get_memory. Нарушение PII/секреты в данных.
- **Исправление:** Убрать поле `dsn` из возвращаемого dict или заменить на sanitized-версию: `dsn_to_sanitized_name(dsn)` (функция уже импортируется в том же методе).
- **Что даст:** Пароль БД не хранится в тактической памяти.

### M119. Защита production legacy-mode зависит от `ENV`/`APP_ENV` — обход при нестандартном naming
- **Модуль:** Text2SQL: валидаторы безопасности
- **Категория:** security | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/validators/safety.py:1194-1197`
- **Проблема:** Блокировка `USE_SQLGLOT=0` в production срабатывает только при `os.getenv("ENV") == "production"` или `os.getenv("APP_ENV") == "production"`. Если деплоймент использует другие имена (`ENVIRONMENT=prod`, `APP_ENVIRONMENT=production`, `DJANGO_ENV=production` и т.п.) — проверка молча пропускается, и legacy-режим с regex-only валидацией (без AST-проверки функций, без защиты от nested parens в IN) работает без предупреждений.
- **Влияние:** В окружениях с нестандартными env-именами legacy-режим активен в production без ведома операторов. Отсутствие WARNING в логе скрывает проблему.
- **Исправление:** Дополнить fallback: если `ENV`/`APP_ENV` не распознаны, логировать WARNING что среда выполнения неизвестна и legacy-режим активен. Добавить документированную поддержку `ENVIRONMENT` и `APP_ENVIRONMENT`. Или проще: всегда логировать WARNING при `USE_SQLGLOT=0`, независимо от среды.
- **Что даст:** Операторы видят в логах, что unsafe legacy-режим активен, независимо от того, как называется env-переменная среды.

### M120. `contains_comments` в sqlglot-пути логирует оригинальный SQL без редактирования в warning
- **Модуль:** Text2SQL: валидаторы безопасности
- **Категория:** security | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/validators/safety.py:559-563`
- **Проблема:** При `_SqlglotTokenError` в `contains_comments` логируется `source[:200]` напрямую: `logger.warning("... sql=%r", e, source[:200])`. В отличие от `mask_identifiers_via_lex` (строка 365), где SQL перед логированием прогоняется через `_redact_safety_value`, здесь редактирования нет. `source` — это либо `original_query` (оригинальный SQL от пользователя), либо `masked_query` (маска строковых литералов). Оригинальный SQL может содержать PII (имена, ИНН, адреса в условиях WHERE), которые уйдут в лог.
- **Влияние:** PII-утечка в логи при попытке tokenize невалидного SQL. Особенно актуально для системы с муниципальным РФ-датасетом, где WHERE-условия содержат имена физических лиц и ОКТМО-коды.
- **Исправление:** Обернуть `source[:200]` в `_redact_safety_value(source[:200])` аналогично строке 365.
- **Что даст:** Исключает попадание пользовательских данных (PII) в логи при ошибках токенайзера.

### M121. SecurityValidator во validators.py — false positive на легитимный SQL в text-to-sql системе
- **Модуль:** Workflow Engine: ядро
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `workflow/contracts/validators.py:212-228`
- **Проблема:** `SecurityValidator` (строки 212-228) флажкует паттерны `delete\s+from`, `insert\s+into`, `union\s+select`, `update.*set` как угрозы SQL-инъекции. В системе text-to-sql эти паттерны являются **ожидаемым выводом** агентов. Тест содержимого проводится по `str(artifact).lower()` — любой правомерный SELECT/INSERT-запрос от sql_generator_agent снизит score на 0.3 и пометит validation как failed. При score ниже порога Decision Engine может перезапустить шаг или остановить workflow.
- **Влияние:** Легитимные SQL-запросы (включая SELECT с UNION для муниципального датасета) могут быть ложно блокированы. Система text-to-sql деградирует: агент получает FAILED из-за собственного корректного вывода.
- **Исправление:** Для sql_query контракта отключить sql_injection_check или заменить его контекстно-зависимой проверкой (например, проверять только параметры запроса, а не сам сгенерированный SQL). Разделить "SQL как output" и "SQL в пользовательском вводе".
- **Что даст:** Устранение false positive; корректная работа security-проверки в контексте text-to-sql пайплайна.
- **Заметка верификатора:** Код валидатора действительно баговый: validators.py:212-227 флажит union select / delete from / insert into / update...set по str(artifact).lower(), passed=len(threats)==0 (273); в registry.py validation_passed=False при любом непройденном валидаторе (114-115), вес security 0.3, контракт sql_query включает 'security' (schemas.py:66), при security_violation Decision Engine даёт STOP (decision.py:167-168). НО в дефолтном проде это недостижимо: pre_step_planner включён по умолчанию (enhanced_global.yaml:17, enhanced_engine.py:61), планировщик кладёт в plan.quality_criteria['required_validators'] значение из quality gate = ['structural','completeness'] (planner.py:236, default.yaml:11), а judge._get_enabled_validators отфильтровывает security (judge.py:115-117). text_to_sql_pipeline.yaml не переопределяет required_validators и не включает security-гейтинг. Валидатор сработал бы лишь при не-дефолтной конфигурации (pre_step_planner выключен при включённом post_step_judge, либо required_validators пуст/содержит security). Латентный баг, не активный прод-дефект -> high занижаю до medium.

### M122. Утечка hostname и версии OS в незащищённых service actions
- **Модуль:** Backend FastAPI / AG-UI
- **Категория:** security | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `backend/fastapi_app/agui/service.py:893`
- **Проблема:** _db_plugin_diagnostics и _system_diagnostics возвращают platform.node() (hostname) и platform.platform() (полную строку ОС с версией ядра) в поле system_info. Эти данные уходят через service actions db.diagnostics и system.diagnostics без авторизации — любой, кто может слать POST /agent с forwarded_props.service_action, получит их.
- **Влияние:** Утечка инфраструктурных данных. Hostname и версия ядра упрощают таргетированные атаки.
- **Исправление:** Убрать из ответа hostname и processor; оставить только platform, architecture, python_version. Либо добавить авторизацию на admin-level actions.
- **Что даст:** Уменьшает attack surface fingerprinting.

### M123. Path traversal в files.list через паттерн glob из payload
- **Модуль:** Backend FastAPI / AG-UI
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `backend/fastapi_app/agui/service.py:3987`
- **Проблема:** files.list принимает pattern из payload без ограничений: payload.get('pattern') or '*'. Результаты glob-а затем прогоняются через _ensure_within_root, но сам вызов base_path.glob(pattern) может принять паттерн вида '../../*' или '../logs/*', что при resolve() вернёт пути вне ожидаемого base_dir. Кроме того, base_dir тоже берётся из payload без ограничений, и _ensure_within_root применяется уже к результатам, а не к паттерну. Клиент получает перечень файлов всего проекта, включая логи, yaml-конфиги, secrets-файлы.
- **Влияние:** Перечисление файловой системы проекта: позволяет обнаружить db_test_config_secrets.json, .env, ключи, дампы SQLite.
- **Исправление:** Ограничить pattern до простого glob (без '/' и '..') через whitelist символов. base_dir должен проходить явную валидацию, не зависящую только от _ensure_within_root.
- **Что даст:** Предотвращает enumeration структуры файловой системы через произвольный glob.
- **Заметка верификатора:** Частично опровергнуто по механизму. Заявленный обход через pattern='../../*' / base_dir='../logs' НЕ работает: каждый результат glob прогоняется через _ensure_within_root(p) (service.py:3990), который через os.path.commonpath отклоняет любой путь вне корня (проверил: '../../../etc/passwd' BLOCKED; glob('../*') вернул бы пути вне корня -> _ensure_within_root бросит ValueError и оборвёт весь листинг). То есть выйти ЗА пределы project_root нельзя. Однако реальная проблема остаётся: pattern='**/*' внутри корня без авторизации перечисляет всё дерево проекта, включая .env, *.secrets.json, logs/. Это раскрытие имён файлов/структуры, но НЕ traversal и НЕ чтение содержимого. Понижаю до medium: реальный риск — разведка перед эксплуатацией files.read, а не сам по себе доступ к данным.

### M124. MySQL: SET SESSION TRANSACTION READ ONLY не защищает от DDL
- **Модуль:** DB Plugins
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `db_plugins/mysql.py:46`
- **Проблема:** MySQL `SET SESSION TRANSACTION READ ONLY` применяется только к DML-транзакциям (INSERT/UPDATE/DELETE) — DDL-операции (DROP, ALTER, CREATE) не блокируются этой настройкой в MySQL 8.x. Комментарий в коде не предупреждает об этом ограничении. Для Impala и SAP IQ это задокументировано, для MySQL — нет.
- **Влияние:** Если LLM сгенерирует DDL-запрос и он пройдёт через deny-list (который, согласно аудит-заметкам проекта, мёртв при USE_SQLGLOT=1), `execute_select` его выполнит несмотря на 'read-only' сессию.
- **Исправление:** Добавить комментарий-предупреждение (как в impala.py/sapiq.py). Рекомендовать использование MySQL-пользователя только с SELECT-правами на уровне GRANT.
- **Что даст:** Честная документация ограничений read-only enforcement, предотвращающая ложное чувство безопасности.
- **Заметка верификатора:** Фактически верно: mysql.py:46 выполняет 'SET SESSION TRANSACTION READ ONLY', что в MySQL блокирует только DML в транзакциях, но не DDL (DROP/ALTER/CREATE — implicit commit, не блокируется этой настройкой). Комментарий действительно не предупреждает об этом, в отличие от impala.py/sapiq.py, где невозможность read-only явно задокументирована и требует opt-in read_only_fail_open. Это реальный пробел defense-in-depth и расхождение в документировании. Но: для эксплуатации DDL должен пройти upstream-верификацию SQL, а connect для MySQL хотя бы частично выставляет read-only (в отличие от Impala/SAP IQ, где сессия полностью writable). Реальный, но не high — это вторичный барьер; medium.

### M125. Полный обход политики доступа при scope_read=all: любой агент может передать requesting_agent='memory_archivist'
- **Модуль:** Memory / RAG
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `memory/tools.py:93, memory/tools.py:484`
- **Проблема:** `requesting_agent` — строковый параметр без аутентификации. Любой агент, знающий строку `"memory_archivist"`, может обойти проверку на строке 484 (`if requesting_agent != "memory_archivist": return []`) и получить доступ ко всей памяти всех сессий без фильтров. Аналогично в `_apply_policy_filters` (строка 93) — `memory_archivist` полностью исключён из фильтрации артефактов и межагентной видимости.
- **Влияние:** Агент с scope_read=agent, передав `requesting_agent='memory_archivist'` и `session_id=None`, прочитает всю память всех сессий системы — полный обход политики изоляции.
- **Исправление:** Проверять, что запрашивающий агент действительно имеет роль `memory_archivist` через верифицированный механизм (например, проверка поля в `AGENT_PROFILES`), а не через доверие строковому параметру.
- **Что даст:** Устранение обхода политики доступа к памяти.
- **Заметка верификатора:** Проверено: requesting_agent — неаутентифицированная строка, и стр.484/93 действительно дают memory_archivist полный обход фильтров. НО реальная модель угроз: requesting_agent заполняется фреймворком как self.agent_name (rag_memory.py:394/670/883/899/1005), а не пользовательским вводом; все агенты работают в одном доверенном процессе. Эксплуатация требует УЖЕ скомпрометированного/злонамеренного агента внутри trust boundary, который напрямую вызывает get_memory с произвольной строкой. Это валидная defense-in-depth заметка (отсутствие аутентификации привилегированной роли), но 'high' предполагает внешний недоверенный вызов, которого тут нет. Понижаю до medium.

### M126. Path traversal через project_id без валидации
- **Модуль:** Storybook: инструменты генерации
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `custom_tools/storybook/project_init.py:36`
- **Проблема:** Все инструменты конструируют пути через f-строки вида `f'plots/storybooks/{project_id}/...'` без какой-либо валидации project_id. Если project_id содержит `../`, атакующий или плохой LLM-output может выйти за пределы дерева проекта. Пример: project_id='../../etc/passwd_inject' → путь `plots/storybooks/../../etc/passwd_inject/00_brief.json`. Аналогично в bible_builder.py:24, style_keeper.py:22, story_planner.py:35, prompt_engineer.py:1019, items_builder.py:18 и во всех других файлах.
- **Влияние:** Запись/чтение произвольных файлов файловой системы, если project_id поступает из внешнего источника (LLM, пользовательский ввод в агент).
- **Исправление:** Добавить re.match(r'^[a-zA-Z0-9_\-]+$', project_id) или os.path.abspath + проверку что путь начинается с ожидаемого prefix в точке входа каждого tool-функции. Минимально: единый хелпер `_validate_project_id(project_id)`.
- **Что даст:** Предотвращает запись в произвольные места ФС при использовании в агентском контексте.
- **Заметка верификатора:** Паттерн подтверждён: project_id интерполируется в f-string пути без валидации во всех инструментах (project_init.py:36, bible_builder.py:24, story_planner.py:35 и др.). Однако в основном GUI/pipeline-потоке project_id формируется через project_manager.generate_project_id (project_manager.py:205-218), который санирует имя regexp `re.sub(r'[^a-zA-Z0-9]+', '_', ...)` — все `../` и разделители вырезаются. run_full_pipeline получает уже очищенный id. Прямой обход возможен только если LLM-агент вызовет инструмент с сырым project_id (инструменты экспортированы через tool_definitions/*.yaml), что требует, чтобы агент целенаправленно передал вредоносный id; запись ограничена JSON в каталог, доступный процессу. Это реальный hardening-пробел (отсутствие валидации на границе инструмента), но не чисто эксплуатируемый в проде путь при штатном потоке — понижаю до medium.

### M127. load_env_file перезаписывает уже установленные переменные окружения
- **Модуль:** Storybook: видео/shots-генераторы
- **Категория:** security | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/video_generator.py:22 / video_generator_mm_tool.py:23 / video_generator_aitunnel_tool.py:62 / video_generator_veo_tool.py:47`
- **Проблема:** Все четыре реализации `load_env_file` используют `os.environ[key] = value` без проверки `if key not in os.environ`. Это означает, что значения из .env-файла безоговорочно перезаписывают переменные, которые уже были установлены в окружении (например, через Docker, systemd, CI/CD, kubernetes secrets). В `video_generator_veo_tool.py` функция вызывается дважды: при импорте (строка 51) и повторно внутри `video_generator_veo_tool` (строка 140) — второй вызов абсолютно избыточен.
- **Влияние:** В production окружение может иметь secrets, инжектированные оркестратором; .env-файл (потенциально менее защищённый) перезапишет их. Повторный вызов в veo_tool — waste.
- **Исправление:** Заменить `os.environ[key] = value` на `os.environ.setdefault(key, value)` — стандартный паттерн dotenv. Удалить повторный вызов load_env_file() внутри video_generator_veo_tool (строка 140).
- **Что даст:** Secrets, заданные через оркестратор, не перезаписываются файлом; единственный вызов при импорте.

### M128. Незащищённое хранение DSN в session_state (plaintext) в 06_DB_Plugins.py
- **Модуль:** Streamlit UI
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. high→medium
- **Где:** `streamlit_app/pages/06_DB_Plugins.py:499`
- **Проблема:** В `show_saved_test_configurations()` DSN сохраняется как `st.session_state.saved_test_configs[config_name] = {"dsn": config_dsn, ...}` в открытом виде. В line 515 он отображается в UI через `st.markdown(f"**DSN:** \`{config_data['dsn']}\`")` — plaintext пароль виден в интерфейсе без маскирования.
- **Влияние:** Пароль БД виден на экране в открытом тексте. При shared-доступе к Streamlit-сессии или скриншоте — credentials утекают.
- **Исправление:** Маскировать пароль при отображении (аналогично `_redact_dsn` в 05_Text_to_SQL.py). Хранить в session_state только redacted-версию, а сырой DSN — в `get_connection_registry()` с opaque ID.
- **Что даст:** Credentials не отображаются на экране в открытом виде.
- **Заметка верификатора:** Код живой: show_saved_test_configurations() вызывается на строке 292 в потоке страницы. DSN сохраняется в plaintext в session_state.saved_test_configs (строка 499) и отображается без маскирования через st.markdown (строка 515) и st.info (строка 524). Реальная проблема — пароль БД виден на экране (скриншоты, демонстрация экрана). Но: st.session_state в Streamlit изолирован per-session, поэтому межпользовательской утечки (как заявлено) нет; пользователь сам вводит этот DSN. Это display-credentials issue, а не cross-user leak. Понижаю до medium.

### M129. agent_constructor: generate_agent_profile записывает файлы в произвольный output_dir
- **Модуль:** custom_tools (прочие инструменты)
- **Категория:** security | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/agent_constructor.py:314`
- **Проблема:** `generate_agent_profile(spec, tools_resolved, plan, output_dir='agent_profiles')` принимает `output_dir` как параметр без ограничений. Вызов с `output_dir='/etc/cron.d'` или `output_dir='../../'` создаст директорию и запишет YAML-файл в произвольное место. `construct_agent` (строка 486) передаёт `output_dir` с дефолтом, но API открыт.
- **Влияние:** Запись YAML-файлов в произвольные пути файловой системы, включая системные директории.
- **Исправление:** Нормализовать `output_dir` через `os.path.realpath`, ограничить допустимым prefix (например, `agent_profiles/` внутри рабочей директории).
- **Что даст:** Устранение возможности записи файлов за пределами рабочей директории.

### M130. _workflow_process_entry: параметры DSN передаются в дочерний процесс через args
- **Модуль:** Workflow Engine: resilience/orchestration/intelligence
- **Категория:** security | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `workflow/streamlit_api.py:846`
- **Проблема:** При запуске дочернего процесса (строка 846) `parameters` передаётся как аргумент `Process(target=..., args=(..., parameters, ...))`. Словарь `parameters` может содержать DSN с паролем (`dsn=postgresql://user:password@host/db`). В multiprocessing с `spawn` (macOS, Windows) аргументы сериализуются через pickle и могут появиться в traceback, core dump или `/proc/PID/cmdline`. Хотя DSN редактируется для логов через `_redact_public_payload`, в `active_runs` записывается `_redact_public_payload(parameters)` (строка 857), но в сам процесс данные идут нередактированными.
- **Влияние:** Утечка credentials (DB password) через межпроцессную передачу аргументов, видимую в системных инструментах мониторинга или crash dumps.
- **Исправление:** Передавать DSN через временный файл или наследованный file descriptor, либо через env-переменные (уже реализовано в `_workflow_dsn_env`). В `_workflow_process_entry` DSN можно считывать из env, не передавая его в args.
- **Что даст:** Устранение вектора утечки credentials в системные логи/dumps.

### M131. _ensure_within_root: os.path.commonpath подвержен prefix-атаке на Windows
- **Модуль:** Backend FastAPI / AG-UI
- **Категория:** security | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `backend/fastapi_app/agui/service.py:135`
- **Проблема:** Проверка os.path.commonpath([str(root), str(resolved)]) != str(root) некорректна при совпадающих префиксах имён директорий на Windows (например, root=/srv/project, resolved=/srv/project_other/secret): commonpath вернёт /srv, что != root. На Linux это безопасно, но если сервис когда-либо запускается на Windows или за WSL, уязвимость реализуется. Правильная проверка — resolved.is_relative_to(root) (Python 3.9+) или str(resolved).startswith(str(root) + os.sep).
- **Влияние:** На Windows/WSL: путевой обход с чтением файлов вне project root.
- **Исправление:** Заменить commonpath на resolved.is_relative_to(root) (Python ≥3.9) или явное сравнение с добавлением os.sep.
- **Что даст:** Устойчивость к prefix-атаке без зависимости от платформы.

### M132. generate_safe_sql: where_clause инъецируется напрямую в SQL без параметризации
- **Модуль:** DB Plugins
- **Категория:** security | **Уверенность:** medium | **Верификация:** скоррект. high→medium
- **Где:** `db_plugins/streamlit_api.py:554-556`
- **Проблема:** `generate_safe_sql()` принимает строку `where_clause` от вызывающего кода и, пройдя через `_validate_where_clause()`, вставляет её в SQL: `sql += f" WHERE {where_clause}"`. Валидация (_validate_where_clause, строки 570-577) маскирует строки и проверяет список запрещённых слов — но это не параметризация. Regex-маскировка строк может быть обойдена вложенными кавычками или unicode-эскейпами. Кроме того, список forbidden не включает EXEC, CALL, LOAD, COPY, которые поддерживаются DuckDB/Impala.
- **Влияние:** where_clause, сформированный из пользовательского ввода, может содержать SQL-фрагменты, обходящие deny-list, и вставляться в запросы к БД без read-only enforcement (Impala, SAP IQ).
- **Исправление:** Отказаться от allow-list валидации строки WHERE как защитного механизма. Использовать параметризованные запросы с биндингом значений вместо конкатенации строк, либо ограничить where_clause до структурированного API (column, operator, value).
- **Что даст:** Устраняет потенциально обходимую строковую фильтрацию SQL.
- **Заметка верификатора:** Подтверждается, что where_clause конкатенируется (streamlit_api.py:556) и проходит лишь deny-list _validate_where_clause (570-577): маскировка строк regex'ом + проверка на ';', '--', '/*', '*/' и список forbidden, в котором действительно НЕТ EXEC/CALL/LOAD/COPY/ATTACH-вне-списка (хотя ATTACH/DETACH/PRAGMA там есть). Это реальный пробел deny-list для DuckDB/Impala. НО серьёзность снижают факты: (1) generate_safe_sql ТОЛЬКО возвращает строку SQL (service.py:3066), сама её не исполняет — исполнение идёт отдельным путём с read-only (для большинства диалектов) либо вовсе отклонённым соединением (Impala/SAP IQ без fail_open); (2) ';' и комментарии заблокированы, поэтому в контексте WHERE нельзя начать новый стейтмент, а EXEC/CALL не являются валидными выражениями внутри WHERE. Реальный, но это defense-in-depth-пробел с ограниченным практическим импактом — medium, не high.

### M133. ast.literal_eval на данных из LLM — небезопасно при неожиданном вводе
- **Модуль:** Storybook: инструменты генерации
- **Категория:** security | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/artist_batch_edit.py:816`
- **Проблема:** Строка `reference_paths = ast.literal_eval(reference_paths)` вызывается на строке, проверяемой как начинающейся с `[` и заканчивающейся на `]`. `ast.literal_eval` безопасен для простых литералов Python, но обрабатывает сложные выражения типа `[1, 2, 3*10**100000]` (bomb-like expressions) и может поднимать исключения на вложенных данных. Хотя это лучше `eval()`, при crash выбрасывается `ValueError`/`SyntaxError`, которые поймает except и выполнит fallback `reference_paths = [reference_paths]` — потенциально теряя реальный список.
- **Влияние:** При необычном LLM-выводе reference_paths превращается в список из одной строки-исходника, что приведёт к FileNotFoundError на строке 865 и падению генерации.
- **Исправление:** Заменить на `json.loads(reference_paths)` (JSON-парсинг более предсказуем) с явным логированием при ошибке.
- **Что даст:** Более предсказуемое поведение при нестандартном LLM-выводе.

### M134. Промпт формируется с f-строками содержащими пользовательские данные (potential prompt injection)
- **Модуль:** Storybook: инструменты генерации
- **Категория:** security | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/artist_batch_edit.py:958-976`
- **Проблема:** Инструкция для агента формируется f-строкой, куда напрямую подставляются: `english_prompt` (результат LLM), `image_paths_str` (пути к файлам), `session_id`, `output_path`, `scene_negative`. Ни один из этих параметров не экранируется. Если `english_prompt` содержит `", ...}` или последовательность закрытия кавычки, это нарушит строку инструкции для следующего агента. Аналогично в `protagonist_initializer.py:212-225` и `_generate_base_image` (строки 1916-1937).
- **Влияние:** Потенциальный prompt injection: LLM-сгенерированный english_prompt может изменить инструкцию для artist_agent и заставить его выполнить произвольные действия (например, вызвать другой инструмент с другими параметрами).
- **Исправление:** Передавать параметры агенту структурированно (через tool_input dict), а не через f-строку инструкции. Если f-строка необходима, экранировать специальные символы.
- **Что даст:** Устраняет вектор prompt injection через LLM-генерированные данные.

### M135. Небезопасный JSON.parse пользовательского ввода builderSteps без валидации схемы
- **Модуль:** Frontend (Next.js / React AG-UI)
- **Категория:** security | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `frontend/client/src/app/page.tsx:1120`
- **Проблема:** В handleGenerateYaml выполняется `JSON.parse(builderSteps || '[]')`, где builderSteps — произвольный текст из textarea. Результат без какой-либо проверки передаётся в `runServiceAction('workflows.generate_yaml', { pipeline_info, steps })`. Если пользователь введёт steps с инъецированными ключами (например, `"__proto__"` или поля с путями к файлам), это уходит напрямую на backend.
- **Влияние:** Prototype pollution на стороне клиента маловероятна, но сгенерированный payload передаётся backend-у без фильтрации. При уязвимости в backend-парсере YAML (например, arbitrary write на основе template injection) это может привести к RCE или path traversal.
- **Исправление:** Валидировать распарсенный JSON: убедиться что это массив объектов с ожидаемыми полями (id, agent, depends_on и т.д.), отклонять __proto__ и прочие опасные ключи. Показывать пользователю ошибку схемы до отправки.
- **Что даст:** Предотвращает передачу неожиданной структуры на backend.

### M136. localStorage читается без sanitize при инициализации шаблонов — возможна persistent XSS при импорте
- **Модуль:** Frontend (Next.js / React AG-UI)
- **Категория:** security | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `frontend/client/src/app/components/sections/DynamicAgentsSection.tsx:115`
- **Проблема:** Начальное состояние templates инициализируется через `JSON.parse(localStorage.getItem('agui-dynamic-templates'))`. При импорте JSON-файла (строка 1328-1337) данные из файла сохраняются в localStorage без какой-либо валидации: `{ ...templates, ...data }`. Если злоумышленник подсунет файл с шаблоном, содержащим field `instructions` с произвольным значением, оно сохранится в localStorage, выживет между сессиями и будет отправлено на backend при следующем создании агента.
- **Влияние:** Persistent injection в instructions агента. Если backend не санирует инструкции перед передачей LLM, это prompt-injection вектор через локальное хранилище.
- **Исправление:** При импорте JSON валидировать схему: разрешить только ожидаемые поля (name, type, model, tools и т.д.) с проверкой типов. Ограничить длину строковых полей.
- **Что даст:** Устраняет persistent injection через импорт файла.

### M137. Отсутствие CSRF-защиты / валидации origin для service actions
- **Модуль:** Frontend (Next.js / React AG-UI)
- **Категория:** security | **Уверенность:** low | **Верификация:** не верифиц.
- **Где:** `frontend/client/src/app/page.tsx:537`
- **Проблема:** copilotkit.setProperties устанавливает service_action и service_payload в свойства агента, затем copilotkit.runAgent отправляет HTTP-запрос на backendUrl. Значение backendUrl берётся из NEXT_PUBLIC_AG_UI_URL без валидации. Если злоумышленник может повлиять на переменную окружения или настройки сборки, он может перенаправить все service_action вызовы на произвольный URL (SSRF с точки зрения браузера). Также нет проверки что backend-ответ (onCustomEvent) действительно пришёл с доверенного origin.
- **Влияние:** Малая вероятность в нормальных условиях деплоя, но отсутствие origin-validation означает что любой CustomEvent, внедрённый через агент, будет обработан как авторизованный ответ.
- **Исправление:** Проверять origin ответа в onCustomEvent, добавить валидацию backendUrl при старте приложения (белый список схем/хостов).
- **Что даст:** Снижение поверхности атаки при компрометации агентского канала.

### M138. test_no_bare_except.py проверяет только один файл из всей кодовой базы
- **Модуль:** Качество тестов
- **Категория:** testing | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `tests/test_no_bare_except.py:19`
- **Проблема:** `SOURCE_PATH = project_root / "StoryBookManager" / "gui" / "universal_json_editor.py"`. Тест проверяет отсутствие bare `except:` исключительно в одном файле. В аудитовом отчёте (memory CLAUDE.md) упоминаются критические проблемы с forbidden_functions и safety в `custom_tools/text_to_sql/` — никакого аналогичного lint-теста для этих модулей нет. Кроме того, тест использует `source.index(...)` без try/except на строках 68 и 70 — если метод `save_editor_to_items_list` будет переименован, тест упадёт с `ValueError`, а не с осмысленным assertion.
- **Влияние:** Bare except в production-критическом коде (text_to_sql, validators, db_exec) остаётся незамеченным. Тест `test_save_editor_uses_logger_error` хрупок к переименованию метода.
- **Исправление:** Расширить проверку bare except на `custom_tools/**/*.py` и `db_plugins/**/*.py`. Обернуть `source.index(...)` в try/except с информативным сообщением.
- **Что даст:** Обнаружение проглоченных исключений в production-пути; менее хрупкий тест.

### M139. test_sqlglot_integration.py: module-level os.environ мутация может влиять на collection phase
- **Модуль:** Качество тестов
- **Категория:** testing | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `tests/test_sqlglot_integration.py:9`
- **Проблема:** Строка `os.environ["USE_SQLGLOT"] = "1"` выполняется при импорте модуля во время collection. Если pytest импортирует этот модуль до других test-модулей, которые предполагают, что USE_SQLGLOT не выставлен или имеет другое значение, то их setup-код (включая module-level код) получит загрязнённое окружение. В отличие от `os.environ.setdefault(...)` (которое используется в test_text_to_sql_safety_config.py), здесь безусловная запись — она перетирает даже явно выставленный `USE_SQLGLOT=0` в CI-окружении.
- **Влияние:** Нарушение изоляции тестов в рамках одного pytest-сеанса; CI с USE_SQLGLOT=0 принудительно переводится в режим USE_SQLGLOT=1.
- **Исправление:** Заменить `os.environ["USE_SQLGLOT"] = "1"` на `os.environ.setdefault("USE_SQLGLOT", "1")` или добавить autouse-фикстуру с monkeypatch.
- **Что даст:** Корректное поведение в CI с кастомными env и при параллельном запуске.

### M140. test_text_to_sql_agui_workflow_contract.py: ручная сохранка/восстановка os.environ без monkeypatch
- **Модуль:** Качество тестов
- **Категория:** testing | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `tests/test_text_to_sql_agui_workflow_contract.py:2082`
- **Проблема:** Тест `test_workflow_manager_accepts_explicit_run_id_contract` вручную сохраняет текущие значения 5 env-переменных (строки 2082–2086), а в finally-блоке восстанавливает (строки 2101–2120). Если `previous_dsn` был None — устанавливает `DB_DSN = None` через `os.environ["DB_DSN"] = None`, что вызовет `TypeError` (os.environ принимает только строки). Также если finally не выполнится из-за process-level сбоя — env загрязняется.
- **Влияние:** Потенциальный `TypeError` при `os.environ["DB_DSN"] = None` — это можно проверить по строкам 2104–2120: `os.environ["KEY"] = previous_value` где `previous_value = os.environ.get(...)` может быть None. Реальное падение зависит от того, выставлен ли DB_DSN в test env.
- **Исправление:** Заменить на monkeypatch.setenv / monkeypatch.delenv. Если None — вызывать `monkeypatch.delenv`, а не устанавливать None в os.environ.
- **Что даст:** Устранение TypeError при None-значении; корректная изоляция.

### M141. test_thread_safety_finish_generation.py: AST-проверки хрупки к рефакторингу и не тестируют реальный поток
- **Модуль:** Качество тестов
- **Категория:** testing | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `tests/test_thread_safety_finish_generation.py:26`
- **Проблема:** Тесты класса `TestFinishGenerationThreadSafety` (строки 82–107) используют AST-парсинг исходного файла `generation_panel.py` для проверки структурных свойств (наличие `self.after(...)`, отсутствие `self.<widget>.config()` на верхнем уровне). Это статический анализ, а не поведенческий тест. `TestFinishGenerationConcurrency.test_multiple_calls_from_threads` (строки 113–151) использует локальную функцию-замену `finish_generation`, не вызывая реальный метод — тест проверяет заглушку, а не production-код. Дополнительно: `_method_has_direct_widget_config` ищет `config()` только на `self.<attr>.<config>`, но не на переменных-алиасах виджетов.
- **Влияние:** Тесты могут оставаться зелёными при реальном нарушении thread-safety в `generation_panel.py`, если рефакторинг переименует переменные или изменит структуру метода без нарушения инварианта `self.after`. `TestFinishGenerationConcurrency` не тестирует реальный код.
- **Исправление:** Дополнить/заменить AST-тесты интеграционными: создать реальный mock Tk-root и вызывать реальный `finish_generation` из потоков. AST-тесты можно оставить как дешёвую статическую сигнализацию, но они не должны быть единственной проверкой.
- **Что даст:** Реальное поведенческое покрытие многопоточного сценария.

### M142. Пробел покрытия: нет тестов для read-only enforcement в DuckDB и Impala плагинах
- **Модуль:** Качество тестов
- **Категория:** testing | **Уверенность:** medium | **Верификация:** скоррект. high→medium
- **Где:** `tests/test_db_plugin_execution_helpers.py:413`
- **Проблема:** В `test_db_plugin_execution_helpers.py` есть тест `test_sapiq_connect_requires_explicit_fail_open_when_read_only_unenforced` (строка 391) и аналогичный для Impala (строка 413). Однако нет аналогичных тестов для DuckDB и Impala в сценарии, когда `read_only_fail_open` НЕ выставлен. Из кода тестов видно, что SAP IQ и Impala `fail closed` по умолчанию без явного opt-in. Для DuckDB такого теста нет — неясно, применяет ли DuckDB read-only enforcement и падает ли он `closed` или `open`. Это критично: если DuckDB Plugin допускает DML без read-only enforcement, это дыра в безопасности для production БД.
- **Влияние:** Возможное тихое разрешение write-операций через DuckDB-плагин, что нарушает принцип read-only доступа в text-to-sql.
- **Исправление:** Добавить тест для DuckDBPlugin, аналогичный test_sapiq_connect_requires_explicit_fail_open: проверить, что DuckDB connect без `read_only_fail_open` либо падает с RuntimeError, либо возвращает read-only соединение. Также аналогичный тест для Impala с MySQL (хотя `test_mysql_read_only_setup_failure_fails_closed` существует).
- **Что даст:** Явная верификация read-only контракта для каждого поддерживаемого плагина.
- **Заметка верификатора:** Частично подтверждено, но impact искажён и severity завышена. Impala-тест ЕСТЬ (test_db_plugin_execution_helpers.py:413). Для DuckDB connect-пути read-only теста действительно НЕТ (упоминания duckdb в тестах — только parse_schema/quote/split DSN). НО утверждение 'неясно, применяет ли DuckDB read-only / возможно тихое разрешение write' — ложно: db_plugins/duckdb.py:32 коннектится с native read_only=True, а при ошибке (строки 34-36) fail-closed по умолчанию (raise 'Failed to open DuckDB database in read-only mode'), open только при явном read_only_fail_open_enabled(dsn). Т.е. enforcement движковый и fail-closed, дыры в проде нет. Реальный пробел — лишь отсутствие регрессионного теста на fail-closed/fail-open ветки connect (как для MySQL: test_mysql_read_only_setup_failure_fails_closed:247 и ...can_explicitly_fail_open:282). Не критично для безопасности → medium, не high.

### M143. Пробел покрытия: нет тестов восстановления workflow при сбое отдельного шага (recovery path)
- **Модуль:** Качество тестов
- **Категория:** testing | **Уверенность:** low | **Верификация:** не верифиц.
- **Где:** `tests/test_incomplete_workflows.py`
- **Проблема:** Файл `test_incomplete_workflows.py` существует, но не был прочитан. Судя по именованию и контексту проекта (memory: `~20 pre-existing TDD failures в test_text_to_sql_core_contracts.py`, AG-UI workflow recovery), нет явного набора тестов для сценариев: (a) шаг pipeline упал, workflow возобновляется с checkpoint, (b) пересоздание run после crash, (c) persistent EventStore корректно восстанавливает состояние после restart процесса. `test_resume_checkpoint_context.py` существует, но его содержимое не проверено на полноту.
- **Влияние:** Непокрытые сценарии восстановления могут привести к потере данных или hang при реальных сбоях в production.
- **Исправление:** Убедиться, что test_incomplete_workflows.py покрывает: шаг X упал, ниже зависящие шаги не запустились, resume стартует с X. Добавить тест на restart-recovery через EventStore.
- **Что даст:** Уверенность в корректности checkpoint/recovery логики.


## ⚪ LOW (62)

### L1. Дублирующийся механизм sqlglot-парсинга с таймаутом: safety.py vs utils.py
- **Модуль:** Text2SQL: ядро + NLU
- **Категория:** architecture | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `/srv/git_projects/MultiAgent/custom_tools/text_to_sql/validators/safety.py:129`
- **Проблема:** В codebase существует два независимых механизма для парсинга sqlglot с таймаутом: `_parse_with_timeout()` в safety.py (per-call executor) и `parse_with_timeout()` в utils.py (module-level pool, max_workers=2). Они используют разные env-переменные для таймаута: `SQL_VALIDATE_PARSE_TIMEOUT_SEC` (safety.py) vs `TEXT_TO_SQL_SQLGLOT_TIMEOUT_S` (utils.py). Метрики `parse_timeout` пишутся только в utils.py путь.
- **Влияние:** Оператор не может централизованно контролировать таймаут парсинга через одну переменную. При отладке производительности путаница — какой таймаут сработал. Dead code в safety.py после перехода на utils.py-паттерн.
- **Исправление:** Унифицировать: заменить `_parse_with_timeout` в safety.py на вызов `parse_with_timeout()` из utils (уже используется в _sql_generation_api.py и _db_exec.py). Убрать `_parse_with_timeout` и `concurrent.futures` из safety.py.
- **Что даст:** Единый контроль таймаута, единые метрики, меньше thread churn.

### L2. Двойной глобальный синглтон: _global_memory_manager + MemoryManager._shared_db_handler — запутанная инициализация
- **Модуль:** Memory / RAG
- **Категория:** architecture | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `memory/manager.py:70,554-565`
- **Проблема:** Существуют два уровня синглтон-паттерна: класс-атрибут `_shared_db_handler` на уровне класса (строка 70) и модульный `_global_memory_manager` (строка 554). При этом `memory_manager = get_memory_manager()` выполняется при импорте модуля (строка 565), что вызывает инициализацию SQLite, ChromaDB и загрузку ML-модели во время import — без возможности отложить инициализацию или передать конфигурацию.
- **Влияние:** Импорт `memory.tools` или `memory.manager` всегда запускает тяжёлую инициализацию. Тесты не могут изолированно подменить зависимости без патчинга.
- **Исправление:** Разделить «регистрацию» синглтона и его инициализацию: убрать вызов `get_memory_manager()` из тела модуля, передавать `memory_manager` явно или через dependency injection.
- **Что даст:** Возможность тестирования без сайд-эффектов при импорте; контроль над временем инициализации.

### L3. LLM max_tokens в llm_models.yaml не имеет профиля muni_ru — рассинхрон с другими конфигами
- **Модуль:** Text2SQL: YAML/конфиги
- **Категория:** architecture | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `config/text_to_sql/llm_models.yaml:18-28`
- **Проблема:** Все остальные text-to-sql YAML-конфиги (significance, main_table_scoring, column_aliases, similarity_thresholds) содержат профиль `muni_ru`. `llm_models.yaml` содержит только `default`. Если `TEXT_TO_SQL_LLM_MODELS_PROFILE=muni_ru` будет выставлен вместе с другими `*_PROFILE=muni_ru` переменными (естественное ожидание при включении muni_ru режима), `get_active_profile()` попытается `config.get_profile('muni_ru')` и упадёт с `KeyError`. Нет никакой автоматической привязки профилей между конфигами.
- **Влияние:** Потенциальный KeyError в проде при системном выставлении одной переменной для всех профилей. Рассинхрон конвенции между yaml-файлами.
- **Исправление:** Добавить профиль `muni_ru` в `llm_models.yaml` (хотя бы как копию default — по аналогии с main_table_scoring). Либо задокументировать в AGENTS.md что llm-профили независимы от domain-профилей.
- **Что даст:** Предотвращение KeyError при смене профиля; согласованность конвенции.

### L4. _workflow_dsn_env удерживает глобальный мьютекс на всё время выполнения workflow
- **Модуль:** Workflow Engine: resilience/orchestration/intelligence
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** скоррект. high→low
- **Где:** `workflow/streamlit_api.py:456`
- **Проблема:** Контекстный менеджер `_workflow_dsn_env` захватывает `_GLOBAL_WORKFLOW_ENV_LOCK` на строке 456 и держит его, пока выполняется `yield` (строки 477-478), то есть на всё время `loop.run_until_complete(...)` — это может быть десятки минут. Такой паттерн превращает Lock в bottleneck: второй параллельный запуск workflow через тот же процесс заблокируется на этом мьютексе навсегда (deadlock-like). Хотя каждый workflow запускается в отдельном процессе (multiprocessing), внутри одного процесса возможны сценарии с несколькими вызовами через один WorkflowManager (тесты, Streamlit reruns).
- **Влияние:** Два одновременных workflow в одном процессе взаимоблокируются. Тестовые сценарии, где WorkflowManager используется напрямую (без fork), полностью сериализуются.
- **Исправление:** Захватывать мьютекс только на время read-modify env vars (до yield и после — в finally), не держать его на весь yield. Паттерн: взять lock, snapshot old, set new, release lock, yield, взять lock, restore, release.
- **Что даст:** Позволяет параллельные запуски workflow в одном процессе без дедлоков.
- **Заметка верификатора:** _workflow_dsn_env (streamlit_api.py:451) держит _GLOBAL_WORKFLOW_ENV_LOCK во время run_until_complete (1074-1082), но эта строка исполняется внутри _execute_workflow_in_context, которую зовёт _run_workflow_thread, а тот — только из _workflow_process_entry (568), т.е. ВНУТРИ выделенного дочернего процесса. Каждый запуск форкается отдельным multiprocessing.Process (844-849), поэтому в одном процессе одновременно идёт ровно один workflow → второй параллельный run в том же процессе в проде невозможен, deadlock не наступает. Публичный run_workflow_async в родителе только спавнит процесс, сам run_until_complete не держит. Тесты зовут _run_workflow_thread последовательно. Реальный prod-импакт отсутствует; глобальная блокировка env через мьютекс корректна для защиты os.environ. Понижено до low (латентный риск только при гипотетическом in-process параллелизме).

### L5. Гонка данных на глобальном синглтоне memory_manager: is_memory_updated / summary без блокировки
- **Модуль:** Memory / RAG
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** скоррект. high→low
- **Где:** `memory/manager.py:96-97, memory/tools.py:952-998`
- **Проблема:** Поля `memory_manager.is_memory_updated` и `memory_manager.summary` являются изменяемым состоянием глобального объекта. `save_memory` пишет `is_memory_updated = True` (tools.py:321) без блокировки. `get_memory_summary` читает флаг, делает дорогой LLM-вызов и записывает summary обратно (строки 952-999) — всё без `lock`. При конкурентных вызовах из нескольких потоков (FastAPI / Streamlit + фоновые агенты) возможен double-LLM-call и race на перезапись summary. Запись в SQLite защищена `memory_manager.db_handler.lock` (строка 296), но кэш-поля — нет.
- **Влияние:** В многопоточных сценариях (Streamlit + несколько агентов) — дублированные дорогие LLM-вызовы или потеря сгенерированного summary. В однопоточном коде проблемы нет.
- **Исправление:** Обернуть чтение-запись `is_memory_updated` + `summary` в `memory_manager.db_handler.lock`, либо использовать threading.Event для флага обновления.
- **Что даст:** Устранение race condition на кэше summary в многопоточных окружениях.
- **Заметка верификатора:** Подтверждено на уровне кода: is_memory_updated (set True на tools.py:321 без lock) и summary читаются/пишутся в get_memory_summary (стр.952 check-then-act -> дорогой LLM на 978 -> запись на 997-998) без блокировки. Гонка реальна. НО: худший исход — дублированный LLM-вызов или безобидная перезапись summary; нет порчи данных, краха или security-импакта, а записи в SQLite заблокированы своим lock. Находка сама признаёт 'В однопоточном коде проблемы нет'. Для редко вызываемого summary-кэша это эффективностная мелочь, а не high.

### L6. TOCTOU гонка при записи sidecar optimization_metadata.yaml
- **Модуль:** prompt_optimizer + telemetry
- **Категория:** concurrency | **Уверенность:** high | **Верификация:** скоррект. high→low
- **Где:** `prompt_optimizer/prompt_optimizer.py:65-68`
- **Проблема:** _write_optimization_metadata_sidecar() выполняет read-modify-write без блокировки: читает весь sidecar, изменяет ключ, записывает обратно. При параллельном запуске оптимизации для нескольких агентов (или повторном вызове optimize_all_agents) два процесса прочитают один и тот же снимок файла и один из них затрёт записи другого.
- **Влияние:** Потеря части метаданных об оптимизации, что приведёт к повторной оптимизации уже обработанных агентов при следующем запуске.
- **Исправление:** Использовать fcntl.flock или filelock (как в _write_trace_event) вокруг операции read-modify-write, либо перейти на append-only формат (каждая запись — отдельная строка JSONL).
- **Что даст:** Корректное обновление sidecar при параллельном использовании.
- **Заметка верификатора:** Факт подтверждён: _write_optimization_metadata_sidecar (65-68) делает read-modify-write без блокировки, в отличие от trace-экспортера, который использует fcntl.flock. Есть два пути вызова optimize_all_agents (run_streamlit.py:224 при старте UI и service.py:2563 action system.prompt_optimizer.run), которые теоретически могут пересечься и потерять записи. НО severity high завышена: внутри один процесс обходит агентов последовательно (цикл 497), а параллельный запуск — нечастый сценарий. Последствие потерянной записи ограничено: повторная оптимизация уже обработанного агента (gate на 528) = лишние вызовы LLM, а не порча/потеря реальных артефактов. Реальных метаданных-ценностей в sidecar нет.

### L7. pii_scanner: regex для телефонов даёт массовые ложные срабатывания
- **Модуль:** Ядро: tool manager + MCP
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/llm_guard_tools.py:41`
- **Проблема:** Паттерн `r'\b\+?[1-9]\d{1,14}\b'` совпадёт с любым числом от 10 до 15 цифр — в том числе с числовыми идентификаторами ОКТМО/ОКАТО, суммами зарплат, ID из БД. В контексте text-to-SQL системы с РФ-муниципальными данными это приводит к тому, что большинство ответов будут помечены как содержащие PII. Тот же паттерн используется в `_pii_regex_scanner` через `Regex(patterns=pii_patterns)`.
- **Влияние:** Ложные блокировки легитимных ответов системы содержащих числовые данные (ОКТМО, ОКАТО, суммы).
- **Исправление:** Уточнить паттерн: добавить требование на пробел/разделитель до числа, ограничить по минимальной длине (не менее 10 цифр для российских номеров), или использовать специализированный RU-паттерн `r'\b(\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}\b'`.
- **Что даст:** Снижение ложных блокировок при работе с муниципальными данными.

### L8. check_forbidden_keywords в _RegexValidator не экранирует keyword через re.escape
- **Модуль:** Text2SQL: ядро + NLU
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `/srv/git_projects/MultiAgent/custom_tools/text_to_sql/validators/safety.py:446`
- **Проблема:** `re.search(fr"\b{forbidden_keyword}\b", upper_sql)` вставляет `forbidden_keyword` напрямую в regex без `re.escape()`. Это отличается от `check_forbidden_functions` (строка 465), который использует `re.escape`. Ключевые слова из safety.yaml (`INSERT`, `DROP`, `MERGE` и т.д.) — только буквы, поэтому сейчас ReDoS невозможен. Но при добавлении в yaml ключевого слова с regex-метасимволом (например, `SELECT+` или `INTO.OUTFILE`) паттерн станет невалидным или неожиданно широким.
- **Влияние:** Пока keywords в yaml — чистые слова, баг спящий. При изменении конфига без review — потенциальный ReDoS или пропуск проверки.
- **Исправление:** Изменить строку 446 на `re.search(rf"\b{re.escape(forbidden_keyword)}\b", upper_sql)` — как это уже сделано для `check_forbidden_functions`.
- **Что даст:** Защита от случайного ReDoS при расширении конфига safety.yaml.

### L9. BEGIN IMMEDIATE проглатывается без логирования — ослабляет гарантии транзакции молча
- **Модуль:** Text2SQL: schema (cache/loader/memory)
- **Категория:** correctness | **Уверенность:** high | **Верификация:** скоррект. high→low
- **Где:** `custom_tools/text_to_sql/schema_memory_sqlite.py:563-570`
- **Проблема:** `except Exception: pass` после `conn.execute('BEGIN IMMEDIATE')` логически эквивалентен silent fallback, запрещённому AGENTS.md. Если `BEGIN IMMEDIATE` падает из-за реальной проблемы (БД занята, WAL-таймаут, `ProgrammingError` при уже открытой транзакции), транзакция начнётся без IMMEDIATE-режима. Комментарий объясняет это как намеренный fallback, но не логирует причину и тип исключения — невозможно понять на production, почему транзакция работает без IMMEDIATE-защиты.
- **Влияние:** Если БД в режиме WAL и два воркера одновременно попадают в `remove_old_schema_records`, без IMMEDIATE у обоих могут пройти SELECT (проверка) — и оба пойдут на UPDATE. Данные не повредятся (UPDATE атомарен), но логика деактивации «старых» записей может дать двойной UPDATE. Важнее: если `BEGIN IMMEDIATE` падает из-за уже открытой транзакции (autocommit-режим sqlite3.Connection), дальнейший `conn.commit()` может коммитнуть чужие изменения.
- **Исправление:** Логировать причину в `except Exception as e: logger.warning('BEGIN IMMEDIATE failed: %s', e)`. Также проверить, что `memory_manager.get_sqlite_connection()` возвращает соединение с `isolation_level=None` (autocommit) или гарантированно без открытых транзакций.
- **Что даст:** Диагностируемость проблем с транзакциями в production; предотвращение ошибок коммита чужих данных.
- **Заметка верификатора:** Риски из находки в проде не реализуются. get_sqlite_connection() (manager.py:252 → database.py:86) возвращает СВЕЖИЙ per-call sqlite3.connect; conn используется только в этой функции и закрывается в finally (строка 600) — поэтому 'commit чужих изменений' невозможен. UPDATE атомарен на выделенном коннекте; вся операция уже внутри `with self._write_lock_cm()` (строка 553, fcntl.flock сериализует writers между процессами), так что WAL-гонка двух воркеров уже исключена внешним локом — BEGIN IMMEDIATE здесь defense-in-depth, а не основная защита. Реальная проблема — только наблюдаемость: `except Exception: pass` (строки 565-570) выбрасывает тип/текст исключения без лога, на проде не видно ПОЧЕМУ IMMEDIATE пропущен. Это мелкий diagnostics-gap, без потери/повреждения данных. Severity high завышена → low.

### L10. SchemaFileManager.load_schema_from_file использует errors='ignore' при чтении JSON
- **Модуль:** Text2SQL: schema (cache/loader/memory)
- **Категория:** correctness | **Уверенность:** high | **Верификация:** скоррект. high→low
- **Где:** `custom_tools/text_to_sql/schema_loader.py:288`
- **Проблема:** `file_path.read_text(encoding='utf-8', errors='ignore')` тихо отбрасывает невалидные UTF-8 байты. JSON-файл схемы содержит имена таблиц, колонок, описания. Удаление байт из UTF-8 строки приводит к corruption данных: описания колонок с кириллическими символами будут обрезаны или искажены. Результирующий JSON может стать невалидным (обрезанные JSON-строки), что вызовет `JSONDecodeError` после silent corrupttion. Особенно опасно для МФ-датасета с описаниями на русском языке (по MEMORY.md — мунициципальный РФ-датасет).
- **Влияние:** Молчаливая потеря данных схемы (описаний колонок на кириллице). LLM получает неполный или искажённый контекст → деградация качества text-to-SQL без видимой причины.
- **Исправление:** Убрать `errors='ignore'`, использовать `errors='strict'` (дефолт). Оборачивать в блок с явной обработкой `UnicodeDecodeError`. `SchemaLoader._load_sqlrag_schema` (строка 79) уже читает без `errors='ignore'` — нужно привести к тому же стилю.
- **Что даст:** Предотвращение silent corruption данных схемы; явная диагностика проблем кодировки.
- **Заметка верификатора:** Принцип верен (errors='ignore' на JSON может молча обрезать кириллицу), но это dead code: SchemaFileManager / load_schema_from_file не имеют ни одного вызова во всей кодовой базе (grep по всему репозиторию даёт совпадения только внутри schema_loader.py). Прод-путь чтения schema-файла идёт через ensure_schema_indexed_in_memory (schema_memory_sqlite.py:377), где используется plain read_text(encoding='utf-8') БЕЗ errors='ignore' — то есть fail-fast на битом UTF-8. Файл пишется save_schema_to_file с ensure_ascii=False в валидном UTF-8, так что битые байты потребовали бы внешнего повреждения. Живого пути потери данных нет. Severity high завышена для недостижимого кода → low (latent code-quality issue).

### L11. coerce_str_list допускает пустые строки в списках значимости и schema-linking
- **Модуль:** Text2SQL: YAML/конфиги
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/_yaml_config_loader.py:177-183`
- **Проблема:** `coerce_str_list` используется в `significance_config.py` и `schema_linking_examples_config.py` для полей `high_priority_exact`, `priority_id_columns`, `critical_description_keywords` и т.п. Docstring явно говорит «Пустые строки НЕ отвергаются (по совместимости)». Пустая строка `""` в `high_priority_exact` или `priority_id_columns` создаёт silent семантическую ошибку: `"" in column_name.lower()` всегда `True` — все колонки станут «значимыми» / «приоритетными».
- **Влияние:** Если YAML-файл содержит пустую строку в списке (например, опечатка или мерж-конфликт), схема-линкинг начнёт считать все колонки приоритетными. Баг трудно обнаружим — поведение меняется тихо, без ошибки при старте.
- **Исправление:** В `coerce_str_list` добавить опцию `reject_empty_strings: bool = False` и выставить её в `True` для полей significance/schema-linking. Либо добавить post-parse проверку в `SignificanceConfig` / `SchemaLinkingExamplesConfig`: `if "" in profile.high_priority_exact: raise ValueError(...)`.
- **Что даст:** Fail-fast при YAML-опечатке; устранение silent semantic bug.

### L12. Формула distance→score некорректна для метрики cosine в ChromaDB
- **Модуль:** Memory / RAG
- **Категория:** correctness | **Уверенность:** high | **Верификация:** скоррект. high→low
- **Где:** `memory/tools.py:542`
- **Проблема:** Используется формула `score = max(0.0, 1.0 - distance / 2)`. ChromaDB с `hnsw:space=cosine` возвращает distance в диапазоне [0, 2], где 0 = идеальное совпадение, 2 = полная противоположность. Формула `1 - d/2` даёт score ∈ [0, 1], что математически верно для cosine. НО: та же формула применена в streamlit_api.py:366 и :411 `1.0 - distance / 2`. Это работает для cosine, но при переключении на метрику l2 (расстояние ∈ [0, ∞)) или ip (∈ (−∞, ∞)) формула даёт неправильные значения. Код в database.py допускает смену метрики через env `TEXT_TO_SQL_CHROMA_METRIC`, но формула перевода не обновляется.
- **Влияние:** При смене `TEXT_TO_SQL_CHROMA_METRIC` на `l2` или `ip` все пороговые фильтрации (vector_threshold) и ранжирование становятся некорректными — возможен отсев релевантных записей или включение нерелевантных.
- **Исправление:** Добавить функцию `_distance_to_score(distance, metric)` с ветками для cosine, l2, ip. Вызывать её вместо инлайн-формулы. Либо жёстко запретить метрики кроме cosine через assert при инициализации.
- **Что даст:** Корректная работа ранжирования при изменении конфигурации метрики.
- **Заметка верификатора:** Формула `1 - d/2` математически верна для cosine — это default и, по явному комментарию в database.py:208-210, ЕДИНСТВЕННАЯ метрика, под которую написана downstream-логика. env TEXT_TO_SQL_CHROMA_METRIC задокументирован как опциональный с предупреждением, что только cosine поддерживается (плюс есть runtime warning при несовпадении метрики коллекции, database.py:239-248). Проблема проявляется ТОЛЬКО если оператор намеренно переключит метрику на l2/ip вопреки явному предупреждению в коде. Сама находка признаёт 'Это работает для cosine'. В дефолтном прод-пути бага нет — это hardening-заметка, не high.

### L13. story_editor_tool сохраняет backup только один раз, затем перезаписывает без backup
- **Модуль:** Storybook: инструменты генерации
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/story_editor.py:240-248`
- **Проблема:** Backup (`story.json.backup`) создаётся только если файл backup ещё не существует. При последующих запусках редактора (force_edit=True) backup не обновляется, и `story.json` перезаписывается. Backup всегда содержит первоначальную версию, но не предыдущую версию перед последним редактированием.
- **Влияние:** Если последовательно запустить story_editor дважды, второй запуск перезапишет первый результат, и восстановить первый результат будет нельзя (backup содержит оригинал, а не промежуточную версию).
- **Исправление:** Переименовывать backup с timestamp или хранить rolling backup (story.json.backup.1, .backup.2). Или принять текущее поведение явно в документации.
- **Что даст:** Предотвращает безвозвратную потерю промежуточных версий истории.

### L14. Отсутствует проверка exists() у backup_path при restore_from_backup перед записью
- **Модуль:** StoryBookManager (десктоп-приложение)
- **Категория:** correctness | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `StoryBookManager/core/file_manager.py:276`
- **Проблема:** `restore_from_backup` принимает `backup_path: str` и проверяет `backup_path.exists()`. Но параметр валидируется только на существование файла, а не на принадлежность директории бэкапов `self.backup_dir`. Пользователь (или код) может передать произвольный абсолютный путь `/etc/passwd` в качестве backup_path, и `shutil.copy2` скопирует его поверх целевого JSON-файла проекта.
- **Влияние:** Запись произвольного файла (чтение из любого доступного пути) в рабочий JSON проекта.
- **Исправление:** Добавить проверку `Path(backup_path).resolve().relative_to(self.backup_dir.resolve())` перед `shutil.copy2`.
- **Что даст:** Устраняет возможность подстановки произвольного файла при восстановлении.

### L15. join_validation.py: рекурсивный DFS без ограничения глубины может переполнить стек
- **Модуль:** Text2SQL: schema-linking + RAG
- **Категория:** correctness | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/schema_linking/join_validation.py:115`
- **Проблема:** Функция `_has_cycle` использует рекурсивный DFS (`_dfs` вызывает себя). При большом числе таблиц (например, несколько сотен) глубина рекурсии может достигнуть Python's default recursion limit (1000). В production-схемах с M:N bridge-таблицами это реально — `_extract_fk_joins` может сгенерировать десятки рёбер.
- **Влияние:** При большой схеме `validate_llm_joins` упадёт с `RecursionError`. Это не security-критично, но блокирует весь pipeline schema-linking для схем с большим числом связей.
- **Исправление:** Переписать `_dfs` итеративно (explicit stack вместо call stack), или добавить `sys.setrecursionlimit` с явным ограничением и документацией.
- **Что даст:** Устойчивость к большим схемам без RecursionError.

### L16. task_queue создаётся но никогда не используется
- **Модуль:** Ядро: оркестрация агентов
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `agent_system.py:37`
- **Проблема:** `self.task_queue = asyncio.Queue()` создаётся в `__init__` `DynamicAgentSystem`, но ни одно место в коде не кладёт в него задачи и не читает из него. Grep по `task_queue` возвращает только строку инициализации.
- **Влияние:** Мёртвый код. В asyncio-контексте `Queue` создаётся без привязки к event loop (до Python 3.10 это иногда вызывало предупреждения). Небольшая утечка памяти при создании множества экземпляров `DynamicAgentSystem`.
- **Исправление:** Удалить `self.task_queue = asyncio.Queue()`.
- **Что даст:** Чище код, нет вводящего в заблуждение неиспользованного атрибута.

### L17. if 1 == 1 — мёртвый условный код, логирование всегда включено
- **Модуль:** Ядро: LLM-модель + codeinterpreter
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `retry_openai_model.py:1161`
- **Проблема:** Условие `if 1 == 1:` перед вызовом `self._write_response_log()` — это всегда True. Очевидно, что изначально было `if DEBUG:` или аналогичный флаг, который был заменён на захардкоженный True. В результате логирование каждого ответа в файл включено безусловно: на каждый LLM-запрос создаётся JSON-файл в `logs/llm_responses/`.
- **Влияние:** Бесконтрольный рост диска; потенциальная утечка данных (содержимое LLM-ответов, включая tool_calls с параметрами SQL-запросов, сохраняется на диск); производительность при большом числе запросов.
- **Исправление:** Заменить на `if self.debug_logging:` с флагом из конфига; добавить ротацию логов или TTL-очистку.
- **Что даст:** Контролируемое логирование; защита от переполнения диска; потенциальная защита от утечки данных в логах.

### L18. Дублирующийся импорт `re` и теневание модуля `html`
- **Модуль:** Ядро: utils + logging + html
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `html_utils.py:3`
- **Проблема:** Модуль `re` импортирован дважды (lines 3 и 10). Модуль `html` импортирован (line 5), но локальная переменная `html = markdown2.markdown(...)` на line 166 затеняет его в пределах метода `_convert_markdown`, делая `html.escape()` недоступным там же. Это не вызывает ошибки, но является источником скрытого бага (см. XSS-находку выше).
- **Влияние:** Дублирующийся импорт не влияет на работу, но затеняние модуля `html` означает, что разработчик, пытающийся использовать `html.escape()` внутри метода, получит AttributeError или неожиданное поведение.
- **Исправление:** Удалить второй `import re` (line 10). Переименовать локальную переменную `html` в `html_str` или `md_html` на lines 166, 171, 174, 175, 178.
- **Что даст:** Устраняется источник скрытого бага с XSS (нет соблазна использовать затенённый `html.escape`) и уменьшается cognitive overhead.

### L19. analysis_tools.py: fact_checking — заглушка без реализации
- **Модуль:** custom_tools (прочие инструменты)
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/analysis_tools.py:17`
- **Проблема:** Функция `fact_checking(claim, sources)` возвращает строку-заглушку `"Проверка '{claim}' по {len(sources)} источникам"` без реальной проверки. Аналогично `analysis()` вызывает `df.describe()` на входных данных без учёта типов, что упадёт с `ValueError` если передать non-numeric DataFrame.
- **Влияние:** Агенты, полагающиеся на `fact_checking`, получают ложное подтверждение вместо реального. `analysis()` — падение при non-numeric данных.
- **Исправление:** Либо реализовать, либо явно пометить `raise NotImplementedError` и задокументировать как stub.
- **Что даст:** Честный контракт с вызывающим кодом.

### L20. Мёртвая функция escape_sql_string в dialects.py — использует ручное экранирование вместо sql_string_literal
- **Модуль:** Text2SQL: генерация SQL
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/dialects.py:200`
- **Проблема:** Функция `escape_sql_string` экранирует только одинарные кавычки через replace, не учитывает диалект, не обрамляет в кавычки, игнорирует NUL-байты и backslash в MySQL. Функция `sql_string_literal` в том же файле делает это правильно через sqlglot. При поиске по всей кодовой базе `escape_sql_string` нигде в области ревью не вызывается.
- **Влияние:** Если кто-то случайно использует `escape_sql_string` вместо `sql_string_literal`, получится небезопасный SQL-литерал без диалект-aware обработки. Для MySQL backslash внутри строки не будет экранирован.
- **Исправление:** Удалить `escape_sql_string` или добавить `raise DeprecationWarning` и перенаправить на `sql_string_literal`.
- **Что даст:** Устраняет потенциальный источник SQL-инъекции при случайном использовании неправильной функции.

### L21. Мёртвый код: _coerce_int в main_table_scoring_config.py
- **Модуль:** Text2SQL: YAML/конфиги
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/main_table_scoring_config.py:195-197`
- **Проблема:** Функция `_coerce_int` объявлена как «совместимость со старым API», но не вызывается нигде в кодовой базе (grep по всем `.py` подтверждает: единственное упоминание — само определение). Комментарий «Совместимость со старым API» предполагает, что она должна была использоваться при миграции, но не была убрана.
- **Влияние:** Мёртвый код вводит в заблуждение — кажется, что есть путь кода с отрицательными весами. Реально _coerce_weight уже принимает min_value=-(2**31) при нужде.
- **Исправление:** Удалить `_coerce_int`.
- **Что даст:** Читаемость кода; устранение ложной точки расширения.

### L22. Дублирование logs.search / logs.search_advanced — идентичный код
- **Модуль:** Backend FastAPI / AG-UI
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `backend/fastapi_app/agui/service.py:3355`
- **Проблема:** Блоки if action == 'logs.search' (строки 3355–3379) и if action == 'logs.search_advanced' (строки 3380–3404) идентичны побайтово — один и тот же вызов _search_logs_advanced с теми же параметрами. Оба добавлены для backward-compat, но это мёртвый дубликат.
- **Влияние:** Любое изменение нужно вносить дважды; ошибка рассинхронизации при правках.
- **Исправление:** Объединить в один блок: if action in ('logs.search', 'logs.search_advanced').
- **Что даст:** Устранение дублирования.

### L23. base.py: dead branch в _normalize_constraint_type — elif для уже проверенных значений
- **Модуль:** DB Plugins
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `db_plugins/base.py:241`
- **Проблема:** Ветка `elif constraint_str in {"PK", "FK", "UNIQUE"}:` никогда не достигается: если constraint_str == "PK", он уже обработан на строке 235 (`constraint_str == "PK"`); аналогично для FK (237) и UNIQUE (239). Эта ветка является мёртвым кодом.
- **Влияние:** Нет функционального эффекта, но вводит читателя в заблуждение о логике нормализации.
- **Исправление:** Удалить строки 241-242 целиком.
- **Что даст:** Улучшает читаемость функции нормализации.

### L24. Дублирование _parse_duration_from_timing / _time_str_to_seconds в четырёх модулях
- **Модуль:** Storybook: видео/shots-генераторы
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/video_generator.py:514,546 / video_generator_mm_tool.py:603,637 / video_generator_aitunnel_tool.py:731,748`
- **Проблема:** Функции `_parse_duration_from_timing` и `_time_str_to_seconds` скопированы дословно (с мелкими вариациями дефолтного значения: 5, 6, 6) в трёх файлах. Четвёртая копия `_time_str_to_seconds` уже существует в screenplay_shots_generator_utils/timing_utils.py и экспортируется через __init__.py.
- **Влияние:** Изменение формата timing требует правок в 3-4 местах; расхождение дефолтов (5 vs 6 секунд) уже произошло.
- **Исправление:** Перенести обе функции в video_generator_common.py, убрать локальные копии.
- **Что даст:** Единое место для правок, консистентные дефолты.

### L25. Неиспользуемый импорт fcntl в screenplay_shots_generator.py
- **Модуль:** Storybook: видео/shots-генераторы
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/screenplay_shots_generator.py:7`
- **Проблема:** `import fcntl` задекларирован с комментарием 'Для блокировки файлов', но нигде в файле fcntl не используется. Файловая блокировка для shots.json реализована через threading.Lock (_SHOTS_WRITE_LOCK), не через fcntl.
- **Влияние:** Вводит в заблуждение (предполагает file-level locking), добавляет ненужную зависимость, ломается на Windows (fcntl — POSIX-only).
- **Исправление:** Удалить строку `import fcntl`.
- **Что даст:** Чище импорты, совместимость с Windows при необходимости, нет ложного сигнала о fcntl-блокировке.

### L26. Дублирование логики синхронизации сцены screenplay: 'dialogue' и 'visual'/'camera' пишутся дважды
- **Модуль:** StoryBookManager (десктоп-приложение)
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `StoryBookManager/gui/editor_panel.py:1638`
- **Проблема:** В `sync_current_scene_data` поля `dialogue` (строки 1638–1641 и 1651–1652), `visual` (1643–1644 и 1659–1660), `camera` (1646–1648 и 1663–1664) записываются дважды: первый раз с `if 'dialogue' in self.current_scene_vars`, второй раз теми же блоками немного ниже (строки 1650–1676). Это не ошибка с точки зрения результата (второй write тот же), но сигнализирует о незавершённом рефакторинге.
- **Влияние:** Мёртвый код: лишние операции `Text.get()` на каждый вызов sync, незначительно замедляющие UI-поток.
- **Исправление:** Удалить дублирующие блоки (строки 1650–1676).
- **Что даст:** Читаемость и устранение скрытых рисков при будущих изменениях.

### L27. Дублирование функции decodeGzipBase64 в 4+ файлах
- **Модуль:** Frontend (Next.js / React AG-UI)
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `frontend/client/src/app/components/sections/TextToSqlSection.tsx:88`
- **Проблема:** Функция `decodeGzipBase64` (10 строк) скопирована идентично в TextToSqlSection.tsx:88, DynamicAgentsSection.tsx:87, ActionCardSections.tsx:26, WorkflowsSection.tsx (вероятно). Аналогично `openReport` продублирована минимум в 3 компонентах.
- **Влияние:** При необходимости исправить баг (например, обработка ошибки Response.body) нужно обновлять 4 места. Уже сейчас TextToSqlSection.tsx:154 не вызывает revokeObjectURL после window.open (в отличие от DynamicAgentsSection, где URL.revokeObjectURL вызывается немедленно, строка 597).
- **Исправление:** Вынести decodeGzipBase64 и openReport в shared-утилиту (например, frontend/client/src/app/utils/report.ts).
- **Что даст:** Единая точка исправления, устранение расхождений в поведении.

### L28. Мёртвый _file_handles в LocalJSONLExporter.shutdown()
- **Модуль:** prompt_optimizer + telemetry
- **Категория:** dead-code | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `telemetry/smolagents_telemetry.py:217`
- **Проблема:** _file_handles инициализируется как пустой словарь (строка 217) и никогда не заполняется — новый подход использует os.open/os.close на каждой записи. Метод shutdown() (строка 387) итерируется по этому всегда-пустому словарю и ничего не делает.
- **Влияние:** Нет функционального ущерба, но misleading код: читатель думает, что файлы держатся открытыми и закрываются в shutdown, хотя это не так.
- **Исправление:** Удалить self._file_handles и соответствующую логику в shutdown().
- **Что даст:** Устраняет путаницу при анализе кода.

### L29. SecurityConfig.blocked_sql_keywords не применяется нигде в reviewed-области — dead config
- **Модуль:** Ядро: configuration & streamlit API
- **Категория:** dead-code | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `configuration_api.py:66-96`
- **Проблема:** SecurityConfig хранит blocked_sql_keywords, allowed_functions, allowed_sql_operations, table_whitelist, table_blacklist и пр. (строки 71-96). В области ревью ни configuration_api.py, ни agent_streamlit_api.py эти поля не читают и не применяют. Согласно memory-контексту аудита (2026-05), deny-list forbidden_functions уже мёртв при USE_SQLGLOT=1. SecurityConfig хранит поля безопасности, которые создают иллюзию защиты, не выполняя её.
- **Влияние:** Операторы системы могут полагать, что настройка blocked_sql_keywords в UI действительно блокирует запросы, тогда как реального enforcement нет.
- **Исправление:** Либо добавить enforcement при передаче задачи агенту, либо убрать поля из UI и добавить явный TODO-комментарий с указанием на место enforcement.
- **Что даст:** Устранение ложного ощущения защищённости; явный контракт безопасности.

### L30. Dead code: `enforce_row_limit` не подключён к pipeline, но поддерживается тестами
- **Модуль:** Text2SQL: валидаторы безопасности
- **Категория:** dead-code | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/validators/schema_limiter.py:263`
- **Проблема:** Docstring функции явно говорит: `"UNUSED utility, designed for future caller (db_exec row-cap enforcement); not currently wired into pipeline."`. Тем не менее функция содержит нетривиальную логику (AST-парсинг, timeout, multi-statement guard, re-rendering). Она занимает ~130 строк и покрыта тестами, создавая поверхность сопровождения без реального применения.
- **Влияние:** Техдолг: при обновлении sqlglot или изменении контракта `_parse_with_timeout` потребуется синхронизировать эту функцию. Риск: когда функцию подключат к db_exec, проблема с re-rendering (#7 выше) реализуется в production.
- **Исправление:** Либо подключить к pipeline (исправив re-rendering), либо удалить с тестами до момента реальной необходимости.
- **Что даст:** Уменьшает площадь сопровождения; при удалении устраняет связанный баг с re-rendering.

### L31. autosave_schema проглатывает все исключения включая ошибки записи файла
- **Модуль:** Text2SQL: schema (cache/loader/memory)
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/schema_loader.py:175-204`
- **Проблема:** `except Exception as e: logger.warning(f'Failed to autosave schema: {e}')` без re-raise. Если диск заполнен или нет прав записи в `sqlrag/`, схема не сохраняется на диск. При следующем рестарте сервиса схема будет переполучена через интроспекцию — это overhead, а не катастрофа. Но ошибка видна только как warning, а не error, что затрудняет мониторинг.
- **Влияние:** Незаметная деградация: автосохранение схемы не работает (например, FS RO), каждый старт требует интроспекции БД. При медленной/недоступной БД — задержки старта.
- **Исправление:** Изменить `logger.warning` на `logger.error`. Решение о re-raise остаётся за командой (автосохранение намеренно best-effort).
- **Что даст:** Лучшая видимость проблем с ФС в мониторинге/алертах.

### L32. _ensure_sqlrag_files_indexed: потеря исключений из _remove_old_file_records для удалённых файлов
- **Модуль:** Text2SQL: schema-linking + RAG
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/rag/indexing.py:276`
- **Проблема:** При обнаружении удалённых файлов (строка 275) вызов `_remove_old_file_records(session_id, filename)` оборачивается в `try/except Exception` — любые исключения (включая сигналы о реальных проблемах: AttributeError, RuntimeError) логируются как warning и проглатываются. При этом `known_files.pop(filename, None)` вызывается в блоке `finally`, то есть запись удаляется из registry даже если деактивация в SQLite/Chroma провалилась.
- **Влияние:** Registry считает файл удалённым, но orphaned-записи в SQLite/Chroma остаются активными. При следующем restart файл не будет обработан повторно — расхождение между registry и memory нарастает.
- **Исправление:** Не удалять `known_files[filename]` при неудачном `_remove_old_file_records`. Сохранить исходный блок `try/except`, но убрать `finally: known_files.pop(...)` — делать pop только при успехе.
- **Что даст:** Согласованность между in-memory registry и SQLite/Chroma state.

### L33. WorkflowExecutionError в _execute_workflow_from_yaml проглатывает оригинальный тип ошибки
- **Модуль:** Workflow Engine: ядро
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `workflow/engine.py:1499-1501`
- **Проблема:** В блоке `except Exception as e: raise WorkflowExecutionError(f"...: {e}")` (строки 1499-1501) оригинальное исключение не пробрасывается через `raise ... from e`. Трейсбек теряется — при отладке сложно понять реальную причину ошибки. Аналогично в `resume_workflow` (строка 384).
- **Влияние:** Потеря трейсбека; затруднённая диагностика ошибок в продакшне.
- **Исправление:** Использовать `raise WorkflowExecutionError(...) from e` во всех аналогичных местах.
- **Что даст:** Полный трейсбек в логах; ускорение диагностики.

### L34. streamlit_api.test_connection: соединение не закрывается при исключении во время выполнения запросов
- **Модуль:** DB Plugins
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** скоррект. high→low
- **Где:** `db_plugins/streamlit_api.py:391-440`
- **Проблема:** В `test_connection()` соединение создаётся на строке 391 (`conn = plugin.connect(dsn)`), но закрывается только в блоке `if conn:` (строка 427). Если между connect и close произойдёт исключение в блоке `except Exception as e` (строка 436) — например, при неожиданном типе conn — `plugin.close(conn)` никогда не вызывается. Блок `finally` отсутствует.
- **Влияние:** При многократных неудачных тестах соединений (например, в UI-петле Streamlit) накапливаются незакрытые соединения к БД, что исчерпывает connection pool.
- **Исправление:** Обернуть использование conn в `try/finally`: создать conn, затем `try: ... finally: plugin.close(conn)`. Это стандартный паттерн, уже применённый в _cursor().
- **Что даст:** Предотвращает утечку соединений в UI-сценарии.
- **Заметка верификатора:** Технически верно, что блока finally нет и close() вызывается только внутри if conn: (строка 427). Однако все операции между connect и close обёрнуты в собственные try/except: execute_select — try на строке 398-410, introspect_schema — try на 413-423, сам close — try на 426-429. Поэтому на практике close ДОСТИГАЕТСЯ почти всегда; внешний except (436) ловит в основном падение самого connect(), когда соединения ещё нет. Реальная утечка возможна лишь при экзотическом падении в неперехваченных строках 394-395 (арифметика времени — не бросает). Impact 'исчерпание connection pool' завышен. Это вопрос стиля (нужен finally), severity low.

### L35. Bare except в rebuild.py маскирует все ошибки при удалении коллекций
- **Модуль:** Memory / RAG
- **Категория:** error-handling | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `memory/rebuild.py:38,43`
- **Проблема:** Блоки `except: pass` при удалении ChromaDB-коллекций (строки 38, 43) перехватывают абсолютно все исключения, включая KeyboardInterrupt, SystemExit, MemoryError. Намерение — «коллекция может не существовать» (комментарий), для этого достаточно ловить `Exception` или конкретный тип ChromaDB.
- **Влияние:** Невидимые критические ошибки (например, потеря соединения с ChromaDB) при rebuild маскируются как «нормальная» ситуация.
- **Исправление:** Заменить на `except Exception: pass` или ловить конкретный ChromaDB-исключение (`chromadb.errors.InvalidCollectionException`).
- **Что даст:** Критические ошибки при rebuild перестанут маскироваться.

### L36. В _stale_handlers нет ограничения на рост списка
- **Модуль:** Text2SQL: ядро + NLU
- **Категория:** error-handling | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `/srv/git_projects/MultiAgent/custom_tools/text_to_sql/core/_audit.py:60`
- **Проблема:** `_stale_handlers: List[...]` растёт при каждой смене параметров max_bytes/backups через env без удаления старых элементов (только atexit очищает). В комментарии написано «рост ограничен числом переключений env», но при автоматической ротации конфигурации или тестах с многократным изменением env это ограничение неявное. Каждый stale-handler держит открытый файловый дескриптор до atexit.
- **Влияние:** При интеграционных тестах, циклически меняющих `AUDIT_LOG_MAX_BYTES`, или при горячем конфигурировании — утечка FD до конца жизни процесса. В типичном сценарии — не критично.
- **Исправление:** Добавить cap на `_stale_handlers` (например, 16): при превышении — close и удалить самый старый под lock. Или закрывать стейл-handler с задержкой (например, через слабую ссылку + GC).
- **Что даст:** Предотвращение FD-утечки при частой смене конфига аудита.

### L37. Необработанное исключение из request.is_disconnected() внутри replay_stream
- **Модуль:** Backend FastAPI / AG-UI
- **Категория:** error-handling | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `backend/fastapi_app/main.py:147`
- **Проблема:** В replay_stream и _stream_agent_events вызов await request.is_disconnected() может бросить RuntimeError / Exception при разрыве соединения в некоторых версиях Starlette (известный баг с on_startup, зафиксированный в project memory). Исключение внутри async generator пробросится наружу без try/except, оставит подписчика в info.subscribers (если в _stream_agent_events это произойдёт до stream.aclose), что нарушит cancel_if_orphaned.
- **Влияние:** Потенциальная утечка подписчика в run_manager и некорректная оценка orphaned-статуса после разрыва.
- **Исправление:** Обернуть await request.is_disconnected() в try/except и при исключении вести себя как при disconnect=True.
- **Что даст:** Корректный cleanup подписчика при аномальных разрывах соединения.

### L38. Ранний return [] внутри блока with conn при семантическом поиске — соединение не закрывается
- **Модуль:** Memory / RAG
- **Категория:** error-handling | **Уверенность:** medium | **Верификация:** скоррект. high→low
- **Где:** `memory/tools.py:576-602`
- **Проблема:** Внутри `try:` блока, где открыт `conn = memory_manager.db_handler._get_connection()`, есть три пути `return []` (строки 576, 600, 602) до достижения `finally: conn.close()`. Соединение SQLite создаётся вне `with`-контекста, закрывается только в `finally` самого внешнего try (строка 794). Прямые `return []` на строках 576, 600, 602 выполнятся корректно — finally на строке 794 сработает. Однако комбинация с ранним return в той же функции делает код хрупким: при любом рефакторинге блока легко пропустить закрытие.
- **Влияние:** В текущем коде finally-блок на строке 794 защищает корректно. Риск реализуется при неосторожном рефакторинге вложенных блоков. Фактической утечки сейчас нет, но архитектурный риск высокий.
- **Исправление:** Использовать `with memory_manager.db_handler.get_connection() as conn:` или явно поднять ранние `return []` до уровня, где `conn` ещё не открыт (с `use_semantic_results = False` и fallback).
- **Что даст:** Исключение класса утечек соединений при будущих правках логики.
- **Заметка верификатора:** Прочитал код: conn открывается на стр.490 ВНЕ with-контекста, обёрнут в try (стр.491) с `finally: conn.close()` на стр.794. Ранние `return []` (стр.576/600/602) находятся внутри этого try, поэтому finally на 794 для них СРАБАТЫВАЕТ — соединение закрывается. Сама находка это прямо признаёт: 'Фактической утечки сейчас нет'. Это чисто гипотетический риск будущего рефакторинга, а не баг в проде. Severity 'high' для несуществующей утечки завышена — это в лучшем случае заметка по maintainability.

### L39. Незакрытый файл при исключении в load_json_file
- **Модуль:** StoryBookManager (десктоп-приложение)
- **Категория:** error-handling | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `StoryBookManager/core/file_manager.py:59`
- **Проблема:** Паттерн `with open(...) as f: data = json.load(f)` корректен — файл закрывается при исключении. Однако метод `validate_data` (строки 137–170) инициализирует `SchemaIntrospector()` на каждый вызов, что каждый раз открывает и парсит `ui_config.json` (через `_load_ui_config`). При большом файле и частом сохранении (автосейв каждые 30 с) это избыточно.
- **Влияние:** N+1 открытий ui_config.json при каждом сохранении каждого JSON-файла проекта.
- **Исправление:** Кэшировать `SchemaIntrospector()` как атрибут `FileManager.__init__` (он уже создаётся в `EditorPanel` — можно передавать как зависимость).
- **Что даст:** Сокращение дисковых операций при частом сохранении.

### L40. run_streamlit.py: docstring функции расположен после первых выполняемых строк
- **Модуль:** Ядро: configuration & streamlit API
- **Категория:** maintainability | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `run_streamlit.py:158-165`
- **Проблема:** В функции run_streamlit() строки 159-163 (import os, чтение переменных окружения) стоят ДО строки-docstring на строке 165. Python не считает строку 165 docstring функции — это просто строковой литерал в середине тела функции. run_streamlit.__doc__ будет None.
- **Влияние:** help(run_streamlit) возвращает None, инструменты документации пропускают описание функции.
- **Исправление:** Переместить docstring на первую строку тела функции (перед import os).
- **Что даст:** Корректная документация функции.

### L41. image_tools: _edit_file_via_direct_mcp(_with_output) — дублирование кода ~90 строк
- **Модуль:** custom_tools (прочие инструменты)
- **Категория:** maintainability | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/image_tools.py:538`
- **Проблема:** Функции `_edit_file_via_direct_mcp` (538-622) и `_edit_file_via_direct_mcp_with_output` (624-734) — практически идентичны: одинаковый код загрузки `mcp_servers.json`, создания `StdioServerParameters`, вызова `session.call_tool`. Разница только в формировании `output_path` и парсинге ответа. Дублирование ~150 строк.
- **Влияние:** Правки приходится вносить в два места; баги синхронизируются не всегда.
- **Исправление:** Объединить в одну функцию с параметром `output_path: Optional[str]`.
- **Что даст:** Уменьшение дублирования, упрощение поддержки.

### L42. Трёхкратное дублирование профиля `extended`/`strict` в safety.yaml
- **Модуль:** Text2SQL: валидаторы безопасности
- **Категория:** maintainability | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `config/text_to_sql/safety.yaml:106-304`
- **Проблема:** Профили `extended` и `strict` полностью идентичны (205 vs 93 строк — `strict` = точная копия `extended`). При добавлении новой опасной функции нужно обновлять оба раздела. Yaml-стандарт поддерживает anchors (`&anchor`/`*alias`) для DRY-дедупликации.
- **Влияние:** При рассинхронизации двух профилей деплоймент с `TEXT_TO_SQL_SAFETY_PROFILE=strict` получит устаревший deny-list. Исторически такие рассинхронизации уже происходили (W8-T8 добавил ClickHouse-функции в оба профиля с комментарием-напоминанием).
- **Исправление:** Использовать YAML anchors: `extended: &extended_profile forbidden_keywords: [...]` и `strict: <<: *extended_profile`.
- **Что даст:** Единое место обновления; исключает дрейф конфигов между профилями.

### L43. eu-юрисдикция в categories.yaml не имеет fullname_exclusions — потенциальный future bug
- **Модуль:** Text2SQL: YAML/конфиги
- **Категория:** maintainability | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `config/pii/categories.yaml:150-220`
- **Проблема:** Юрисдикция `eu` не содержит ключа `fullname_exclusions`. `_coerce_str_list(..., allow_empty=True)` возвращает `[]`. Сейчас это безопасно: EU-правило `full_name` не имеет `use_fullname_exclusions: true`. Но если кто-то добавит `use_fullname_exclusions: true` в EU-правило, `_is_likely_fullname` будет вызвана с пустым `frozenset()` — функция вернёт `True` для любого матча (нет исключений → маскировать всё). Это семантически корректно (нет географических исключений для EU), но не очевидно. Важнее: `_is_likely_fullname` в `_pii.py` явно вызывает `_ru_fullname_exclusions()` — функцию, привязанную к понятию «ru», — а не обобщённую `get_jurisdiction_fullname_exclusions(active_jur)`. Функция жёстко использует `_load_active_pii_jurisdiction()` без параметра, т.е. работает на активной юрисдикции. Если активна `eu`, `_is_likely_fullname` возьмёт пустой `eu.fullname_exclusions`, что корректно — но имя `_ru_fullname_exclusions` вводит в заблуждение.
- **Влияние:** Misleading naming; неявная связь между юрисдикцией и функцией маскировки.
- **Исправление:** Переименовать `_ru_fullname_exclusions` в `_active_fullname_exclusions`. Добавить заглушку `fullname_exclusions: []` в eu-юрисдикцию для явности.
- **Что даст:** Читаемость; устранение скрытой зависимости от имени юрисдикции в Python-коде.

### L44. Небезопасное использование `locals()` в get_context для проверки переменной
- **Модуль:** Memory / RAG
- **Категория:** maintainability | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `memory/rag_memory.py:509`
- **Проблема:** `trimmed_parts if 'trimmed_parts' in locals() else context_parts` — проверка существования переменной через `locals()`. Это хрупкая конструкция: переменная `trimmed_parts` может существовать из предыдущей итерации или вовсе не быть определена, если первый `if` (строка 504) не выполнился. Python не гарантирует, что `locals()` внутри условного выражения отражает текущее состояние фрейма во всех реализациях.
- **Влияние:** Потенциально некорректный выбор списка секций при обрезке контекста. В CPython работает, но это непредсказуемый код.
- **Исправление:** Инициализировать `trimmed_parts = context_parts` перед первым if, убрав проверку через `locals()`.
- **Что даст:** Чёткий, предсказуемый поток управления при обрезке контекста.

### L45. protagonist_initializer_tool содержит отладочные print вместо logger
- **Модуль:** Storybook: инструменты генерации
- **Категория:** maintainability | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/protagonist_initializer.py:62-91`
- **Проблема:** Несколько `print(f'[DEBUG] protagonist_initializer: ...')` разбросаны по production-коду (строки 62, 66-68, 73, 80, 83, 90-91). Это отладочный код, который попал в продакшн и не использует стандартный `logger` модуля.
- **Влияние:** Засоряет stdout при использовании в агентском контексте, может нарушить парсинг вывода агента, который ищет пути к PNG-файлам в `_parse_output_path`.
- **Исправление:** Заменить все `print(f'[DEBUG] ...')` на `logger.debug(...)` или убрать.
- **Что даст:** Чистый вывод агента, нормальное логирование.

### L46. DEBUG print() вместо logger в production-коде veo_tool
- **Модуль:** Storybook: видео/shots-генераторы
- **Категория:** maintainability | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/storybook/video_generator_veo_tool.py:98,101,157,158,162,163,169,180,196,336`
- **Проблема:** В video_generator_veo_tool.py повсеместно используются `print(f'DEBUG: ...')` вместо logger. Все остальные инструменты используют стандартный logging. Строка 336 логирует полный объект operation.response через `logger.info(f'DEBUG: Operation Response...')` — потенциальная утечка чувствительных данных в лог.
- **Влияние:** Debug-вывод попадает в stdout в production, обходя уровни логирования и обработчики. Возможна утечка URI видео/токенов в логах.
- **Исправление:** Заменить print на logger.debug/logger.info. Удалить или понизить до logger.debug строку с полным operation.response.
- **Что даст:** Единообразное логирование, контролируемые уровни, нет утечки данных.

### L47. muni_ru в similarity_thresholds.yaml — идентичный default: профиль существует, но не настроен
- **Модуль:** Text2SQL: YAML/конфиги
- **Категория:** maintainability | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `config/text_to_sql/similarity_thresholds.yaml:46-51`
- **Проблема:** Профиль `muni_ru` — точная копия `default`. YAML-комментарий честно говорит «зарезервирован для тюнинга» и это намеренная заглушка. Проблема в том, что код в `_yaml_config_loader` при `profile_extra` создаёт отдельную кэш-запись для `muni_ru`, тратя память и время на парсинг того же файла. Более серьёзно: если разработчик включит `TEXT_TO_SQL_SIMILARITY_PROFILE=muni_ru` в prod-конфиге «для перспективы», пороги останутся дефолтными, и никакого сигнала об этом не будет.
- **Влияние:** Silent no-op при активации muni_ru профиля, потенциально создаёт иллюзию доменной настройки.
- **Исправление:** Либо удалить профиль muni_ru до момента реального тюнинга, либо добавить в loader предупреждение при использовании профиля-копии. Как минимум — убрать `profile_extra` из loader'а (см. предыдущую находку), что снизит накладные расходы.
- **Что даст:** Прозрачность намерений; устранение потенциального заблуждения «я активировал muni_ru — должны быть другие пороги».

### L48. _compute_type_hint_bonus вызывает load_type_categories_config() на каждую колонку
- **Модуль:** Text2SQL: schema-linking + RAG
- **Категория:** performance | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/schema_linking/heuristic_linker.py:96`
- **Проблема:** `_compute_type_hint_bonus` вызывается из `_score_columns_for_name` в цикле по колонкам таблицы. Внутри метода на строке 96-97 каждый раз вызывается `load_type_categories_config()`. Хотя сам loader кэшируется, неизбежен overhead dict-lookup + function call + thread-safe check на каждую колонку. При схеме с сотнями колонок это O(N_cols) обращений к кэшу вместо одного.
- **Влияние:** Незначительное замедление при больших схемах; основная проблема — разрыв уровней: config-загрузка должна происходить на уровне вызова метода, а не внутри inner-loop helper.
- **Исправление:** Вынести вызов `load_type_categories_config()` на уровень `_score_columns_for_name` (один вызов на таблицу) и передавать `_type_cfg` в `_compute_type_hint_bonus` параметром.
- **Что даст:** Чистота архитектуры, снижение накладных расходов, более тестируемый код.

### L49. profile_extra в similarity_thresholds_config вызывает лишний re-parse yaml при смене профиля
- **Модуль:** Text2SQL: YAML/конфиги
- **Категория:** performance | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/similarity_thresholds_config.py:186-195`
- **Проблема:** `_loader` создан с `profile_extra=lambda: os.getenv(_ENV_PROFILE_VAR, _DEFAULT_PROFILE)`. Это означает, что YamlConfigLoader кэширует `SimilarityThresholdsConfig` (содержащий ВСЕ профили) по ключу `(path, profile_name)`. При смене профиля через env будет заново читаться и парситься тот же yaml-файл. Для `safety_config` это паттерн оправдан — там `parser` реально зависит от профиля. Здесь `parser` всегда возвращает объект со всеми профилями, `profile_extra` не нужен.
- **Влияние:** Лишнее чтение диска при первом вызове с новым профилем. Не критично в проде (смена профиля — startup-событие), но создаёт копии одного и того же объекта в кэше.
- **Исправление:** Убрать `profile_extra` из конфигурации `_loader`. Выбор профиля уже делается в `load_similarity_thresholds()` через `config.get_profile(resolve_active_profile())`.
- **Что даст:** Единое кэшированное представление yaml; соответствие паттерну других profile-aware конфигов (significance, column_aliases).

### L50. state_changes список в AgentCircuitBreaker растёт неограниченно
- **Модуль:** Workflow Engine: resilience/orchestration/intelligence
- **Категория:** performance | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `workflow/resilience/circuit_breaker.py:134`
- **Проблема:** `self.state_changes` — список, в который при каждой смене состояния добавляется запись (строка 134). Размер не ограничен. При нестабильном агенте, который многократно переходит CLOSED->OPEN->HALF_OPEN->CLOSED, список растёт бесконечно. Аналогично — `decision_history` в `DecisionEngine` ограничен 10 записями на шаг (строка 412), но `retry_history` в `AdaptiveRetryEngine` и `execution_history` в `LoopDetector` ограничены только явными cleanup-вызовами, которые не вызываются автоматически.
- **Влияние:** Постепенная утечка памяти в долгоработающих процессах. При 1 смене состояния в секунду за 24 часа: ~86k записей в `state_changes`.
- **Исправление:** Добавить `maxlen` ограничение: `self.state_changes = collections.deque(maxlen=100)`. Для retry_history добавить автоматическую очистку по времени в `execute_with_retry`.
- **Что даст:** Предсказуемое потребление памяти в production.

### L51. История SQL не ограничена при загрузке в session_state
- **Модуль:** Streamlit UI
- **Категория:** performance | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `streamlit_app/pages/05_Text_to_SQL.py:231`
- **Проблема:** В `init_session_state()` история загружается с `max_entries=100`, однако при вызовах `save_to_history()` история в `session_state` не обрезается — `st.session_state.sql_history.append(history_entry)` без лимита (строка 1392). За долгую сессию словарь растёт неограниченно.
- **Влияние:** Утечка памяти в session_state при активном использовании (сотни запросов в сессии).
- **Исправление:** После append добавить `st.session_state.sql_history = st.session_state.sql_history[-100:]`.
- **Что даст:** Ограниченное потребление памяти сессии.

### L52. Metrics._cleanup_labels_index: O(n) поиск при каждом trimming — квадратичная сложность
- **Модуль:** Workflow Engine: resilience/orchestration/intelligence
- **Категория:** performance | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `workflow/monitoring/metrics.py:87`
- **Проблема:** При достижении лимита 10000 записей удаляются 5000 старых (строки 76-81). Для каждой удалённой записи `_cleanup_labels_index` (строки 87-97) вызывает `list.remove(value)` — это O(n) поиск в списке. При удалении 5000 записей и, допустим, 100 различных label-комбинациях каждая с ~50 записями, это 5000 * 50 = 250000 сравнений объектов.
- **Влияние:** Заметная пауза при трimmers под нагрузкой (интенсивное логирование шагов). Не критично, но на high-traffic workflow с мелкими шагами может тормозить метрики.
- **Исправление:** Использовать deque с maxlen или хранить индекс по labels как WeakRef-ссылки. Более просто: вместо list.remove использовать set или deque.
- **Что даст:** Устранение O(n²) при сборке метрик.

### L53. Небезопасный fallback: весь context становится linked_entities при отсутствии ключа
- **Модуль:** Text2SQL: генерация SQL
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. high→low
- **Где:** `custom_tools/text_to_sql/sql_builder.py:273`
- **Проблема:** В `_get_linked_entities` паттерн `context.get("linked_entities", context)` возвращает весь словарь context как linked_entities, если ключ `linked_entities` отсутствует. Тот же паттерн в sql_generator.py:312. Если вызывающий код передаёт контекст без явного ключа `linked_entities` (например, сырой JSON от LLM), функции `metrics`, `dimensions`, `filters` и `joins` будут читаться из верхнего уровня контекста, который может содержать произвольные поля из user-запроса.
- **Влияние:** Злоумышленник может добавить поля `metrics`, `dimensions`, `filters` в верхний уровень контекста и управлять генерацией SQL, обходя схему linked_entities. В лучшем случае — неожиданный SQL, в худшем — обход фильтров безопасности через вставку произвольных table/column имён в структурный путь.
- **Исправление:** Вернуть `{}` если ключ `linked_entities` отсутствует, а не весь context. Шаблон: `linked = context.get("linked_entities"); return linked if isinstance(linked, dict) else {}`.
- **Что даст:** Устраняет неявный fallback, который позволяет произвольным полям context влиять на построение SQL.
- **Заметка верификатора:** Фоллбэк context.get("linked_entities", context) действительно возвращает весь context при отсутствии ключа (sql_builder.py:273, sql_generator.py:312). Но эксплойт-сценарий не подтверждается. (1) context приходит из доверенного пайплайна schema-linking (schema_linker/llm_linker/linking_orchestrator), который ВСЕГДА эмитит ключ {"linked_entities": {...}} (schema_linker.py:109/120, llm_linker.py:209/215, _schema_linking_api.py:110); фоллбэк в нормальном потоке недостижим. (2) Ключ joins и так читается с верхнего уровня context.get("joins", []) на строке 53 — то есть top-level dict это и есть доверенный envelope, а не сырой user-JSON. (3) Даже если бы top-level поля читались, итоговый SQL проходит и SQLSafetyValidator (_apply_safety_validation, sql_generator.py:197/289), и schema-валидацию (sql_builder.py:256-266), а table/column оборачиваются как идентификаторы через quote_identifier/quote_single_identifier — это не конкатенация в сырой SQL, поэтому "обход фильтров через вставку произвольных table/column" как инъекция недостижим. Реальная посылка "злоумышленник контролирует верхний уровень context" не обоснована. Это defensive-coding смелл (лучше вернуть {} при отсутствии ключа), а не прод-эксплойт. High не оправдан.

### L54. MD5 в _hash_key — коллизионно небезопасный алгоритм для integrity-check
- **Модуль:** Text2SQL: schema-linking + RAG
- **Категория:** security | **Уверенность:** high | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/rag/embedding_utils.py:109`
- **Проблема:** `hashlib.md5(payload.encode('utf-8')).hexdigest()` используется как ключ кэша для объектов (включая содержимое RAG-документов). MD5 не является криптографически стойким — при целенаправленной атаке возможна коллизия, из-за которой новый документ получит тот же ключ, что и существующий, и будет «невидим» системе. Кроме того, md5 запрещён в ряде FIPS-окружений.
- **Влияние:** При злонамеренно подобранном содержимом sqlrag/*.md документ с другим контентом может замаскироваться под уже проиндексированный (cache collision). В FIPS-окружениях вызов упадёт с RuntimeError.
- **Исправление:** Заменить на `hashlib.sha256(payload.encode('utf-8')).hexdigest()` — уже используется во всех остальных местах модуля.
- **Что даст:** Единообразие хэш-алгоритмов, устойчивость к коллизиям, совместимость с FIPS.

### L55. Legacy-маршрут (`_validate_legacy`) не маскирует quoted-идентификаторы перед regex-проверкой forbidden_functions
- **Модуль:** Text2SQL: валидаторы безопасности
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. high→low
- **Где:** `custom_tools/text_to_sql/validators/safety.py:1239-1250`
- **Проблема:** В `_validate_legacy` переменная `upper_sql` формируется из `masked_sql.upper()` — после маскировки строковых литералов, но без вызова `_mask_identifiers_via_lex`. Метод `_validate_with_sqlglot` (строка 1306) вызывает `_mask_identifiers_via_lex` специально, чтобы quoted-идентификаторы вида `"current_user"` или `` `current_user` `` не ловились как запрещённые функции (ложные позитивы). В legacy-пути этого нет, однако последствие обратное: в стандартных диалектах, где двойные кавычки — идентификаторы, `_mask_string_literals` их НЕ маскирует (содержимое оставляется «как есть», строка 287). Значит `SELECT "current_user"` в legacy-пути вызовет ложный FORBIDDEN_FUNCTION, а хитрый вариант с backslash-экранированием или unicode может дать false-negative в зависимости от версии regex. Кроме того, в MySQL-режиме (dq_is_string=True) идентификаторы маскируются пробелами, что может скрыть реальные атаки внутри них.
- **Влияние:** При `USE_SQLGLOT=0` в production (с `SQL_SAFETY_ALLOW_LEGACY=1`) regex-защита forbidden_functions работает не так, как задумано: ложные позитивы на легитимные имена колонок и потенциальные false-negative обходы.
- **Исправление:** В `_validate_legacy` применять `_mask_identifiers_via_lex` перед `check_forbidden_functions` так же, как в `_validate_with_sqlglot`. Либо явно задокументировать и ограничить, что legacy-путь не поддерживает корректную обработку quoted-идентификаторов.
- **Что даст:** Унифицирует поведение regex-проверки между legacy и sqlglot маршрутами; устраняет расхождение в семантике.
- **Заметка верификатора:** Фактические утверждения о коде верны: _validate_legacy (safety.py:1239) использует masked_sql.upper() БЕЗ _mask_identifiers_via_lex, тогда как _validate_with_sqlglot (safety.py:1306) этот вызов делает. Воспроизвёл ложный позитив: SELECT "current_user" в профиле extended/strict + USE_SQLGLOT=0 + не-MySQL диалект даёт FORBIDDEN_FUNCTION на легитимном quoted-идентификаторе (regex \bCURRENT_USER\b совпадает с незамаскированным содержимым identifier). Однако severity high завышена: (1) реально демонстрируемый дефект — это FALSE POSITIVE (over-blocking легитимного запроса), т.е. проблема корректности/доступности, а НЕ обход безопасности; (2) заявленные false-negative обходы спекулятивны и частично неверны — в MySQL «...» это строковый литерал (маскировать его корректно), а имя функции внутри строкового литерала не исполняется как функция, так что bypass-а нет; backslash/unicode обход дан как «в зависимости от версии regex» без подтверждения; (3) узкая область: нужен USE_SQLGLOT=0 (дефолт=1, dialects.py:245) И профиль extended/strict (у default forbidden_functions пуст, legacy с default сюда не доходит) И production-override SQL_SAFETY_ALLOW_LEGACY=1, который сам логирует «regex-only validation is unsafe» (safety.py:1210-1212). Итого: реальный изъян — ложный позитив в deprecated, явно не рекомендованном, не-дефолтном пути. Понижаю до low.

### L56. Path traversal: project_id напрямую используется как имя директории без санитизации при load_project
- **Модуль:** StoryBookManager (десктоп-приложение)
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. high→low
- **Где:** `StoryBookManager/core/project_manager.py:297`
- **Проблема:** В `load_project(project_id)` строка `project_path = self.projects_dir / project_id` конкатенирует project_id к базовой директории без проверки. Аналогично в `delete_project`, `backup_project`, `get_project_files`. Если project_id содержит `../../`, атакующий (или вредоносный JSON в ui-форме) может направить операции на произвольный путь файловой системы. `generate_project_id` нормализует только при создании, но `load_project` принимает внешний строковый аргумент напрямую.
- **Влияние:** Чтение, перезапись или удаление файлов за пределами директории проектов при подстановке вредоносного project_id через UI или восстановленный checkpoint.
- **Исправление:** Добавить проверку `project_path.resolve().relative_to(self.projects_dir.resolve())` перед любой файловой операцией и отклонять project_id, содержащие `..` или `/`.
- **Что даст:** Устраняет path-traversal при манипуляции project_id.
- **Заметка верификатора:** Сырая конкатенация self.projects_dir / project_id действительно присутствует в load_project (project_manager.py:300), delete_project (393), backup_project (313), get_project_files (417) без проверки на '../'. Однако в этом desktop-приложении нет источника недоверенного project_id: значения приходят из Project-объектов, созданных list_projects() через iterdir() по реальным директориям, либо из create_project(), где generate_project_id() (стр.207) нормализует имя regex'ом [^a-zA-Z0-9]+ -> '_'. UI (project_panel.py) выбирает проект из дерева, построенного по существующим проектам; диалог создания дополнительно фильтрует ввод (validate_project_id, regex стр.948). Тезис про 'восстановленный checkpoint' не подтверждается: workflow_id генерируется как sbm_{project_id}_uuid, project_id туда подаётся уже из current_project. Реально достижимого вектора с '../../' нет. Это легитимный defensive-hardening недочёт, но не достижимая прод-уязвимость в однопользовательском GUI — severity 'high' завышена.

### L57. DSN (строка подключения с паролем) пишется в глобальный os.environ из UI-потока
- **Модуль:** Streamlit UI
- **Категория:** security | **Уверенность:** high | **Верификация:** скоррект. high→low
- **Где:** `streamlit_app/pages/05_Text_to_SQL.py:531`
- **Проблема:** `os.environ["DB_DSN"] = st.session_state.selected_dsn` в `run_agents_text_to_sql()` и `run_yaml_text_to_sql()` (строки 531, 695) устанавливает DSN, включая пароль, в переменную окружения процесса. Это происходит в UI-потоке Streamlit, который является общим для всех пользователей.
- **Влияние:** В многопользовательском окружении DSN одного пользователя может быть прочитан фоновым потоком или другим параллельным запросом другого пользователя через `os.environ.get('DB_DSN')`. Пароль БД попадает в переменные окружения процесса, откуда читается `/proc/self/environ` на Linux.
- **Исправление:** Передавать DSN только через аргументы функции/потока или thread-local контекст (как уже сделано через `run_id_context` в том же файле). Обе функции помечены DEPRECATED — убрать их целиком.
- **Что даст:** Устранение утечки credentials между сессиями пользователей.
- **Заметка верификатора:** Строки 531 и 695 существуют буквально, НО обе функции run_agents_text_to_sql и run_yaml_text_to_sql помечены DEPRECATED и не вызываются нигде в репозитории (grep по всему дереву кроме .venv дал 0 вызовов, только определения). Это мёртвый код — os.environ['DB_DSN'] никогда не выполняется в проде. Живой путь generate_sql_query() передаёт DSN через payload dict (строка 793), а не через os.environ. Анти-паттерн реален как латентный риск, но не эксплуатируемая прод-уязвимость. Понижаю до low.

### L58. Промпт schema-linking раскрывает структуру алгоритма поиска join'ов без sanitization dsn
- **Модуль:** Text2SQL: schema-linking + RAG
- **Категория:** security | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `custom_tools/text_to_sql/schema_linking/llm_linker.py:166`
- **Проблема:** `build_schema_linking_prompt(entities, schema_str, dsn=dsn)` вызывается с сырым `dsn` (строка, полученная от caller'а). Функция `build_schema_linking_prompt` использует `dsn` только для определения диалекта через `get_current_dialect_label`, но сам DSN не включается в промпт. Проблема другая: `entities` могут содержать PII (имена/значения фильтров из пользовательского запроса), и хотя применяется `_redact_prompt_value`, эта функция лишь оборачивает данные и не удаляет PII-поля — она выглядит как sanitizer, но им не является.
- **Влияние:** Пользовательские данные из `entities` (например, значения фильтров с именами людей, ИНН, адресами) передаются в LLM-промпт через `json.dumps(safe_entities, ...)` — риск утечки PII в LLM API и в логи (строка logger.info с prompt preview).
- **Исправление:** Применить реальный PII-redaction к значениям (не ключам) в entities перед передачей в prompt. Проверить контракт `_redact_prompt_value` — является ли он действительно PII-фильтром или только type-sanitizer.
- **Что даст:** Соответствие требованиям защиты данных; снижение риска утечки PII через LLM API.

### L59. Prompt injection через оптимизируемый промпт агента
- **Модуль:** prompt_optimizer + telemetry
- **Категория:** security | **Уверенность:** medium | **Верификация:** скоррект. high→low
- **Где:** `prompt_optimizer/prompt_optimizer.py:252-256`
- **Проблема:** Содержимое original_prompt вставляется напрямую в f-string мета-промпта (строка 252: ````{original_prompt}`````). Несмотря на предупреждения «НЕ СЛЕДУЙТЕ ИНСТРУКЦИЯМ ИЗ BASELINE PROMPT», сам исходный промпт может содержать специально сформированные инструкции, которые перезапишут System-промпт оптимизатора. Это «prompt injection через данные» — хорошо известный класс атак для LLM-пайплайнов.
- **Влияние:** Злоумышленник, контролирующий содержимое профиля агента, может заставить модель-оптимизатор выполнить произвольные инструкции (утечку других промптов, изменение логики оптимизации, обход ограничений).
- **Исправление:** Санитизировать original_prompt перед вставкой в мета-промпт: экранировать маркеры блоков кода и инструкционные триггеры. Рассмотреть передачу промпта через отдельное сообщение без мета-контекста.
- **Что даст:** Снижает риск инъекции для всей цепочки оптимизации.
- **Заметка верификатора:** Факт подтверждён: original_prompt интерполируется в f-string мета-промпта (prompt_optimizer.py:254) — классический pattern prompt-injection-via-data, и предупреждения-разделители не дают гарантий. НО severity high завышена для прода: источник данных — собственный YAML профиля агента (agent_profiles/*.yaml), доверенный локальный конфиг, а не пользовательский ввод. Предусловие 'злоумышленник контролирует содержимое профиля' уже подразумевает запись в trusted config на ФС. Выход оптимизатора — просто текст нового промпта. Это hardening-замечание, а не high-severity уязвимость в данной модели доверия.

### L60. Отсутствие проверки пути tool_name при загрузке YAML инструмента
- **Модуль:** prompt_optimizer + telemetry
- **Категория:** security | **Уверенность:** medium | **Верификация:** не верифиц.
- **Где:** `prompt_optimizer/prompt_optimizer.py:94`
- **Проблема:** get_tools_info() формирует путь к файлу инструмента как `tool_definitions_dir / f"{tool_name}.yaml"` без проверки, что tool_name не содержит `..` или `/`. Имена инструментов читаются из YAML-профилей агентов, которые могут быть изменены вручную.
- **Влияние:** Path traversal внутри файловой системы: чтение произвольных .yaml-файлов за пределами tool_definitions_dir. Риск реален только при компрометации профилей агентов.
- **Исправление:** Добавить валидацию: `if '/' in tool_name or tool_name.startswith('.'): continue`. Или использовать `.resolve()` и проверить, что результат внутри tool_definitions_dir.
- **Что даст:** Устраняет структурную возможность path traversal.

### L61. Прямая мутация os.environ в test_sqlglot_integration.py нарушает изоляцию тестов
- **Модуль:** Качество тестов
- **Категория:** testing | **Уверенность:** high | **Верификация:** скоррект. high→low
- **Где:** `tests/test_sqlglot_integration.py:9`
- **Проблема:** На уровне модуля: `os.environ["USE_SQLGLOT"] = "1"`. В классе `TestSQLGlotDisabled.setup_method` выставляется `os.environ["USE_SQLGLOT"] = "0"`, а `teardown_method` возвращает `"1"`. Метод `test_strict_mode_requires_sqlglot` (строки 213–222) дополнительно использует `monkeypatch.setenv("USE_SQLGLOT", "1")` поверх уже выставленного `"0"` из setup_method. После завершения теста monkeypatch восстанавливает значение до `"0"` (то, что было ДО monkeypatch.setenv), а затем teardown_method ставит `"1"`. Это корректно в pytest, но только потому что monkeypatch завершается до teardown. Если порядок изменится или тест упадёт посередине — возможно попадание в следующий тест с неожиданным значением `USE_SQLGLOT`. Также глобальная мутация `os.environ` на уровне модуля (строка 9) может влиять на другие параллельно импортируемые модули при collect-фазе.
- **Влияние:** Загрязнение env между тестами: тесты, запускаемые после этого модуля при --forked=no (обычный режим), могут получить неверный `USE_SQLGLOT`, особенно если teardown не выполнился при xfail/skip. Фиксирует состояние снаружи — нарушает принцип изоляции.
- **Исправление:** Использовать monkeypatch вместо прямой мутации os.environ. Убрать module-level `os.environ["USE_SQLGLOT"] = "1"` — заменить на `os.environ.setdefault(...)` или autouse-фикстуру. setup_method/teardown_method заменить на pytest-фикстуру с monkeypatch.
- **Что даст:** Гарантированная изоляция между тестами; устранение потенциальной причины flaky-тестов.
- **Заметка верификатора:** Подтверждено фактически, но severity завышена. is_sqlglot_enabled() в dialects.py:243-245 читает os.getenv('USE_SQLGLOT','1') живьём, и ДЕФОЛТ = '1'. Module-level os.environ['USE_SQLGLOT']='1' (строка 9) выставляет значение, равное прод-дефолту, поэтому 'загрязнение' соседних модулей сводится к установке того же значения, что уже подразумевается по умолчанию. teardown_method в pytest выполняется и при падении ассерта (пропускается только если упал setup_method), и восстанавливает '1' — снова дефолт. Сам же ревьюер пишет 'Это корректно в pytest'. monkeypatch в test_strict_mode_requires_sqlglot восстанавливается до teardown — ок. conftest.py не сбрасывает USE_SQLGLOT, но т.к. утечка всегда даёт дефолтное '1', реального межтестового искажения прод-режима нет. Это стилевой issue изоляции, не high.

### L62. test_schema_linker_improvements.py: прямая мутация os.environ без cleanup в тест-методе
- **Модуль:** Качество тестов
- **Категория:** testing | **Уверенность:** high | **Верификация:** скоррект. high→low
- **Где:** `tests/test_schema_linker_improvements.py:228`
- **Проблема:** В методе `test_cache_info_includes_params` напрямую устанавливаются `os.environ["SCHEMA_LINKING_USE_LLM"] = "1"` и `os.environ["SCHEMA_TABLE_CANDIDATES_K"] = "3"`/`"7"` (строки 228–252). Cleanup только через `os.environ.pop(...)` в конце тела метода (строки 259–260) — это не выполнится, если тест упадёт до cleanup. `DB_DSN` устанавливается через `os.environ.setdefault` (строка 230) — не очищается вообще, если его не было до запуска теста.
- **Влияние:** При любом падении теста до строк очистки следующие тесты получат `SCHEMA_LINKING_USE_LLM=1`, что меняет поведение schema-linking. `DB_DSN` может оставаться выставленным из теста, влияя на тесты, проверяющие поведение при отсутствии DB_DSN.
- **Исправление:** Перевести на monkeypatch.setenv / monkeypatch.delenv. Убрать ручную логику cleanup.
- **Что даст:** Стабильный порядок выполнения, устранение flaky-цепочек при неожиданных падениях.
- **Заметка верификатора:** Подтверждено как нарушение изоляции (строки 228-260): единственный тест, использующий raw os.environ[...] вместо monkeypatch (все остальные 20+ упоминаний SCHEMA_LINKING_USE_LLM используют monkeypatch.setenv с авто-restore). DB_DSN через setdefault (строка 230) не чистится даже при успехе и реально утечёт, если DB_DSN не был задан. НО: severity high завышена — это чисто тестовая гигиена, прод не затронут; ассерты теста прямолинейны и маловероятно падают между set и pop; а зависимые тесты защищаются сами (monkeypatch.delenv('DB_DSN', raising=False) в test_text_to_sql_core_contracts.py, _dialects_utils_refactor.py и др. — см. множество вхождений). Реальный, но низкий риск.
