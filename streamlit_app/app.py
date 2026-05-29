"""
Главная точка входа для Streamlit приложения MultiAgent
======================================================

Многостраничное приложение для управления агентами, пайплайнами и системой.
"""

import streamlit as st
import sys
import warnings
from pathlib import Path
import time

# Подавляем предупреждения Streamlit в многопоточной среде
warnings.filterwarnings('ignore', message='.*missing ScriptRunContext.*')
warnings.filterwarnings('ignore', message='.*This warning can be ignored when running in bare mode.*')

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
from telemetry.helpers import is_trace_completed

def get_agent_manager():
    """Получить AgentManager с глобальным состоянием"""
    from agent_streamlit_api import AgentManager
    return AgentManager()

def get_workflow_manager():
    """Получить WorkflowManager с глобальным состоянием"""
    from workflow.streamlit_api import WorkflowManager
    return WorkflowManager()

def main():
    """Главная функция приложения"""
    
    # Настройка страницы
    st.set_page_config(
        page_title="MultiAgent System",
        page_icon="🤖",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    # Проверяем состояние инициализации
    if "app_initialized" not in st.session_state:
        show_initialization_screen()
    else:
        # Заголовок приложения
        st.title("🤖 MultiAgent System")
        st.markdown("---")
        
        # Проверка активации виртуального окружения
        venv_check()
        
        # Кнопка для сброса инициализации (в боковой панели)
        with st.sidebar:
            st.markdown("---")
            if st.button("🔄 Переинициализировать систему", help="Сбросить состояние инициализации и перезапустить"):
                st.session_state.clear()
                st.rerun()
        
        # Основное содержимое главной страницы
        show_dashboard()

def show_initialization_screen():
    """Экран инициализации с прогресс-баром"""
    import time
    
    st.title("🤖 MultiAgent System")
    st.markdown("### 🔄 Инициализация системы...")
    
    # Создаем прогресс бар и статус
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    # Этапы инициализации
    initialization_steps = [
        ("🔧 Загрузка конфигурации", initialize_config),
        ("🤖 Инициализация агентов", initialize_agents),
        ("🧠 Настройка системы памяти", initialize_memory),
        ("🔌 Проверка плагинов БД", initialize_db_plugins),
        ("⚙️ Подготовка интерфейса", initialize_ui_components),
    ]
    
    total_steps = len(initialization_steps)
    
    try:
        for i, (step_name, step_function) in enumerate(initialization_steps):
            status_text.text(f"📝 {step_name}...")
            progress_bar.progress((i) / total_steps)
            
            # Выполняем этап инициализации
            step_function()
            
            # Обновляем прогресс
            progress_bar.progress((i + 1) / total_steps)
            time.sleep(0.1)  # Небольшая пауза для UX
        
        # Завершение инициализации
        status_text.text("✅ Инициализация завершена!")
        progress_bar.progress(1.0)
        
        # Устанавливаем флаг инициализации
        st.session_state.app_initialized = True
        
        # Небольшая пауза перед перезагрузкой
        time.sleep(0.5)
        st.rerun()
        
    except Exception as e:
        status_text.text(f"❌ Ошибка инициализации: {e}")
        st.error(f"Произошла ошибка при инициализации: {e}")
        
        if st.button("🔄 Попробовать снова"):
            st.rerun()

def initialize_config():
    """Инициализация конфигурации"""
    try:
        from configuration_api import ConfigurationManager
        config_manager = ConfigurationManager()
        # Загружаем конфигурацию
        config = config_manager.get_config()
        st.session_state.config_loaded = True
        st.session_state.config_manager = config_manager
    except Exception as e:
        st.warning(f"Предупреждение при загрузке конфигурации: {e}")
        st.session_state.config_loaded = False

def initialize_agents():
    """Инициализация системы агентов"""
    try:
        # Загружаем профили агентов
        agent_manager = get_agent_manager()
        profiles = agent_manager.list_agents()
        st.session_state.agents_count = len(profiles)
        st.session_state.agents_loaded = True
    except Exception as e:
        st.warning(f"Предупреждение при инициализации агентов: {e}")
        st.session_state.agents_loaded = False

def initialize_memory():
    """Инициализация системы памяти"""
    try:
        # Это самый долгий этап - загрузка модели embeddings
        from memory.streamlit_api import get_memory_rag_manager
        memory_manager = get_memory_rag_manager()
        status = memory_manager.get_memory_status()
        st.session_state.memory_available = status.sqlite_available and status.chromadb_available
        st.session_state.memory_loaded = True
    except Exception as e:
        st.warning(f"Предупреждение при инициализации памяти: {e}")
        st.session_state.memory_loaded = False

def initialize_db_plugins():
    """Инициализация плагинов БД"""
    try:
        from db_plugins.streamlit_api import get_db_plugin_manager
        db_manager = get_db_plugin_manager()
        plugins = db_manager.list_plugins()
        st.session_state.db_plugins_count = len(plugins)
        st.session_state.db_plugins_loaded = True
    except Exception as e:
        st.warning(f"Предупреждение при инициализации плагинов БД: {e}")
        st.session_state.db_plugins_loaded = False

def initialize_ui_components():
    """Инициализация компонентов UI"""
    try:
        # Сохраняем timestamp инициализации
        import datetime
        st.session_state.initialization_time = datetime.datetime.now()
        st.session_state.ui_loaded = True
    except Exception as e:
        st.warning(f"Предупреждение при инициализации UI: {e}")
        st.session_state.ui_loaded = False

def venv_check():
    """Проверка активации виртуального окружения"""
    import os
    
    venv_active = os.environ.get('VIRTUAL_ENV') is not None
    
    if not venv_active:
        st.error(
            "⚠️ **Виртуальное окружение не активировано!**\n\n"
            "Перед запуском активируйте окружение:\n"
            "```bash\n"
            "source .venv/bin/activate\n"
            "```"
        )
        st.stop()
    else:
        with st.sidebar:
            st.success("✅ Virtual environment активировано")

def show_dashboard():
    """Отображение дашборда"""
    
    st.markdown("## 📊 Дашборд системы")
    
    # Информация о времени инициализации
    if hasattr(st.session_state, 'initialization_time'):
        init_time = st.session_state.initialization_time
        st.success(f"✅ Система инициализирована: {init_time.strftime('%H:%M:%S')}")
    
    # Создаем колонки для метрик
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        agents_count = getattr(st.session_state, 'agents_count', 0)
        st.metric(
            label="🤖 Доступные агенты",
            value=agents_count,
            delta=f"+{agents_count}" if agents_count > 0 else "0"
        )
    
    with col2:
        db_plugins_count = getattr(st.session_state, 'db_plugins_count', 0)
        st.metric(
            label="🔌 Плагины БД", 
            value=db_plugins_count,
            delta=f"+{db_plugins_count}" if db_plugins_count > 0 else "0"
        )
    
    with col3:
        memory_available = getattr(st.session_state, 'memory_available', False)
        st.metric(
            label="🧠 Система памяти",
            value="✅" if memory_available else "❌",
            delta="Готова" if memory_available else "Недоступна"
        )
    
    with col4:
        config_loaded = getattr(st.session_state, 'config_loaded', False)
        st.metric(
            label="⚙️ Конфигурация",
            value="✅" if config_loaded else "❌",
            delta="Загружена" if config_loaded else "Ошибка"
        )
    
    # Быстрые действия
    st.markdown("## ⚡ Быстрые действия")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        if st.button("🚀 Запустить пайплайн", use_container_width=True):
            st.switch_page("pages/02_Workflows.py")
    
    with col2:
        if st.button("🤖 Создать агента", use_container_width=True):
            st.switch_page("pages/03_Agents.py")
    
    with col3:
        if st.button("🔍 Text-to-SQL", use_container_width=True):
            st.switch_page("pages/05_Text_to_SQL.py")
    
    # Метрики активных запусков
    show_active_runs_metrics()
    
    # Последние активности
    show_recent_activities()
    
    # Статус системы
    with st.expander("🔧 Статус системы", expanded=False):
        show_system_status()

def show_system_status():
    """Отображение статуса системы"""
    
    try:
        # Проверяем доступность API
        from configuration_api import get_configuration_manager
        config_manager = get_configuration_manager()
        config = config_manager.get_config()
        
        st.markdown("### ⚙️ Конфигурация")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("**🔄 Телеметрия:**")
            if config.telemetry.enabled:
                st.success("✅ Включена")
            else:
                st.warning("⚠️ Отключена")
            
            st.markdown("**📝 Логирование:**")
            st.info(f"Уровень: {config.logging.level}")
            
            st.markdown("**🤖 LLM:**")
            st.info(f"Провайдер: {config.llm.provider}")
            st.info(f"Модель: {config.llm.model}")
        
        with col2:
            st.markdown("**🔒 Безопасность:**")
            if config.security.sql_execution_enabled:
                st.warning("⚠️ Выполнение SQL включено")
            else:
                st.success("✅ Выполнение SQL отключено")
            
            st.info(f"Уровень: {config.security.safety_level}")
            
            st.markdown("**📊 Лимиты:**")
            st.info(f"Workflows: {config.resource_limits.max_concurrent_workflows}")
            st.info(f"Agents: {config.resource_limits.max_concurrent_agents}")
        
    except Exception as e:
        st.error(f"❌ Ошибка получения статуса: {e}")

def show_active_runs_metrics():
    """Отображение метрик активных запусков"""
    
    st.markdown("## ⚡ Активные запуски")
    
    try:
        agent_manager = get_agent_manager()
        wf_manager = get_workflow_manager()
        
        # Подсчитываем активные запуски
        active_agents = len([run for run in agent_manager.active_runs.values() 
                           if run.get("status") == "running"])
        active_workflows = len([run for run in wf_manager.active_runs.values() 
                              if run.get("status") == "running"])

        # Fallback: считаем активные по сохраненным в session_state идентификаторам
        if active_agents == 0 and "agent_runs" in st.session_state:
            try:
                running = 0
                for rid in st.session_state.agent_runs.keys():
                    status = agent_manager.get_agent_status(rid)
                    if status and status.status == "running":
                        running += 1
                active_agents = running
            except Exception:
                pass
        if active_workflows == 0 and "workflow_runs" in st.session_state:
            try:
                running = 0
                for rid in st.session_state.workflow_runs.keys():
                    status = wf_manager.get_workflow_status(rid)
                    if status and status.status == "running":
                        running += 1
                active_workflows = running
            except Exception:
                pass
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric(
                label="🤖 Запущенные агенты",
                value=active_agents,
                delta=f"{active_agents} запущено" if active_agents > 0 else "0 запущено"
            )
        
        with col2:
            st.metric(
                label="🔄 Активные пайплайны", 
                value=active_workflows,
                delta=f"{active_workflows} выполняется" if active_workflows > 0 else "0 выполняется"
            )
        
        with col3:
            total_active = active_agents + active_workflows
            st.metric(
                label="📊 Всего активных",
                value=total_active,
                delta=f"{total_active} запущено" if total_active > 0 else "Нет активных"
            )
            if total_active == 0:
                # Подсказка: после перезапуска активные запуски не восстанавливаются
                try:
                    from telemetry import get_telemetry_manager
                    tm = get_telemetry_manager()
                    recent_traces = tm.get_trace_files()[:3]
                    # Исключаем служебную трассу unknown
                    recent_traces = [t for t in recent_traces if t.get("run_id") != "unknown"]
                    if recent_traces:
                        st.caption(f"Недавние трассы: {', '.join([t['run_id'][:8] for t in recent_traces])}")
                except Exception:
                    pass
            
    except Exception as e:
        st.error(f"❌ Ошибка загрузки метрик: {e}")

def show_recent_activities():
    """Последние активности"""
    
    st.markdown("## 📝 Последние активности")
    
    try:
        agent_manager = get_agent_manager()
        wf_manager = get_workflow_manager()
        
        # Объединяем активности из разных источников
        activities = []
        
        # Активности пайплайнов
        from datetime import datetime
        for run_id, run_data in list(wf_manager.active_runs.items())[-5:]:
            # Получаем топик из parameters или user_input
            params = run_data.get("parameters", {}) or run_data.get("user_input", {})
            topic = ""
            if isinstance(params, dict):
                topic = params.get("topic", "")
            elif isinstance(params, str):
                topic = params[:30]
            
            # Формируем название с топиком если есть
            workflow_name = run_data.get('workflow_name', 'Unknown')
            title = f"Пайплайн: {workflow_name}"
            if topic:
                title += f" ({topic[:20]}...)" if len(topic) > 20 else f" ({topic})"
            
            # Проверяем реальный статус из телеметрии
            real_status = run_data.get("status", "unknown")
            try:
                from telemetry import get_telemetry_manager
                tm = get_telemetry_manager()
                trace_data = tm.load_trace_file(run_id)
                spans = trace_data.get("spans", [])
                if spans:
                    has_errors = any(s.get("status", {}).get("status_code") == "ERROR" for s in spans)
                    if has_errors:
                        real_status = "failed"
                    elif is_trace_completed(spans):
                        real_status = "completed"
                    else:
                        real_status = "running"
            except Exception:
                pass
            # Приоритет статуса менеджера
            try:
                mgr_status = wf_manager.get_workflow_status(run_id)
                if mgr_status and mgr_status.status == "cancelled":
                    real_status = "cancelled"
            except Exception:
                pass
            
            activities.append({
                "time": run_data.get("start_time", datetime.now()),
                "type": "workflow",
                "icon": "🔄",
                "title": title,
                "status": real_status,
                "run_id": run_id
            })
        
        # Активности агентов
        for run_id, run_data in list(agent_manager.active_runs.items())[-5:]:
            # Проверяем реальный статус из телеметрии
            real_status = run_data.get("status", "unknown")
            try:
                from telemetry import get_telemetry_manager
                tm = get_telemetry_manager()
                trace_data = tm.load_trace_file(run_id)
                spans = trace_data.get("spans", [])
                if spans:
                    has_errors = any(s.get("status", {}).get("status_code") == "ERROR" for s in spans)
                    if has_errors:
                        real_status = "failed"
                    elif is_trace_completed(spans):
                        real_status = "completed"
                    else:
                        real_status = "running"
            except Exception:
                pass
            # Приоритет статуса менеджера
            try:
                mgr_status = agent_manager.get_agent_status(run_id)
                if mgr_status and mgr_status.status == "cancelled":
                    real_status = "cancelled"
            except Exception:
                pass
                
            activities.append({
                "time": run_data.get("start_time", datetime.now()),
                "type": "agent",
                "icon": "🤖",
                "title": f"Агент: {run_data.get('profile_name', 'Unknown')}",
                "status": real_status,
                "run_id": run_id
            })
        
        # Fallback: используем данные из session_state, если список пуст
        if not activities:
            try:
                from datetime import datetime
                # Workflows из состояния - с проверкой телеметрии
                for rid, info in (st.session_state.get("workflow_runs") or {}).items():
                    status = wf_manager.get_workflow_status(rid)
                    real_status = status.status if status else "unknown"
                    
                    # Проверяем реальный статус из телеметрии
                    try:
                        from telemetry import get_telemetry_manager
                        tm = get_telemetry_manager()
                        trace_data = tm.load_trace_file(rid)
                        spans = trace_data.get("spans", [])
                        if spans:
                            has_errors = any(s.get("status", {}).get("status_code") == "ERROR" for s in spans)
                            if has_errors:
                                real_status = "failed"
                            elif is_trace_completed(spans):
                                real_status = "completed"
                            else:
                                real_status = "running"
                    except Exception:
                        pass
                    
                    activities.append({
                        "time": info.get("start_time", datetime.now()),
                        "type": "workflow",
                        "icon": "🔄",
                        "title": f"Пайплайн: {info.get('workflow_name', 'Unknown')}",
                        "status": real_status,
                        "run_id": rid
                    })
                    
                # Agents из состояния - с проверкой телеметрии
                for rid, info in (st.session_state.get("agent_runs") or {}).items():
                    status = agent_manager.get_agent_status(rid)
                    real_status = status.status if status else "unknown"
                    
                    # Проверяем реальный статус из телеметрии
                    try:
                        from telemetry import get_telemetry_manager
                        tm = get_telemetry_manager()
                        trace_data = tm.load_trace_file(rid)
                        spans = trace_data.get("spans", [])
                        if spans:
                            has_errors = any(s.get("status", {}).get("status_code") == "ERROR" for s in spans)
                            if has_errors:
                                real_status = "failed"
                            elif is_trace_completed(spans):
                                real_status = "completed"
                            else:
                                real_status = "running"
                    except Exception:
                        pass
                    
                    activities.append({
                        "time": info.get("start_time", datetime.now()),
                        "type": "agent",
                        "icon": "🤖",
                        "title": f"Агент: {info.get('profile_name', 'Unknown')}",
                        "status": real_status,
                        "run_id": rid
                    })
            except Exception:
                pass

        # Если все еще пусто — добираем из телеметрии (последние трассы)
        if not activities:
            try:
                from telemetry import get_telemetry_manager
                tm = get_telemetry_manager()
                trace_files = tm.get_trace_files()
                # Исключаем служебную трассу unknown
                trace_files = [tf for tf in trace_files if tf.get("run_id") != "unknown"]
                for tf in trace_files[:10]:
                    run_id = tf.get("run_id")
                    modified = tf.get("modified_time")
                    # Загружаем детали, чтобы попытаться определить тип
                    try:
                        trace_data = tm.load_trace_file(run_id)
                        spans = trace_data.get("spans", [])
                    except Exception:
                        spans = []
                    title = f"Запуск: {run_id[:12]}..."
                    atype = "run"
                    icon = "📄"
                    
                    # Определяем статус и тип по spans
                    status = "unknown"
                    if spans:
                        # Проверяем статус
                        has_errors = any(s.get("status", {}).get("status_code") == "ERROR" for s in spans)
                        if has_errors:
                            status = "failed"
                        elif is_trace_completed(spans):
                            status = "completed"
                        else:
                            status = "running"
                        
                        # Инферируем тип по атрибутам
                        for sp in spans:
                            attrs = sp.get("attributes", {}) or {}
                            if attrs.get("pipeline_name"):
                                atype = "workflow"
                                icon = "🔄"
                                title = f"Пайплайн: {attrs.get('pipeline_name')}"
                                break
                            if attrs.get("agent_name"):
                                atype = "agent"
                                icon = "🤖"
                                title = f"Агент: {attrs.get('agent_name')}"
                                break
                    activities.append({
                        "time": modified,
                        "type": atype,
                        "icon": icon,
                        "title": title,
                        "status": status,
                        "run_id": run_id
                    })
            except Exception:
                pass

        # Сортируем по времени
        activities.sort(key=lambda x: x["time"], reverse=True)
        
        if activities:
            for activity in activities[:10]:
                col1, col2, col3, col4, col5 = st.columns([1, 3, 2, 1, 1])
                
                with col1:
                    st.write(activity["icon"])
                
                with col2:
                    st.write(activity["title"])
                
                with col3:
                    status_color = {
                        "running": "🟡 Выполняется",
                        "completed": "🟢 Завершено",
                        "failed": "🔴 Ошибка",
                        "cancelled": "⚫ Отменено"
                    }
                    st.write(status_color.get(activity["status"], "⚪ Неизвестно"))
                
                with col4:
                    time_str = activity["time"].strftime("%H:%M") if isinstance(activity["time"], datetime) else "N/A"
                    st.write(time_str)
                
                with col5:
                    try:
                        if activity["status"] == "running":
                            if activity["type"] == "workflow":
                                from workflow.streamlit_api import WorkflowManager
                                wf_manager = get_workflow_manager()
                                if st.button("⏹️", key=f"app_cancel_wf_{activity['run_id']}", help="Отменить"):
                                    if wf_manager.cancel_workflow(activity["run_id"]):
                                        st.success("✅ Отменено")
                                        st.rerun()
                            elif activity["type"] == "agent":
                                from agent_streamlit_api import AgentManager
                                agent_manager = get_agent_manager()
                                if st.button("⏹️", key=f"app_cancel_ag_{activity['run_id']}", help="Отменить"):
                                    if agent_manager.cancel_agent_run(activity["run_id"]):
                                        st.success("✅ Отменено")
                                        st.rerun()
                    except Exception:
                        pass
        else:
            st.info("📭 Нет недавних активностей")
    
    except Exception as e:
        st.error(f"❌ Ошибка загрузки активностей: {e}")

if __name__ == "__main__":
    main()
