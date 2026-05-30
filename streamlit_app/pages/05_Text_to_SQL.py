"""
Страница Text-to-SQL генерации и выполнения
==========================================
"""

import streamlit as st
import sys
from pathlib import Path
import json
from datetime import datetime
import pandas as pd
import os
from typing import Any
import asyncio
import threading
import uuid
import time
import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_DSN_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "key",
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
}
_SENSITIVE_PAYLOAD_KEYS = {
    "connection_string",
    "database_dsn",
    "database_url",
    "db_dsn",
    "db_url",
    "dsn",
}
_SENSITIVE_SCALAR_KEYS = set(_SENSITIVE_DSN_QUERY_KEYS)
_URL_LIKE_PAYLOAD_KEYS = {"url"}
_DSN_TEXT_RE = re.compile(r"(?P<dsn>[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+)")
_SENSITIVE_TEXT_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>\b(?:access_token|api_key|apikey|auth|key|password|passwd|pwd|secret|token)\b\s*[:=]\s*)"
    r"(?P<secret>[^\s,;]+)",
    re.IGNORECASE,
)


def _dsn_fingerprint(dsn: str) -> str:
    return hashlib.sha256(dsn.encode("utf-8")).hexdigest()[:16]


def _redact_dsn(dsn: Any) -> Any:
    if not isinstance(dsn, str):
        return dsn
    try:
        parts = urlsplit(dsn)
        if not parts.scheme:
            return "<redacted>"
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, hostinfo = netloc.rsplit("@", 1)
            username = userinfo.split(":", 1)[0]
            netloc = f"{username}:***@{hostinfo}" if username else f"***@{hostinfo}"
        query_items = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            query_items.append((key, "***" if key.lower() in _SENSITIVE_DSN_QUERY_KEYS else value))
        return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query_items, doseq=True), parts.fragment))
    except Exception:
        return "<redacted>"


def _looks_like_dsn(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        return bool(parts.scheme and (parts.netloc or parts.scheme in {"sqlite", "duckdb"}))
    except Exception:
        return False


def _redact_query_string(value: str) -> str:
    try:
        items = parse_qsl(value, keep_blank_values=True)
    except Exception:
        return value
    if not items or not any(key.lower() in _SENSITIVE_DSN_QUERY_KEYS for key, _ in items):
        return value
    return urlencode(
        [(key, "***" if key.lower() in _SENSITIVE_DSN_QUERY_KEYS else item) for key, item in items],
        doseq=True,
    )


def _redact_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        candidate = match.group("dsn")
        return _redact_dsn(candidate) if _looks_like_dsn(candidate) else candidate

    redacted = _DSN_TEXT_RE.sub(replace, value)
    redacted = _redact_query_string(redacted)
    return _SENSITIVE_TEXT_ASSIGNMENT_RE.sub(r"\g<prefix>***", redacted)


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text in _SENSITIVE_PAYLOAD_KEYS or (key_text in _URL_LIKE_PAYLOAD_KEYS and _looks_like_dsn(item)):
                redacted[key] = _redact_dsn(item)
                if isinstance(item, str):
                    redacted.setdefault(f"{key}_fingerprint", _dsn_fingerprint(item))
            elif key_text in _SENSITIVE_SCALAR_KEYS:
                redacted[key] = "<redacted>"
            elif key_text == "query" and isinstance(item, str):
                redacted[key] = _redact_query_string(item)
            else:
                redacted[key] = _redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# Реестр фоновых задач (персистентный между перезапусками скрипта)
@st.cache_resource(show_spinner=False)
def get_job_registry():
    return {}

@st.cache_resource(show_spinner=False)
def get_job_registry_lock():
    import threading
    return threading.Lock()

# Реестр подключений (connection_id -> DSN), чтобы не светить DSN в URL
@st.cache_resource(show_spinner=False)
def get_connection_registry():
    return {}

def _generate_connection_id(dsn: str) -> str:
    """Opaque identifier for passing an active connection through URL state."""
    return f"conn-{uuid.uuid4().hex}"


def _validate_text_to_sql_options(max_rows: Any, safety_level: str) -> tuple[int, str]:
    if isinstance(max_rows, bool):
        raise ValueError("max_rows должен быть целым числом")
    if isinstance(max_rows, int):
        normalized_max_rows = max_rows
    elif isinstance(max_rows, float):
        if not max_rows.is_integer():
            raise ValueError("max_rows должен быть целым числом")
        normalized_max_rows = int(max_rows)
    elif isinstance(max_rows, str):
        normalized = max_rows.strip()
        if not normalized.isdigit():
            raise ValueError("max_rows должен быть целым числом")
        normalized_max_rows = int(normalized)
    else:
        raise ValueError("max_rows должен быть целым числом")
    max_rows = normalized_max_rows
    if max_rows < 1 or max_rows > 10000:
        raise ValueError("max_rows должен быть от 1 до 10000")
    safety_level = str(safety_level or "strict").strip().lower()
    if safety_level != "strict":
        raise ValueError("Поддерживается только safety_level=strict")
    return max_rows, safety_level


def main():
    st.set_page_config(
        page_title="Text-to-SQL - MultiAgent System",
        page_icon="🔍",
        layout="wide"
    )
    
    st.title("🔍 Text-to-SQL генерация и выполнение")
    st.markdown("---")
    
    # Инициализация состояния
    init_session_state()
    
    # Главные вкладки
    tab1, tab2, tab3, tab4 = st.tabs(["🔍 Генерация SQL", "🔌 Подключения", "📊 Схема БД", "📚 История"])
    
    with tab1:
        show_sql_generation()
    
    with tab2:
        show_database_connections()
    
    with tab3:
        show_database_schema()
    
    with tab4:
        show_sql_history()

def init_session_state():
    """Инициализация состояния сессии"""
    if "selected_dsn" not in st.session_state:
        # Восстанавливаем DSN по безопасному connection_id из query params (если есть)
        st.session_state.selected_dsn = ""
        try:
            params = getattr(st, "query_params", None)
            if params is not None:
                qp_conn = params.get("conn")
            else:
                qp_conn = st.query_params.get("conn")
            if isinstance(qp_conn, list):
                qp_conn = qp_conn[0] if qp_conn else ""
            if qp_conn:
                registry = get_connection_registry()
                st.session_state.selected_dsn = registry.get(qp_conn, "")
        except Exception:
            pass
    if "generated_sql" not in st.session_state:
        st.session_state.generated_sql = ""
    if "sql_history" not in st.session_state:
        st.session_state.sql_history = []
        # Попробуем восстановить историю из файла на диске
        try:
            history = _load_history_from_disk(max_entries=100)
            if history:
                st.session_state.sql_history = history
        except Exception:
            pass
    if "current_schema" not in st.session_state:
        st.session_state.current_schema = None
    if "agent_run" not in st.session_state:
        st.session_state.agent_run = None
    if "agent_job_id" not in st.session_state:
        st.session_state.agent_job_id = None

def show_sql_generation():
    """Интерфейс генерации SQL"""
    
    st.markdown("## 🔍 Генерация SQL из естественного языка")
    
    # Проверка подключения к БД
    if not st.session_state.selected_dsn:
        st.warning("⚠️ Выберите подключение к БД на вкладке 'Подключения'")
        return
    
    # Информация о текущем подключении
    with st.container():
        col1, col2, col3 = st.columns(3)
        
        try:
            from db_plugins.streamlit_api import get_db_plugin_manager
            
            db_manager = get_db_plugin_manager()
            validation = db_manager.validate_dsn(st.session_state.selected_dsn)
            
            with col1:
                st.info(f"**База данных:** {validation.detected_scheme or 'Unknown'}")
            
            with col2:
                if validation.detected_schema:
                    st.info(f"**Схема:** {validation.detected_schema}")
                else:
                    st.warning("**Схема:** Не определена")
            
            with col3:
                dialect_info = db_manager.get_dialect_info(validation.detected_scheme or "unknown")
                st.info(f"**Диалект:** {dialect_info.get('dialect_label', 'Unknown')}")
        
        except Exception as e:
            st.error(f"❌ Ошибка анализа подключения: {e}")
    
    st.markdown("---")
    
    # Основной интерфейс генерации
    with st.form("sql_generation_form"):
        st.markdown("### 📝 Описание запроса")
        
        natural_query = st.text_area(
            "🗣️ Опишите что вы хотите получить из базы данных",
            height=150,
            placeholder="""Примеры запросов:
- Покажи всех пользователей, зарегистрированных за последний месяц
- Найди топ-10 самых популярных товаров по продажам
- Посчитай среднюю выручку по месяцам за текущий год
- Покажи клиентов, которые не делали заказов более 3 месяцев""",
            help="Опишите запрос на естественном языке"
        )
        
        # Дополнительные параметры
        with st.expander("⚙️ Дополнительные параметры"):
            col1, col2, col3 = st.columns(3)
            
            with col1:
                max_rows = st.number_input(
                    "📊 Максимум строк",
                    min_value=1,
                    max_value=10000,
                    value=100,
                    help="Лимит строк в результате (применяется через API плагина)"
                )
                
                safety_level = st.selectbox(
                    "🔒 Уровень безопасности",
                    ["strict"],
                    index=0,
                    help="Уровень проверки безопасности SQL"
                )
            
            with col2:
                include_explanation = st.checkbox(
                    "📝 Включить объяснение",
                    value=True,
                    help="Генерировать объяснение к SQL запросу"
                )
                
                validate_schema = st.checkbox(
                    "🔍 Валидация схемы",
                    value=True,
                    help="Проверять соответствие SQL схеме БД"
                )
            
            with col3:
                dry_run_only = st.checkbox(
                    "🧪 Только валидация (dry run)",
                    value=False,
                    help="Не выполнять SQL, только генерация и валидация (только для YAML workflow)"
                )
                
                use_schema_suggestions = st.checkbox(
                    "💡 Использовать подсказки схемы",
                    value=True,
                    help="Использовать информацию о схеме для улучшения генерации"
                )
        
        # Кнопки действий
        col1, col2 = st.columns(2)
        
        with col1:
            generate_clicked = st.form_submit_button("🔍 Генерировать SQL", type="primary")
        
        with col2:
            explain_clicked = st.form_submit_button("💡 Анализ запроса", type="secondary")
        
        # Обработка генерации - всегда через новый workflow pipeline
        if generate_clicked and natural_query:
            generate_sql_query(
                natural_query, max_rows, safety_level, include_explanation,
                validate_schema, dry_run_only, use_schema_suggestions
            )
        
        if explain_clicked and natural_query:
            # Показываем информацию о диалекте и схеме
            explain_natural_query(natural_query)
    
    # Отображение результатов
    if st.session_state.generated_sql:
        show_sql_results()
    if st.session_state.agent_run:
        show_agent_workflow_results()

def _compute_session_id_from_dsn(dsn: str) -> str:
    try:
        from custom_tools.text_to_sql.utils import dsn_to_sanitized_name
        return dsn_to_sanitized_name(dsn)
    except Exception:
        # Фоллбек
        return f"session_{abs(hash(dsn)) % (10**10)}"

def _history_file_path() -> Path:
    try:
        return (project_root / "logs" / "sql_history.jsonl")
    except Exception:
        return Path("logs/sql_history.jsonl")

def _load_history_from_disk(max_entries: int = 100):
    """Загрузить историю SQL из файла JSONL (персистентно между перезапусками)."""
    file_path = _history_file_path()
    if not file_path.exists():
        return []
    entries = []
    migrated = False
    serialized_entries = []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    redacted_rec = _redact_payload(rec)
                    if redacted_rec != rec:
                        migrated = True
                    rec = redacted_rec
                    serialized_entries.append(rec)
                    # Восстановим timestamp
                    ts = rec.get("timestamp")
                    if isinstance(ts, str):
                        try:
                            rec["timestamp"] = datetime.fromisoformat(ts)
                        except Exception:
                            rec["timestamp"] = datetime.now()
                    entries.append(rec)
                except Exception:
                    continue
        if migrated:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                for rec in serialized_entries:
                    writable = dict(rec)
                    ts = writable.get("timestamp")
                    if isinstance(ts, datetime):
                        writable["timestamp"] = ts.isoformat()
                    f.write(json.dumps(writable, ensure_ascii=False) + "\n")
        # Оставим последние max_entries
        return entries[-max_entries:]
    except Exception:
        return []

def _load_schema_from_memory(dsn: str):
    """Пытается загрузить схему БД из памяти (tactical memory), если доступна."""
    try:
        from custom_tools.text_to_sql.utils import dsn_to_sanitized_name
        from memory.tools import get_memory
        session_id = dsn_to_sanitized_name(dsn)
        # Читаем записи, сохраненные индексатором схемы как schema_table
        records = get_memory(
            session_id=session_id,
            cache_kind="schema_table",
            include_historical=False,
            requesting_agent="streamlit_ui"
        )
        if not records:
            return None

        schema = {}
        for rec in records:
            data = rec.get("data") or {}
            # Полная информация таблицы хранится в data.table_info
            table_info = data.get("table_info") or {}
            table_fqn = table_info.get("table_name") or data.get("table_fqn") or data.get("table_name") or ""
            if not table_fqn:
                continue
            short_table = table_fqn.split(".")[-1]

            # Описание берем ИЗ table_info.description (а не из сгенерированного summary в data.description)
            description = table_info.get("description", "")

            # Колонки в памяти — список объектов; преобразуем в dict с полным набором полей
            columns_list = table_info.get("columns") or []
            columns_dict = {}
            for col in columns_list:
                if isinstance(col, dict):
                    name = col.get("name") or col.get("column_name")
                    if not name:
                        continue
                    columns_dict[name] = {
                        "type": col.get("type", ""),
                        "description": col.get("description", ""),
                        "constraint_type": col.get("constraint_type", ""),
                        "references": col.get("references", ""),
                        # Допполя сохраняем, но UI может их не использовать
                        "not_null": col.get("not_null", ""),
                        "default_value": col.get("default_value", "")
                    }

            # Если по какой-то причине columns пустой, пробуем альтернативные поля
            if not columns_dict:
                alt_columns = data.get("columns") or (data.get("table_schema") or {}).get("columns") or {}
                if isinstance(alt_columns, list):
                    for col in alt_columns:
                        if isinstance(col, dict):
                            name = col.get("name") or col.get("column_name")
                            if not name:
                                continue
                            columns_dict[name] = {k: v for k, v in col.items() if k not in ["name", "column_name"]}
                elif isinstance(alt_columns, dict):
                    columns_dict = alt_columns

            if columns_dict:
                # Используем FQN как ключ, как в json файле; UI показывает короткое имя
                schema[table_fqn] = {
                    "description": description,
                    "columns": columns_dict
                }

        return schema or None
    except Exception:
        return None

def _extract_workflow_steps(workflow_result) -> list:
    """Извлекает шаги из WorkflowResult для отображения step-by-step."""
    try:
        steps_ui = []
        # workflow_result.step_results: Dict[str, StepResult]
        for step_id, step_res in getattr(workflow_result, 'step_results', {}).items():
            try:
                steps_ui.append({
                    "id": step_id,
                    "status": getattr(step_res, 'status', None).value if getattr(step_res, 'status', None) else None,
                    "output": getattr(step_res, 'output', None),
                    "error": getattr(step_res, 'error', None),
                    "duration_seconds": getattr(step_res, 'duration_seconds', None),
                    "agent": getattr(step_res, 'agent_name', None),
                })
            except Exception:
                continue
        # Стабильный порядок по id
        steps_ui.sort(key=lambda x: x.get('id', ''))
        return steps_ui
    except Exception:
        return []

def run_agents_text_to_sql(natural_query, max_rows, safety_level, include_explanation,
                           validate_schema, dry_run_only, use_schema_suggestions):
    """
    DEPRECATED: Старый агентный подход.
    Используйте generate_sql_query() с text_to_sql_pipeline.yaml вместо этого.
    """
    try:
        session_id = _compute_session_id_from_dsn(st.session_state.selected_dsn or "t2s")

        # Формируем задачу для менеджера
        task = (
            f"Сформируй SQL-запрос по описанию на русском и выполни его.\n "
            f"Описание: {natural_query}.\n\n"
            f"{'А так же объясни полученные результаты' if include_explanation else ''}."
        )

        # Фоновый запуск в отдельном потоке, чтобы не блокировать UI
        # run_id генерируется внутри потока; здесь лишь регистрируем задачу
        job_id = str(uuid.uuid4())[:8]
        jobs = get_job_registry()
        lock = get_job_registry_lock()
        with lock:
            jobs[job_id] = {
            "status": "running",
            "mode": "agent",
            "session_id": session_id,
            "natural_query": natural_query,
            "started_at": datetime.now().isoformat(),
        }

        def _worker(job_id_local: str, task_local: str, session_local: str):
            try:
                # Генерируем единый run_id в потоке
                run_id_local = f"run-{uuid.uuid4().hex[:16]}"
                
                # Используем thread-safe run_id_context вместо глобального os.environ
                try:
                    from unified_logging import get_run_logger, run_id_context
                    
                    with run_id_context(run_id_local):
                        _rlog_thr = get_run_logger(run_id_local, __name__)
                        _rlog_thr.info("Фоновый поток динамического агента запущен")
                        
                        # КРИТИЧНО: Инициализируем корневой span УЖЕ ВНУТРИ run_id_context
                        root_span = None
                        try:
                            from telemetry import get_telemetry_manager
                            telemetry_manager = get_telemetry_manager()
                            if telemetry_manager and telemetry_manager.is_enabled():
                                root_span = telemetry_manager.start_run_trace(
                                    run_id=run_id_local,
                                    agent_name="DynamicAgentSystem",
                                    task=task_local,
                                    profile_type="text_to_sql_coordination",
                                    pipeline_name="text_to_sql",
                                    session_id=session_local
                                )
                                _rlog_thr.info(f"🔍 Создан корневой span для Text-to-SQL run_id: {run_id_local}")
                        except Exception as e:
                            _rlog_thr.warning(f"⚠️ Не удалось создать корневой span: {e}")
                            telemetry_manager = None
                        
                        # Запускаем DynamicAgentSystem в контексте корневого span
                        try:
                            from agent_system import DynamicAgentSystem
                            system_local = DynamicAgentSystem()
                            
                            # КРИТИЧНО: Выполняем coordinate в контексте корневого span
                            if root_span is not None:
                                # Используем OpenTelemetry context для span
                                from opentelemetry import trace
                                with trace.use_span(root_span):
                                    result_text_local = asyncio.run(system_local.coordinate(initial_task=task_local, session_id=run_id_local, show=False))
                            else:
                                # Fallback без span context
                                result_text_local = asyncio.run(system_local.coordinate(initial_task=task_local, session_id=run_id_local, show=False))
                            
                            # Успешное завершение
                            if root_span is not None:
                                telemetry_manager.finish_run_trace(root_span, success=True)
                                
                        except Exception as coord_err:
                            # Ошибка координации
                            if root_span is not None:
                                telemetry_manager.finish_run_trace(root_span, success=False, error_message=str(coord_err))
                            raise
                        
                except ImportError:
                    # Fallback если run_id_context недоступен  
                    os.environ["RUN_ID"] = run_id_local
                    try:
                        from unified_logging import get_run_logger
                        _rlog_thr = get_run_logger(run_id_local, __name__)
                        _rlog_thr.info("Фоновый поток агента запущен (fallback)")
                    except Exception:
                        _rlog_thr = None
                    
                    # Инициализируем корневой span для fallback тоже
                    root_span = None
                    try:
                        from telemetry import get_telemetry_manager
                        telemetry_manager = get_telemetry_manager()
                        if telemetry_manager and telemetry_manager.is_enabled():
                            root_span = telemetry_manager.start_run_trace(
                                run_id=run_id_local,
                                agent_name="DynamicAgentSystem",
                                task=task_local,
                                profile_type="text_to_sql_coordination",
                                pipeline_name="text_to_sql_fallback",
                                session_id=session_local
                            )
                    except Exception:
                        telemetry_manager = None
                    
                    # Запускаем DynamicAgentSystem с fallback
                    try:
                        from agent_system import DynamicAgentSystem
                        system_local = DynamicAgentSystem()
                        
                        # Выполняем в контексте корневого span (fallback)
                        if root_span is not None:
                            from opentelemetry import trace
                            with trace.use_span(root_span):
                                result_text_local = asyncio.run(system_local.coordinate(initial_task=task_local, session_id=run_id_local, show=False))
                        else:
                            result_text_local = asyncio.run(system_local.coordinate(initial_task=task_local, session_id=session_local, show=False))
                        
                        if root_span is not None:
                            telemetry_manager.finish_run_trace(root_span, success=True)
                            
                    except Exception as coord_err:
                        if root_span is not None:
                            telemetry_manager.finish_run_trace(root_span, success=False, error_message=str(coord_err))
                        raise
                # Обновляем запись задачи
                lock = get_job_registry_lock()
                with lock:
                    jobs[job_id_local].update({
                    "status": "done",
                    "report": result_text_local,
                    "finished_at": datetime.now().isoformat(),
                    "run_id": run_id_local,
                })
            except Exception as worker_err:
                lock = get_job_registry_lock()
                with lock:
                    jobs[job_id_local].update({
                    "status": "error",
                    "error": str(worker_err),
                    "finished_at": datetime.now().isoformat(),
                })

        t = threading.Thread(target=_worker, args=(job_id, task, session_id), daemon=True)
        t.start()

        # Фиксируем job_id в состоянии и информируем пользователя
        st.session_state.agent_job_id = job_id
        st.session_state.agent_run = None
        st.info(f"🚀 Агентный пайплайн запущен в фоне (job_id={job_id}). Run ID появится после старта.")
    except Exception as e:
        st.error(f"❌ Ошибка агентного запуска: {e}")

def run_yaml_text_to_sql(natural_query, max_rows, safety_level, include_explanation,
                         validate_schema, dry_run_only, use_schema_suggestions, pipeline_name: str):
    """
    DEPRECATED: Старый YAML подход с data_analysis pipeline.
    Используйте generate_sql_query() с text_to_sql_pipeline.yaml вместо этого.
    """
    try:
        session_id = _compute_session_id_from_dsn(st.session_state.selected_dsn or "t2s")

        from workflow.engine import WorkflowEngine
        engine = WorkflowEngine()

        # Генерируем и пробрасываем единый run_id для корреляции трасс
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        os.environ["RUN_ID"] = run_id

        variables = {
            "analysis_request": natural_query,
            "database_url": st.session_state.selected_dsn,
            "include_explanation": include_explanation,
            "dry_run_only": dry_run_only,
            "run_id": run_id,
        }

        with st.spinner("Запуск YAML workflow..."):
            workflow_result = asyncio.run(engine.execute_pipeline_by_name(pipeline_name=pipeline_name, **variables))

        # Готовим компактное представление результата для UI
        result_summary = {
            "status": str(getattr(workflow_result, "status", "unknown")),
            "duration_seconds": getattr(workflow_result, "duration_seconds", None),
            "total_steps": getattr(workflow_result, "total_steps", None),
            "completed_steps": getattr(workflow_result, "completed_steps", None),
            "failed_steps": getattr(workflow_result, "failed_steps", None),
        }

        # Извлекаем шаги для пошагового отображения
        yaml_steps = _extract_workflow_steps(workflow_result)

        st.session_state.agent_run = {
            "mode": "yaml",
            "session_id": session_id,
            "pipeline": pipeline_name,
            "summary": result_summary,
            "steps": yaml_steps,
            "natural_query": natural_query,
            "timestamp": datetime.now(),
            "run_id": run_id,
        }

        st.success(f"✅ YAML workflow выполнен (Run ID: {run_id})")
        # Сохраняем для показа кнопки вне формы
        st.session_state.last_yaml_run_id = run_id
    except Exception as e:
        st.error(f"❌ Ошибка YAML workflow: {e}")


def _extract_sql_from_structured_payload(payload: object) -> str:
    """Возвращает SQL только из явных структурированных SQL-полей."""
    if isinstance(payload, dict):
        for key in ("sql_query", "sqlQuery", "sql", "generated_sql"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("result", "artifacts", "output"):
            nested = _extract_sql_from_structured_payload(payload.get(key))
            if nested:
                return nested
        for value in payload.values():
            nested = _extract_sql_from_structured_payload(value)
            if nested:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _extract_sql_from_structured_payload(item)
            if nested:
                return nested
    elif hasattr(payload, "output"):
        return _extract_sql_from_structured_payload(getattr(payload, "output"))
    return ""


def generate_sql_query(natural_query, max_rows, safety_level, include_explanation,
                      validate_schema, dry_run_only, use_schema_suggestions):
    """Генерация SQL запроса через AG-UI service action presets.text_to_sql.generate.

    Маршрут A (in-process): handle_service_action валидирует whitelist параметров,
    резолвит DSN-рефы (`db_config:` / `env:`), вычисляет session_id по DSN-fingerprint
    и стартует workflow через WorkflowManager. UI читает прогресс / артефакты через
    WorkflowManager.get_workflow_status / get_workflow_artifacts (см. workflow_pipelines/text_to_sql_pipeline.yaml).
    Параметр 'allow_enhanced_fallback': False фиксирует strict-режим (без legacy fallback).
    """
    try:
        selected_dsn = st.session_state.selected_dsn
        if not selected_dsn:
            st.error("❌ Выберите подключение к базе данных")
            return
        max_rows, safety_level = _validate_text_to_sql_options(max_rows, safety_level)

        from backend.fastapi_app.agui.service import handle_service_action
        from workflow.streamlit_api import WorkflowManager

        payload = {
            "query": natural_query,
            "dsn": selected_dsn,
            "max_rows": max_rows,
            "safety_level": safety_level,
            "include_explanation": include_explanation,
            "validate_schema": validate_schema,
            "dry_run_only": dry_run_only,
            "use_schema_suggestions": use_schema_suggestions,
            "allow_enhanced_fallback": False,
            "workflow_name": "text_to_sql_pipeline",
            "client_id": st.session_state.get("user_id", "streamlit_user"),
        }

        with st.spinner("⚡ Выполняем Text-to-SQL workflow..."):
            start_time = time.time()
            response = handle_service_action("presets.text_to_sql.generate", payload)
            run_id = response["run_id"]
            session_id = response["session_id"]

            wf_manager = WorkflowManager()

            # Polling до завершения workflow (таймаут 5 минут)
            terminal_statuses = {"completed", "failed", "cancelled"}
            status_obj = None
            poll_deadline = time.time() + 300  # 5 minutes
            # Early-exit: если статус подряд возвращается None (run_id не в реестре),
            # нет смысла ждать весь таймаут — прерываемся после 10 итераций подряд.
            none_streak = 0
            while True:
                status_obj = wf_manager.get_workflow_status(run_id)
                if status_obj is not None and status_obj.status in terminal_statuses:
                    break
                if status_obj is None:
                    none_streak += 1
                    if none_streak > 10:
                        st.error("⛔ run_id не найден в реестре, выполнение прервано.")
                        return
                else:
                    none_streak = 0
                if time.time() >= poll_deadline:
                    st.error("⏱️ Превышен таймаут ожидания workflow (5 минут). Попробуйте позже.")
                    return
                time.sleep(0.5)

            execution_time = (time.time() - start_time) * 1000  # ms

        artifacts = wf_manager.get_workflow_artifacts(run_id)
        step_outputs = (artifacts.step_outputs if artifacts and artifacts.step_outputs else {}) or {}
        step_results = status_obj.step_results if status_obj and status_obj.step_results else {}
        final_output = artifacts.final_output if artifacts else None

        if status_obj.status == "completed":
            # EPIC 6.3: god-manager sql_pipeline декомпозирован на
            # sql_generation / sql_verification / db_audit. Финальный шаг — db_audit;
            # старое имя сохраняем как fallback для уже сохранённых запусков.
            sql_query = _extract_sql_from_structured_payload(step_outputs)

            st.session_state.generated_sql = {
                "final_output": final_output,
                "query": sql_query,
                "formatted": sql_query,
                "sql_query": sql_query,
                "natural_query": natural_query,
                "timestamp": datetime.now(),
                "execution_time_ms": execution_time,
                "generation_time": execution_time,
                "status": "completed",
                "execution_status": {
                    "dry_run_only": bool(dry_run_only),
                    "executed": not bool(dry_run_only),
                    "status": "skipped" if dry_run_only else "completed",
                },
                "run_id": run_id,
                "session_id": session_id,
                "max_rows": max_rows,
                "flags": {
                    "safety_level": safety_level,
                    "include_explanation": include_explanation,
                    "validate_schema": validate_schema,
                    "dry_run_only": dry_run_only,
                    "use_schema_suggestions": use_schema_suggestions,
                },
                "steps": {
                    "nlu_processing": step_outputs.get("nlu_processing"),
                    "intent_extraction": step_outputs.get("intent_extraction_step"),
                    "schema_linking": step_outputs.get("schema_linking_step"),
                    # EPIC 6.3: новые декомпозированные шаги (db_audit — финальный).
                    "sql_generation": step_outputs.get("sql_generation"),
                    "sql_verification": step_outputs.get("sql_verification"),
                    "db_audit": step_outputs.get("db_audit"),
                    # legacy ключ оставляем для совместимости со старыми запусками
                    "sql_pipeline": step_outputs.get("sql_pipeline"),
                },
            }

            st.success(f"✅ Text-to-SQL workflow выполнен за {execution_time:.1f}ms (Run ID: {run_id})")

            # Показываем прогресс по шагам
            with st.expander("📊 Детали выполнения шагов", expanded=False):
                for step_id, step_meta in step_results.items():
                    if not step_meta:
                        continue
                    col1, col2 = st.columns([3, 1])
                    col1.write(f"**{step_id}**")
                    status_str = step_meta.get("status") if isinstance(step_meta, dict) else getattr(step_meta, "status", "")
                    col2.write(f"✅ {status_str}")

            # Отображаем финальный результат
            if final_output:
                st.markdown("---")
                st.markdown("### 📄 Итоговый отчет")
                st.markdown(final_output if isinstance(final_output, str) else str(final_output))

            # Автосохранение в историю
            try:
                save_to_history(st.session_state.generated_sql)
            except Exception as e:
                st.warning(f"⚠️ Не удалось сохранить в историю: {e}")

        elif status_obj.status == "failed":
            st.error(f"❌ Workflow завершился с ошибкой: {status_obj.error_message}")
            if step_results:
                with st.expander("⚠️ Частичные результаты выполнения"):
                    for step_id, step_meta in step_results.items():
                        if not step_meta:
                            continue
                        status_str = step_meta.get("status") if isinstance(step_meta, dict) else getattr(step_meta, "status", "")
                        st.write(f"**{step_id}**: {status_str}")
        else:
            st.warning(f"⚠️ Неожиданный статус workflow: {status_obj.status}")

    except FileNotFoundError:
        st.error("❌ Файл text_to_sql_pipeline.yaml не найден!")
        st.info("Убедитесь, что файл находится в workflow_pipelines/text_to_sql_pipeline.yaml")

    except Exception as e:
        st.error(f"❌ Ошибка выполнения workflow: {e}")
        st.exception(e)

def execute_sql_query(sql_query, max_rows):
    """
    DEPRECATED: Функция больше не используется.
    Workflow pipeline выполняет SQL автоматически через db_audit_agent.
    """
    st.warning("⚠️ Эта функция устарела. SQL выполняется автоматически в workflow pipeline.")
    pass

def explain_natural_query(natural_query):
    """
    DEPRECATED: Функция больше не используется.
    Schema linking выполняется автоматически в workflow pipeline.
    """
    st.warning("⚠️ Эта функция устарела. Анализ запроса выполняется автоматически в workflow pipeline.")
    
    # Показываем базовую информацию о диалекте
    try:
        from db_plugins.streamlit_api import get_db_plugin_manager
        db_manager = get_db_plugin_manager()
        validation = db_manager.validate_dsn(st.session_state.selected_dsn)
        
        if validation.is_valid:
            limits_info = db_manager.get_sql_generation_limits(validation.detected_scheme)
            
            st.markdown("**⚙️ Особенности диалекта:**")
            st.info(f"Диалект: {limits_info.get('dialect_label', 'Unknown')}")
            st.info(f"Синтаксис лимитов: {limits_info.get('limit_syntax', 'LIMIT')}")
            st.info(f"Максимум строк: {limits_info.get('max_rows_recommended', 1000)}")
    except Exception as e:
        st.error(f"❌ Ошибка получения информации о диалекте: {e}")

# === Вспомогательная функция: автосохранение истории агентного режима ===
def _try_autosave_agent_history(run_info: dict):
    """Сохраняет историю только из уже структурированного SQL в session_state."""
    sql_data = st.session_state.get("generated_sql")
    if isinstance(sql_data, dict) and sql_data.get("query"):
        try:
            if "execution" in sql_data:
                save_to_history(sql_data)
        except Exception:
            pass

def show_sql_results():
    """Отображение результатов генерации и выполнения SQL"""
    
    sql_data = st.session_state.generated_sql
    
    st.markdown("## 📊 Результаты")
    
    # Метрики
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("⚡ Время генерации", f"{sql_data.get('generation_time', 0):.1f}ms")
    
    with col2:
        confidence = sql_data.get('confidence', 0)
        st.metric("🎯 Уверенность", f"{confidence:.1%}")
    
    with col3:
        st.metric("🗃️ Диалект", sql_data.get('dialect', 'Unknown'))
    
    with col4:
        if 'execution' in sql_data:
            execution = sql_data['execution']
            st.metric("📊 Строк", execution.row_count)
        else:
            st.metric("📊 Строк", "N/A")
    
    # Вкладки результатов
    result_tab1, result_tab2, result_tab3, result_tab4 = st.tabs(["🔍 SQL", "📊 Данные", "🔒 Валидация", "📋 Объяснение"])
    
    with result_tab1:
        show_sql_code()
    
    with result_tab2:
        show_execution_results()
    
    with result_tab3:
        show_validation_results()
    
    with result_tab4:
        show_explanation_results()

def show_agent_workflow_results():
    """Отображение результатов агентного/Workflow запуска (трассировка)."""
    # Статус фоновой задачи динамического менеджера (если есть)
    job_id = st.session_state.get("agent_job_id")
    jobs = get_job_registry()
    if job_id and job_id in jobs:
        job = jobs[job_id]
        st.markdown("## 🤖 Результаты агентного выполнения")
        status = job.get("status")
        cols = st.columns(3)
        with cols[0]:
            st.info(f"Режим: Агентный")
        with cols[1]:
            st.info(f"Сессия: {job.get('session_id')}")
        with cols[2]:
            st.info(f"Статус: {status}")

        if status == "running":
            st.warning("⏳ Выполняется... Нажмите '🔄 Обновить статус' для обновления.")
            # Отображаем простой прогресс по времени
            try:
                started = job.get("started_at")
                if started:
                    try:
                        started_dt = datetime.fromisoformat(started)
                        elapsed = (datetime.now() - started_dt).total_seconds()
                    except Exception:
                        elapsed = None
                else:
                    elapsed = None
                prog = st.progress(int(time.time()) % 10 / 10)
                if elapsed is not None:
                    st.caption(f"Прошло ~ {int(elapsed)} сек")
            except Exception:
                pass
            # Автообновление статуса: throttle через session_state-метку, БЕЗ
            # блокировки UI-потока (исходный time.sleep(2) замораживал воркер).
            # Trade-off: в классическом Streamlit непрерывный неблокирующий поллинг
            # без st.fragment/компонента невозможен — поэтому есть кнопка
            # «🔄 Обновить статус» как явный fallback. Безусловный st.rerun() здесь
            # НЕЛЬЗЯ: он даёт busy-loop (rerun каждые ~0мс, 100% CPU + долбёжка backend).
            # Ключ throttle композитный с job_id: новый запуск не наследует
            # устаревшую метку предыдущего (иначе первый rerun мог запаздывать).
            rerun_key = f"_agent_last_rerun_{job_id}"
            last_check = st.session_state.get(rerun_key, 0)
            if time.time() - last_check >= 2:
                st.session_state[rerun_key] = time.time()
                st.rerun()
            return
        elif status == "error":
            st.error(f"❌ Ошибка: {job.get('error')}")
        elif status == "done":
            # Переносим в session_state.agent_run для дальнейшего отображения
            st.session_state.agent_run = {
                "mode": "agent",
                "session_id": job.get('session_id'),
                "report": job.get('report'),
                "natural_query": job.get('natural_query'),
                "timestamp": datetime.now(),
                "run_id": job.get('run_id'),
            }
            # Пытаемся автосохранить историю на основании трасс текущего run_id
            try:
                _try_autosave_agent_history(st.session_state.agent_run)
            except Exception:
                pass

    run_info = st.session_state.agent_run
    if not run_info:
        return

    st.markdown("## 🤖 Результаты агентного выполнения")
    cols = st.columns(3)
    with cols[0]:
        st.info(f"Режим: {'Агентный' if run_info.get('mode')=='agent' else 'YAML'}")
    with cols[1]:
        st.info(f"Сессия: {run_info.get('session_id')}")
    with cols[2]:
        ts = run_info.get('timestamp')
        if ts:
            st.info(f"Время: {ts.strftime('%Y-%m-%d %H:%M:%S')}")

    if run_info.get("mode") == "agent" and run_info.get("report"):
        # Best-effort автосохранение истории, если ещё не сохраняли
        try:
            _try_autosave_agent_history(run_info)
        except Exception:
            pass
        with st.expander("📋 Отчет менеджера", expanded=False):
            st.text(run_info["report"])
        # Best-effort: выделим предполагаемые шаги из отчета (по заголовкам/иконкам)
        try:
            import re
            steps = []
            for line in run_info["report"].splitlines():
                if line.strip().startswith("📋 Результаты агента") or line.strip().startswith("🔍 Промежуточные шаги"):
                    steps.append(line.strip())
            if steps:
                with st.expander("🧭 Шаги динамического агента (эвристика)", expanded=False):
                    for s in steps[:50]:
                        st.write(s)
        except Exception:
            pass
    elif run_info.get("mode") == "yaml" and run_info.get("summary"):
        with st.expander("📊 Сводка workflow", expanded=True):
            st.json(run_info["summary"])
        # Показ пошаговых результатов YAML (если есть)
        if run_info.get("steps"):
            with st.expander("🧭 Шаги YAML Workflow", expanded=True):
                for step in run_info["steps"]:
                    st.markdown(f"**{step.get('id','step')}** · {step.get('status','unknown')}")
                    if step.get('agent'):
                        st.caption(f"Агент: {step['agent']}")
                    if step.get('duration_seconds') is not None:
                        st.caption(f"Время: {step['duration_seconds']}с")
                    if step.get('error'):
                        st.error(step['error'])
                    if step.get('output'):
                        with st.expander("Вывод", expanded=False):
                            try:
                                st.json(step['output'])
                            except Exception:
                                st.text(str(step['output'])[:4000])

        # Кнопка открытия трасс (вне форм)
        rid = run_info.get("run_id") or st.session_state.get("last_yaml_run_id")
        if rid:
            go_col1, go_col2 = st.columns([1,3])
            with go_col1:
                if st.button("🔍 Открыть трассы", key=f"yaml_traces_{rid}"):
                    try:
                        st.query_params["run_id"] = rid
                    except Exception:
                        pass
                    st.switch_page("pages/08_Logs_Traces.py")

def show_sql_code():
    """Отображение сгенерированного SQL"""
    
    sql_data = st.session_state.generated_sql
    
    col1, col2 = st.columns([3, 1])
    
    with col1:
        st.markdown("### 🔍 Сгенерированный SQL")
        
        # Форматированный SQL
        formatted_sql = sql_data.get('formatted', sql_data.get('query', ''))
        st.code(formatted_sql, language='sql')
        
        # Исходный запрос
        st.markdown("**📝 Исходный запрос:**")
        st.info(sql_data.get('natural_query', ''))
    
    with col2:
        st.markdown("### ⚙️ Действия")
        
        # Копирование SQL
        if st.button("📋 Копировать SQL"):
            st.code(sql_data.get('query', ''), language='sql')
        
        # Сохранение в историю
        if st.button("💾 Сохранить в историю"):
            save_to_history(sql_data)
            st.success("✅ Сохранено в историю")
        
        # Редактирование
        if st.button("✏️ Редактировать"):
            show_sql_editor()
        
        # Повторное выполнение
        if 'execution' in sql_data:
            if st.button("🔄 Повторить выполнение"):
                execute_sql_query(sql_data['query'], 100)
                st.rerun()

def show_sql_editor():
    """Редактор SQL запроса"""
    
    sql_data = st.session_state.generated_sql
    
    st.markdown("### ✏️ Редактирование SQL")
    
    edited_sql = st.text_area(
        "SQL запрос",
        value=sql_data.get('query', ''),
        height=200,
        help="Отредактируйте SQL запрос"
    )
    
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("✅ Сохранить изменения"):
            sql_data['query'] = edited_sql
            sql_data['formatted'] = edited_sql  # Простое форматирование
            st.session_state.generated_sql = sql_data
            st.success("✅ Изменения сохранены")
            st.rerun()
    
    with col2:
        if st.button("🚀 Выполнить отредактированный"):
            execute_sql_query(edited_sql, 100)
            st.rerun()

def show_execution_results():
    """Отображение результатов выполнения"""
    
    sql_data = st.session_state.generated_sql
    
    execution_status = sql_data.get('execution_status') or {}
    if 'execution' not in sql_data:
        if execution_status.get("dry_run_only") or execution_status.get("status") == "skipped":
            st.info("📊 Dry-run: SQL сгенерирован, выполнение запроса было отключено.")
        else:
            st.info("📊 SQL не был выполнен. Включите выполнение в настройках генерации.")
        return
    
    execution = sql_data['execution']
    
    if execution.success and execution.rows:
        st.markdown(f"### 📊 Результаты ({execution.row_count} строк)")
        
        # Конвертируем в DataFrame
        df = pd.DataFrame(execution.rows)
        
        # Отображаем данные
        st.dataframe(df, use_container_width=True)
        
        # Дополнительная информация
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric("⏱️ Время выполнения", f"{execution.execution_time_ms:.1f}ms")
        
        with col2:
            st.metric("📊 Колонок", len(execution.columns))
        
        with col3:
            if st.button("📥 Скачать CSV"):
                csv = df.to_csv(index=False)
                st.download_button(
                    label="💾 Скачать результаты",
                    data=csv,
                    file_name=f"sql_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv"
                )
        
        # Предупреждения
        if execution.warnings:
            st.markdown("### ⚠️ Предупреждения")
            for warning in execution.warnings:
                st.warning(warning)
    
    elif execution.success and not execution.rows:
        st.info("📊 Запрос выполнен успешно, но не вернул данных")
    
    else:
        st.error(f"❌ Ошибка выполнения: {execution.error_message}")

def show_validation_results():
    """Отображение результатов валидации"""
    
    sql_data = st.session_state.generated_sql
    
    if 'validation' not in sql_data:
        st.info("🔒 Валидация не выполнялась")
        return
    
    validation = sql_data['validation']
    
    # Общий статус
    col1, col2, col3 = st.columns(3)
    
    with col1:
        if validation.is_safe:
            st.success("✅ Безопасность: OK")
        else:
            st.error("❌ Небезопасно")
    
    with col2:
        if validation.is_valid:
            st.success("✅ Валидность: OK")
        else:
            st.error("❌ Невалидно")
    
    with col3:
        risk_colors = {
            "low": "🟢",
            "medium": "🟡",
            "high": "🟠",
            "critical": "🔴"
        }
        risk_icon = risk_colors.get(validation.risk_level, "⚪")
        st.info(f"Риск: {risk_icon} {validation.risk_level}")
    
    # Детали валидации
    if validation.safety_issues:
        st.markdown("### 🔒 Проблемы безопасности")
        for issue in validation.safety_issues:
            issue_type = issue.get('issue_type', 'Unknown')
            description = issue.get('description', 'Нет описания')
            
            if 'CRITICAL' in issue_type or 'DANGER' in issue_type:
                st.error(f"🔴 **{issue_type}**: {description}")
            elif 'WARNING' in issue_type:
                st.warning(f"🟡 **{issue_type}**: {description}")
            else:
                st.info(f"ℹ️ **{issue_type}**: {description}")
    
    if validation.schema_issues:
        st.markdown("### 🗄️ Проблемы схемы")
        for issue in validation.schema_issues:
            st.warning(f"⚠️ {issue}")
    
    if validation.suggestions:
        st.markdown("### 💡 Предложения по улучшению")
        for suggestion in validation.suggestions:
            st.info(f"💡 {suggestion}")

def show_explanation_results():
    """Отображение объяснений"""
    
    sql_data = st.session_state.generated_sql
    
    # Объяснение запроса
    if sql_data.get('explanation'):
        st.markdown("### 📝 Объяснение запроса")
        st.markdown(sql_data['explanation'])
    
    # План выполнения
    if 'explain_plan' in sql_data:
        explain_plan = sql_data['explain_plan']
        
        st.markdown("### 📋 План выполнения")
        
        if isinstance(explain_plan, dict) and 'plan' in explain_plan:
            st.json(explain_plan['plan'])
        elif isinstance(explain_plan, dict):
            st.json(explain_plan)
        else:
            st.text(str(explain_plan))
    
    # Дополнительная информация
    st.markdown("### ℹ️ Дополнительная информация")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("**⏰ Время генерации:**")
        st.info(f"{sql_data.get('generation_time', 0):.1f} мс")
        
        st.markdown("**🎯 Уверенность:**")
        confidence = sql_data.get('confidence', 0)
        confidence_text = "Высокая" if confidence > 0.8 else "Средняя" if confidence > 0.5 else "Низкая"
        st.info(f"{confidence:.1%} ({confidence_text})")
    
    with col2:
        st.markdown("**🕐 Время создания:**")
        timestamp = sql_data.get('timestamp')
        if timestamp:
            st.info(timestamp.strftime("%Y-%m-%d %H:%M:%S"))
        
        st.markdown("**🗃️ Диалект БД:**")
        st.info(sql_data.get('dialect', 'Unknown'))

def save_to_history(sql_data):
    """Сохранение запроса в историю"""
    sql_query = (
        sql_data.get('sql_query')
        or sql_data.get('query')
        or sql_data.get('formatted')
    )
    success = None
    if 'execution' in sql_data:
        success = bool(sql_data['execution'].success)
    elif sql_data.get('status'):
        success = sql_data.get('status') == 'completed'
    
    history_entry = {
        "id": len(st.session_state.sql_history) + 1,
        "natural_query": sql_data.get('natural_query', ''),
        "sql_query": sql_query,
        "final_output": sql_data.get('final_output'),
        "dialect": sql_data.get('dialect', ''),
        "confidence": sql_data.get('confidence', 0),
        "generation_time": sql_data.get('generation_time', sql_data.get('execution_time_ms', 0)),
        "timestamp": datetime.now(),
        "dsn": _redact_dsn(st.session_state.selected_dsn),
        "dsn_fingerprint": _dsn_fingerprint(st.session_state.selected_dsn) if st.session_state.selected_dsn else None,
        "status": sql_data.get('status'),
        "execution_status": sql_data.get('execution_status'),
        "run_id": sql_data.get('run_id'),
        "session_id": sql_data.get('session_id'),
        "max_rows": sql_data.get('max_rows'),
        "flags": sql_data.get('flags'),
        "success": success,
        "row_count": sql_data['execution'].row_count if 'execution' in sql_data and sql_data['execution'].success else None
    }
    
    history_entry = _redact_payload(history_entry)
    st.session_state.sql_history.append(history_entry)
    st.session_state.sql_history = st.session_state.sql_history[-100:]
    # Персистим в JSONL, чтобы история переживала перезапуск
    try:
        file_path = _history_file_path()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        to_dump = dict(history_entry)
        # Сериализуем timestamp
        ts = to_dump.get("timestamp")
        if isinstance(ts, datetime):
            to_dump["timestamp"] = ts.isoformat()
        with open(file_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(to_dump, ensure_ascii=False) + "\n")
    except Exception:
        pass

def show_database_connections():
    """Управление подключениями к БД"""
    
    st.markdown("## 🔌 Подключения к базам данных")
    
    try:
        from db_plugins.streamlit_api import get_db_plugin_manager
        
        db_manager = get_db_plugin_manager()
        
        # Доступные плагины
        st.markdown("### 🛠️ Доступные плагины БД")
        
        plugins = db_manager.list_plugins()
        
        plugin_info = []
        for plugin in plugins:
            plugin_info.append({
                "Схема": plugin.scheme,
                "Название": plugin.name,
                "Диалект": plugin.dialect_label,
                "Возможности": len(plugin.supported_features)
            })
        
        if plugin_info:
            st.dataframe(plugin_info, use_container_width=True)
        
        # Форма подключения
        st.markdown("### 🔗 Настройка подключения")
        
        with st.form("database_connection_form"):
            col1, col2 = st.columns(2)
            
            with col1:
                selected_scheme = st.selectbox(
                    "🛠️ Тип базы данных",
                    options=[p.scheme for p in plugins],
                    help="Выберите тип базы данных"
                )
                
                # Получаем примеры DSN для выбранной схемы
                selected_plugin = next((p for p in plugins if p.scheme == selected_scheme), None)
                dsn_examples = selected_plugin.dsn_examples if selected_plugin else []
                
                if dsn_examples:
                    st.markdown("**📋 Примеры DSN:**")
                    for example in dsn_examples:
                        st.code(example, language='text')
            
            with col2:
                dsn = st.text_input(
                    "🔗 DSN (строка подключения)",
                    value=st.session_state.selected_dsn,
                    placeholder="postgresql://user:password@host:5432/database.schema",
                    help="Строка подключения к базе данных. Схема определяется автоматически или используется по умолчанию."
                )
                
                # Информация о схемах по умолчанию
                st.info("💡 **Автоматическое определение схемы**: Если схема не указана в DSN, будет использована схема по умолчанию для данной БД")
            
            # Кнопки
            col1, col2, col3 = st.columns(3)
            
            with col1:
                validate_clicked = st.form_submit_button("🔍 Валидировать DSN", type="secondary")
            
            with col2:
                test_clicked = st.form_submit_button("🧪 Тест соединения", type="secondary")
            
            with col3:
                connect_clicked = st.form_submit_button("🔗 Подключиться", type="primary")
            
            # Обработка действий
            if validate_clicked and dsn:
                validate_dsn_connection(db_manager, dsn)
            
            if test_clicked and dsn:
                test_database_connection(db_manager, dsn)
            
            if connect_clicked and dsn:
                connect_to_database(db_manager, dsn)
    
    except Exception as e:
        st.error(f"❌ Ошибка загрузки плагинов: {e}")

def validate_dsn_connection(db_manager, dsn):
    """Валидация DSN"""
    
    with st.spinner("Валидация DSN..."):
        validation = db_manager.validate_dsn(dsn, check_schema_requirement=True)
    
    if validation.is_valid:
        st.success("✅ DSN валиден")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("**🔍 Обнаруженные компоненты:**")
            for key, value in validation.parsed_components.items():
                if value and key != "password":
                    st.info(f"**{key}**: {value}")
        
        with col2:
            st.info(f"**Схема БД**: {validation.detected_scheme}")
            if validation.detected_schema:
                st.info(f"**Схема**: {validation.detected_schema}")
            else:
                st.warning("**Схема**: Не найдена в DSN")
    
    else:
        st.error("❌ DSN невалиден")
        
        if validation.errors:
            st.markdown("**Ошибки:**")
            for error in validation.errors:
                st.error(f"• {error}")
        
        if validation.warnings:
            st.markdown("**Предупреждения:**")
            for warning in validation.warnings:
                st.warning(f"• {warning}")
        
        if validation.suggestions:
            st.markdown("**Предложения:**")
            for suggestion in validation.suggestions:
                st.info(f"💡 {suggestion}")

def test_database_connection(db_manager, dsn):
    """Тестирование соединения с БД"""
    
    with st.spinner("Тестирование соединения..."):
        test_result = db_manager.test_connection(dsn, timeout_seconds=10)
    
    if test_result.success:
        st.success(f"✅ Соединение успешно установлено за {test_result.connection_time_ms:.1f}ms")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.info(f"**Плагин:** {test_result.plugin_name}")
            st.info(f"**Диалект:** {test_result.dialect}")
            if test_result.schema_detected:
                st.info(f"**Схема:** {test_result.schema_detected}")
        
        with col2:
            if test_result.metadata:
                if test_result.metadata.get("test_query_success"):
                    st.success("✅ Тестовый запрос выполнен")
                
                if test_result.metadata.get("schema_introspection_success"):
                    st.success("✅ Интроспекция схемы работает")
                    tables_count = test_result.metadata.get("tables_count", 0)
                    st.info(f"**Таблиц найдено:** {tables_count}")
        
        if test_result.validation_warnings:
            st.markdown("**Предупреждения:**")
            for warning in test_result.validation_warnings:
                st.warning(f"⚠️ {warning}")
    
    else:
        st.error(f"❌ Ошибка соединения: {test_result.error_message}")
        
        if test_result.validation_warnings:
            st.markdown("**Дополнительная информация:**")
            for warning in test_result.validation_warnings:
                st.info(f"ℹ️ {warning}")

def connect_to_database(db_manager, dsn):
    """Подключение к базе данных"""
    
    # Сначала валидируем
    validation = db_manager.validate_dsn(dsn)
    
    if not validation.is_valid:
        st.error("❌ Невозможно подключиться: DSN невалиден")
        return
    
    # Тестируем соединение
    test_result = db_manager.test_connection(dsn)
    
    if test_result.success:
        st.session_state.selected_dsn = dsn
        st.success(f"✅ Подключено к {test_result.dialect} ({test_result.plugin_name})")
        # Регистрируем подключение и пробрасываем безопасный connection_id в URL
        try:
            registry = get_connection_registry()
            conn_id = _generate_connection_id(dsn)
            registry[conn_id] = dsn
            # Обновляем query params современным API
            try:
                if "dsn" in st.query_params:
                    del st.query_params["dsn"]
                st.query_params["conn"] = conn_id
            except Exception:
                pass
        except Exception:
            pass
        
        # Загружаем схему
        load_database_schema(dsn)
        
        st.rerun()
    else:
        st.error(f"❌ Не удалось подключиться: {test_result.error_message}")

def load_database_schema(dsn):
    """Загрузка схемы базы данных"""
    
    try:
        # 1) Пробуем получить схему из памяти (если индексирована)
        mem_schema = _load_schema_from_memory(dsn)
        if mem_schema:
            st.session_state.current_schema = mem_schema
            st.session_state.current_schema_source = "memory"
            return

        # 2) Fallback: интроспекция через плагин
        from db_plugins import get_plugin
        plugin = get_plugin(dsn)
        conn = plugin.connect(dsn)
        try:
            schema = plugin.introspect_schema(conn)
            st.session_state.current_schema = schema
            st.session_state.current_schema_source = "db"
        finally:
            plugin.close(conn)
    
    except Exception as e:
        st.warning(f"⚠️ Не удалось загрузить схему: {e}")

def show_database_schema():
    """Отображение схемы базы данных"""
    
    st.markdown("## 📊 Схема базы данных")
    
    # Дополнительно восстанавливаем DSN по безопасному connection_id из query params
    if not st.session_state.selected_dsn:
        try:
            qp_conn = st.query_params.get("conn")
            if isinstance(qp_conn, list):
                qp_conn = qp_conn[0]
            if qp_conn:
                registry = get_connection_registry()
                st.session_state.selected_dsn = registry.get(qp_conn, "")
        except Exception:
            pass
    if not st.session_state.selected_dsn:
        st.warning("⚠️ Подключитесь к базе данных на вкладке 'Подключения'")
        return
    
    if not st.session_state.current_schema:
        st.info("📊 Схема не загружена. Попробуйте переподключиться к БД.")
        
        if st.button("🔄 Загрузить схему"):
            with st.spinner("Загрузка схемы..."):
                load_database_schema(st.session_state.selected_dsn)
            st.rerun()
        return
    
    schema = st.session_state.current_schema
    source = st.session_state.get("current_schema_source", "unknown")
    source_human = "Память" if source == "memory" else ("База данных" if source == "db" else "неизвестно")
    st.caption(f"Источник схемы: {source_human}")
    
    # Статистика схемы
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric("📊 Таблиц", len(schema))
    
    with col2:
        total_columns = sum(len(table_info.get("columns", {})) for table_info in schema.values())
        st.metric("📋 Колонок", total_columns)
    
    with col3:
        pk_tables = sum(1 for table_info in schema.values() 
                       if any(col.get("constraint_type") == "PK" 
                             for col in table_info.get("columns", {}).values()))
        st.metric("🔑 Таблиц с PK", pk_tables)
    
    # Поиск по таблицам
    search_term = st.text_input("🔍 Поиск таблиц", placeholder="Введите название таблицы...")
    
    # Фильтрация таблиц
    filtered_tables = schema
    if search_term:
        filtered_tables = {
            table_name: table_info 
            for table_name, table_info in schema.items()
            if search_term.lower() in table_name.lower()
        }
    
    # Отображение таблиц
    for table_name, table_info in filtered_tables.items():
        with st.expander(f"📊 {table_name}", expanded=False):
            
            # Описание таблицы
            description = table_info.get("description", "")
            if description:
                st.markdown(f"**Описание:** {description}")
            
            # Колонки
            columns = table_info.get("columns", {})
            if columns:
                st.markdown("**Колонки:**")
                
                columns_data = []
                for col_name, col_info in columns.items():
                    constraint_icon = {
                        "PK": "🔑",
                        "FK": "🔗",
                        "UNIQUE": "🎯",
                        "": ""
                    }
                    
                    columns_data.append({
                        "Название": col_name,
                        "Тип": col_info.get("type", ""),
                        "Ограничение": f"{constraint_icon.get(col_info.get('constraint_type', ''), '')} {col_info.get('constraint_type', '')}",
                        "Описание": col_info.get("description", "")[:50] + ("..." if len(col_info.get("description", "")) > 50 else "")
                    })
                
                st.dataframe(columns_data, use_container_width=True)
                
                # FK связи
                fk_columns = [
                    (col_name, col_info.get("references", ""))
                    for col_name, col_info in columns.items()
                    if col_info.get("constraint_type") == "FK" and col_info.get("references")
                ]
                
                if fk_columns:
                    st.markdown("**🔗 Внешние ключи:**")
                    for col_name, reference in fk_columns:
                        st.info(f"**{col_name}** → {reference}")

def show_sql_history():
    """История SQL запросов"""
    
    st.markdown("## 📚 История SQL запросов")
    
    if not st.session_state.sql_history:
        st.info("📭 История пуста. Выполните несколько запросов для заполнения истории.")
        return
    
    # Фильтры
    col1, col2, col3 = st.columns(3)
    
    with col1:
        filter_success = st.selectbox(
            "🎯 Статус",
            ["Все", "Успешные", "Ошибки", "Не выполнялись"]
        )
    
    with col2:
        filter_dialect = st.selectbox(
            "🗃️ Диалект",
            ["Все"] + list(set(entry.get("dialect", "Unknown") for entry in st.session_state.sql_history))
        )
    
    with col3:
        search_query = st.text_input("🔍 Поиск в запросах")
    
    # Фильтрация истории
    filtered_history = st.session_state.sql_history
    
    if filter_success != "Все":
        if filter_success == "Успешные":
            filtered_history = [h for h in filtered_history if h.get("success") is True]
        elif filter_success == "Ошибки":
            filtered_history = [h for h in filtered_history if h.get("success") is False]
        elif filter_success == "Не выполнялись":
            filtered_history = [h for h in filtered_history if h.get("success") is None]
    
    if filter_dialect != "Все":
        filtered_history = [h for h in filtered_history if h.get("dialect") == filter_dialect]
    
    if search_query:
        filtered_history = [
            h for h in filtered_history 
            if search_query.lower() in h.get("natural_query", "").lower() or 
               search_query.lower() in h.get("sql_query", "").lower()
        ]
    
    # Отображение истории
    for entry in reversed(filtered_history[-20:]):  # Последние 20 записей
        with st.expander(f"🕐 {entry['timestamp'].strftime('%H:%M:%S')} - {entry['natural_query'][:50]}...", expanded=False):
            
            col1, col2 = st.columns([2, 1])
            
            with col1:
                st.markdown("**📝 Естественный запрос:**")
                st.info(entry["natural_query"])
                
                st.markdown("**🔍 SQL запрос:**")
                st.code(entry["sql_query"], language='sql')
            
            with col2:
                st.metric("🎯 Уверенность", f"{entry.get('confidence', 0):.1%}")
                st.metric("⚡ Время", f"{entry.get('generation_time', 0):.1f}ms")
                st.info(f"**Диалект:** {entry.get('dialect', 'Unknown')}")
                
                if entry.get("success") is True:
                    st.success(f"✅ Успешно ({entry.get('row_count', 0)} строк)")
                elif entry.get("success") is False:
                    st.error("❌ Ошибка выполнения")
                else:
                    st.info("ℹ️ Не выполнялся")
                
                # Кнопки действий
                if st.button(f"🔄 Повторить", key=f"repeat_{entry['id']}"):
                    st.session_state.generated_sql = {
                        "query": entry["sql_query"],
                        "formatted": entry["sql_query"],
                        "natural_query": entry["natural_query"],
                        "dialect": entry.get("dialect", ""),
                        "timestamp": datetime.now()
                    }
                    st.success("✅ Запрос загружен для повторения")
                    st.rerun()
    
    # Агентный аудит и последний запуск
    st.markdown("---")
    st.markdown("## 🤖 Агентный аудит и последние запуски")

    # Показ последнего агентного запуска
    last_run = st.session_state.get("agent_run")
    if last_run:
        with st.expander("🧠 Последний агентный запуск", expanded=False):
            st.info(f"Режим: {'Агентный' if last_run.get('mode')=='agent' else 'YAML'}")
            st.info(f"Сессия: {last_run.get('session_id')}")
            st.info(f"Запрос: {last_run.get('natural_query', '')[:120]}")
            if last_run.get('mode') == 'yaml' and last_run.get('summary'):
                st.json(last_run['summary'])
            elif last_run.get('mode') == 'agent' and last_run.get('report'):
                st.text(last_run['report'][:5000])
    else:
        st.caption("Пока нет данных о последних агентных запусках")

    # Просмотр audit лога (если доступен)
    logs_path = Path(project_root) / "logs" / "audit.log"
    if logs_path.exists():
        with st.expander("📜 Audit log (последние строки)", expanded=False):
            try:
                # Читаем последние ~2000 символов для производительности
                with open(logs_path, 'r', encoding='utf-8', errors='ignore') as f:
                    f.seek(0, 2)
                    file_size = f.tell()
                    read_size = min(4000, file_size)
                    f.seek(file_size - read_size if file_size > read_size else 0)
                    tail = f.read()
                st.text(tail)
            except Exception as e:
                st.warning(f"Не удалось прочитать audit.log: {e}")
    else:
        st.caption("Файл audit.log не найден")

    # Очистка истории
    if st.button("🧹 Очистить историю"):
        st.session_state.sql_history = []
        st.success("✅ История очищена")
        st.rerun()

if __name__ == "__main__":
    main()
