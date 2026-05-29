# Глава 19: Веб‑интерфейс и Streamlit API

Стабильный слой‑посредник между UI и ядром: запускает агентов и workflow, управляет процессами и возвращает статусы по типизированным контрактам.

## Зачем
- Разделение ответственности: UI не знает внутренностей агентов.
- Стабильные контракты данных (dataclasses): совместимость при рефакторинге.
- Отзывчивость UI: выполнение в отдельных процессах.

## Компоненты API
- AgentManager (`agent_streamlit_api.py`): запуск одиночных агентов/команд.
- WorkflowManager (`workflow/streamlit_api.py`): запуск YAML‑пайплайнов.
- MemoryRAGManager (`memory/streamlit_api.py`): поиск/статусы памяти.
- DBPluginManager (`db_plugins/streamlit_api.py`): управление подключениями БД.

## Пример: запуск агента
```python
from agent_streamlit_api import AgentManager
mgr = AgentManager()
run_id = mgr.run_agent("DataAnalyst", task)
```
Под капотом:
```python
def run_agent(self, agent_id_or_profile: str, task: str) -> str:
    run_id = f"run-{uuid.uuid4().hex[:16]}"
    proc = Process(target=_agent_process_entry, args=(run_id, ...))
    proc.start()
    self.active_runs[run_id] = {"status": "running", "pid": proc.pid}
    return run_id
```

## Контракты статусов
```python
@dataclass
class AgentRunStatus:
    run_id: str
    agent_name: str
    status: str  # queued|running|completed|failed|cancelled
    task: str = ""
    start_time: datetime | None = None
```
UI получает предсказуемую структуру, независимо от изменений внутри менеджера.

## Workflow из UI
```python
from workflow.streamlit_api import WorkflowManager
wm = WorkflowManager()
run_id = wm.start_workflow("simple_research", parameters={"topic": topic})
```

## Связка с остальными API
- `memory/streamlit_api.py`: поиск по RAG без знания векторного хранилища.
- `db_plugins/streamlit_api.py`: list/validate/test DSN.

## Вывод
Streamlit API обеспечивает чистые контракты, изоляцию и масштабируемость интерфейса: UI обращается к менеджерам, а те — к ядру системы, сохраняя отзывчивость и устойчивость.
