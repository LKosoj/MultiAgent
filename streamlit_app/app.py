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
from _dashboard_common import show_recent_activities_common
from _theme import inject_theme

def get_agent_manager():
    """Получить AgentManager с глобальным состоянием"""
    from agent_streamlit_api import AgentManager
    return AgentManager()

def get_workflow_manager():
    """Получить WorkflowManager с глобальным состоянием"""
    from workflow.streamlit_api import WorkflowManager
    return WorkflowManager()

@st.cache_data(ttl=5)
def _load_trace_cached(run_id: str):
    """Загрузка trace-файла с кэшированием на 5 секунд (снижает N+1 disk I/O при рендере)."""
    from telemetry import get_telemetry_manager
    tm = get_telemetry_manager()
    return tm.load_trace_file(run_id)

def main():
    """Главная функция приложения"""
    
    # Настройка страницы
    st.set_page_config(
        page_title="MultiAgent System",
        page_icon="🤖",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    inject_theme()

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
            if not st.session_state.get("_confirm_reinit"):
                if st.button("🔄 Переинициализировать систему", help="Сбросить состояние инициализации и перезапустить"):
                    st.session_state["_confirm_reinit"] = True
                    st.rerun()
            else:
                st.warning("Вы уверены? Все данные сессии будут сброшены.")
                col_yes, col_no = st.columns(2)
                with col_yes:
                    if st.button("✅ Да", key="reinit_confirm_yes"):
                        st.session_state.clear()
                        st.rerun()
                with col_no:
                    if st.button("❌ Нет", key="reinit_confirm_no"):
                        st.session_state["_confirm_reinit"] = False
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

    # Запускаем фоновый монитор зависших запусков (идемпотентно)
    try:
        from monitoring import get_stale_run_monitor
        get_stale_run_monitor().start()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Не удалось запустить StaleRunMonitor: {e}")

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
        )

    with col2:
        db_plugins_count = getattr(st.session_state, 'db_plugins_count', 0)
        st.metric(
            label="🔌 Плагины БД",
            value=db_plugins_count,
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
        show_recent_activities_common(
            wf_manager=wf_manager,
            agent_manager=agent_manager,
            load_trace_fn=_load_trace_cached,
            is_trace_completed_fn=is_trace_completed,
            key_prefix="app",
        )
    except Exception as e:
        st.error(f"❌ Ошибка загрузки активностей: {e}")

if __name__ == "__main__":
    main()
