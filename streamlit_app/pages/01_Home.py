"""
Главная страница - дашборд системы
==================================
"""

import streamlit as st
import sys
from pathlib import Path
from datetime import datetime, timedelta
import json

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent.parent
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
    st.set_page_config(
        page_title="Dashboard - MultiAgent System",
        page_icon="📊",
        layout="wide"
    )
    
    st.title("📊 Дашборд системы")
    st.markdown("---")
    
    # Основные метрики
    show_metrics()
    
    # Быстрые действия
    show_quick_actions()
    
    # Последние активности
    show_recent_activities()
    
    # Статус системы
    show_system_overview()

def show_metrics():
    """Отображение основных метрик"""
    
    st.markdown("## 📈 Основные метрики")
    
    col1, col2, col3, col4 = st.columns(4)
    
    try:
        # Получаем данные из API
        from workflow.streamlit_api import WorkflowManager
        from agent_streamlit_api import AgentManager
        from memory.streamlit_api import get_memory_rag_manager
        from telemetry import get_telemetry_manager
        
        wf_manager = get_workflow_manager()
        agent_manager = get_agent_manager()
        memory_manager = get_memory_rag_manager()
        telemetry_manager = get_telemetry_manager()
        
        # Метрика пайплайнов
        workflows = wf_manager.list_workflows()
        active_workflows = len([run for run in wf_manager.active_runs.values() 
                              if run.get("status") == "running"])
        # Fallback по session_state
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
        
        with col1:
            st.metric(
                label="🔄 Доступные пайплайны",
                value=len(workflows),
                help="Количество YAML пайплайнов в системе"
            )
            st.metric(
                label="⚡ Активные пайплайны",
                value=active_workflows,
                delta=f"{active_workflows} запущено"
            )
        
        # Метрика агентов
        agents = agent_manager.list_agents()
        active_agents = len([run for run in agent_manager.active_runs.values() 
                           if run.get("status") == "running"])
        # Fallback по session_state
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
        
        with col2:
            st.metric(
                label="🤖 Профили агентов",
                value=len(agents),
                help="Количество доступных профилей агентов"
            )
            st.metric(
                label="🏃‍♂️ Запущенные агенты",
                value=active_agents,
                delta=f"{active_agents} активных"
            )
        
        # Метрика памяти
        memory_status = memory_manager.get_memory_status()
        
        with col3:
            st.metric(
                label="💾 Тактическая память",
                value=memory_status.tactical_memories_count,
                help="Записей в тактической памяти"
            )
            st.metric(
                label="🎯 Стратегическая память",
                value=memory_status.strategic_memories_count,
                help="Записей в стратегической памяти"
            )
        
        # Метрика телеметрии
        trace_files = telemetry_manager.get_trace_files() if telemetry_manager.is_enabled() else []
        # Исключаем служебную трассу unknown
        trace_files = [tf for tf in trace_files if tf.get("run_id") != "unknown"]
        
        with col4:
            st.metric(
                label="🔍 Файлы трасс",
                value=len(trace_files),
                help="Количество файлов телеметрии"
            )
            if memory_status.database_size_mb:
                st.metric(
                    label="💽 Размер БД",
                    value=f"{memory_status.database_size_mb} MB",
                    help="Размер базы данных памяти"
                )
            else:
                st.metric("💽 Размер БД", "N/A")
        
    except Exception as e:
        st.error(f"❌ Ошибка загрузки метрик: {e}")
        
        # Fallback метрики
        col1.metric("🔄 Пайплайны", "N/A")
        col2.metric("🤖 Агенты", "N/A") 
        col3.metric("💾 Память", "N/A")
        col4.metric("🔍 Трассы", "N/A")

def show_quick_actions():
    """Быстрые действия"""
    
    st.markdown("## ⚡ Быстрые действия")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        if st.button("🚀 Запустить пайплайн", use_container_width=True, type="primary"):
            st.switch_page("pages/02_Workflows.py")
        
        if st.button("📋 Просмотр пайплайнов", use_container_width=True):
            st.switch_page("pages/02_Workflows.py")
    
    with col2:
        if st.button("🤖 Создать агента", use_container_width=True, type="primary"):
            st.switch_page("pages/03_Agents.py")
        
        if st.button("🔧 Динамический агент", use_container_width=True):
            st.switch_page("pages/04_Dynamic_Agents.py")
    
    with col3:
        if st.button("🔍 Text-to-SQL", use_container_width=True, type="primary"):
            st.switch_page("pages/05_Text_to_SQL.py")
        
        if st.button("🔌 DB плагины", use_container_width=True):
            st.switch_page("pages/06_DB_Plugins.py")
    
    with col4:
        if st.button("🧠 Управление памятью", use_container_width=True, type="primary"):
            st.switch_page("pages/07_Memory_RAG.py")
        
        if st.button("⚙️ Настройки", use_container_width=True):
            st.switch_page("pages/10_Settings.py")

def show_recent_activities():
    """Последние активности"""
    
    st.markdown("## 📝 Последние активности")
    
    try:
        # Получаем последние запуски
        from workflow.streamlit_api import WorkflowManager
        from agent_streamlit_api import AgentManager
        
        wf_manager = get_workflow_manager()
        agent_manager = get_agent_manager()
        
        # Объединяем активности из разных источников
        activities = []
        
        # Активности пайплайнов
        for run_id, run_data in list(wf_manager.active_runs.items())[-5:]:
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
            # Приоритет: если менеджер пометил как отменённый — показываем cancelled
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
                "title": f"Пайплайн: {run_data.get('workflow_name', 'Unknown')}",
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
        
        # Fallback: если активностей нет, пробуем из session_state
        if not activities:
            try:
                for rid, info in (st.session_state.get("workflow_runs") or {}).items():
                    activities.append({
                        "time": info.get("start_time", datetime.now()),
                        "type": "workflow",
                        "icon": "🔄",
                        "title": f"Пайплайн: {info.get('workflow_name', 'Unknown')}",
                        "status": "unknown",
                        "run_id": rid
                    })
                for rid, info in (st.session_state.get("agent_runs") or {}).items():
                    activities.append({
                        "time": info.get("start_time", datetime.now()),
                        "type": "agent",
                        "icon": "🤖",
                        "title": f"Агент: {info.get('profile_name', 'Unknown')}",
                        "status": "unknown",
                        "run_id": rid
                    })
            except Exception:
                pass

        # Если пусто — попробуем подхватить из телеметрии
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
                    title = f"Запуск: {run_id[:12]}..."
                    icon = "📄"
                    atype = "run"
                    try:
                        trace_data = tm.load_trace_file(run_id)
                        for sp in trace_data.get("spans", []):
                            attrs = sp.get("attributes", {}) or {}
                            if attrs.get("pipeline_name"):
                                title = f"Пайплайн: {attrs.get('pipeline_name')}"
                                icon = "🔄"
                                atype = "workflow"
                                break
                            if attrs.get("agent_name"):
                                title = f"Агент: {attrs.get('agent_name')}"
                                icon = "🤖"
                                atype = "agent"
                                break
                    except Exception:
                        pass
                    # Определяем статус из телеметрии
                    status = "unknown"
                    try:
                        trace_data = tm.load_trace_file(run_id)
                        spans = trace_data.get("spans", [])
                        if spans:
                            # Проверяем статус
                            has_errors = any(s.get("status", {}).get("status_code") == "ERROR" for s in spans)
                            if has_errors:
                                status = "failed"
                            elif is_trace_completed(spans):
                                status = "completed"
                            else:
                                status = "running"
                    except Exception:
                        pass
                    
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
            for activity in activities[:10]:  # Показываем последние 10
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
                                if st.button("⏹️", key=f"home_cancel_wf_{activity['run_id']}", help="Отменить"):
                                    if wf_manager.cancel_workflow(activity["run_id"]):
                                        st.success("✅ Отменено")
                                        st.rerun()
                            elif activity["type"] == "agent":
                                from agent_streamlit_api import AgentManager
                                agent_manager = get_agent_manager()
                                if st.button("⏹️", key=f"home_cancel_ag_{activity['run_id']}", help="Отменить"):
                                    if agent_manager.cancel_agent_run(activity["run_id"]):
                                        st.success("✅ Отменено")
                                        st.rerun()
                    except Exception:
                        pass
        else:
            st.info("Нет недавних активностей")
    
    except Exception as e:
        st.error(f"❌ Ошибка загрузки активностей: {e}")

def show_system_overview():
    """Обзор системы"""
    
    st.markdown("## 🔧 Обзор системы")
    
    tab1, tab2, tab3 = st.tabs(["🔧 Конфигурация", "💾 Память", "📊 Телеметрия"])
    
    with tab1:
        show_configuration_status()
    
    with tab2:
        show_memory_status()
    
    with tab3:
        show_telemetry_status()

def show_configuration_status():
    """Статус конфигурации"""
    
    try:
        from configuration_api import get_configuration_manager
        
        config_manager = get_configuration_manager()
        config = config_manager.get_config()
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("**🤖 LLM Конфигурация**")
            st.info(f"Провайдер: {config.llm.provider}")
            st.info(f"Модель: {config.llm.model}")
            
            if st.button("🧪 Тест соединения LLM"):
                with st.spinner("Тестирование..."):
                    result = config_manager.test_llm_connection()
                
                if result["success"]:
                    st.success(f"✅ Соединение успешно ({result['response_time_ms']}ms)")
                else:
                    st.error(f"❌ Ошибка: {result['error_message']}")
        
        with col2:
            st.markdown("**🔒 Безопасность**")
            
            if config.security.sql_execution_enabled:
                st.warning("⚠️ Выполнение SQL включено")
            else:
                st.success("✅ Выполнение SQL отключено")
            
            st.info(f"Уровень безопасности: {config.security.safety_level}")
            st.info(f"Макс. строк SQL: {config.security.max_sql_rows}")
        
        # Ресурсные лимиты
        st.markdown("**📊 Лимиты ресурсов**")
        limits_col1, limits_col2 = st.columns(2)
        
        with limits_col1:
            st.metric("Макс. workflows", config.resource_limits.max_concurrent_workflows)
            st.metric("Макс. agents", config.resource_limits.max_concurrent_agents)
        
        with limits_col2:
            st.metric("Лимит памяти (MB)", config.resource_limits.memory_limit_mb)
            st.metric("Таймаут (мин)", config.resource_limits.execution_timeout_minutes)
    
    except Exception as e:
        st.error(f"❌ Ошибка загрузки конфигурации: {e}")

def show_memory_status():
    """Статус системы памяти"""
    
    try:
        from memory.streamlit_api import get_memory_rag_manager
        
        memory_manager = get_memory_rag_manager()
        status = memory_manager.get_memory_status()
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("**💾 SQLite**")
            if status.sqlite_available:
                st.success("✅ Доступна")
                st.info(f"Путь: {status.sqlite_path}")
                st.info(f"Размер: {status.database_size_mb} MB")
            else:
                st.error("❌ Недоступна")
            
            st.markdown("**📊 Статистика**")
            st.metric("Тактические записи", status.tactical_memories_count)
            st.metric("Стратегические записи", status.strategic_memories_count)
        
        with col2:
            st.markdown("**🔍 ChromaDB**")
            if status.chromadb_available:
                st.success("✅ Доступна")
                st.info(f"Путь: {status.chromadb_path}")
                
                if status.collections_info:
                    for coll_name, coll_info in status.collections_info.items():
                        st.metric(f"Коллекция {coll_name}", coll_info.get("count", 0))
            else:
                st.warning("⚠️ Недоступна")
            
            st.markdown("**🧠 Embeddings**")
            if status.embedding_model_available:
                st.success("✅ Доступна")
                st.info(f"Модель: {status.embedding_model_name}")
            else:
                st.warning("⚠️ Недоступна")
        
        if st.button("🔄 Перестроить ChromaDB"):
            with st.spinner("Перестройка в процессе..."):
                result = memory_manager.rebuild_memory()
            
            if result.success:
                st.success(f"✅ Перестройка завершена за {result.rebuild_time_ms}ms")
                st.info(f"Восстановлено: {result.tactical_count} тактических, {result.strategic_count} стратегических записей")
                st.rerun()
            else:
                st.error(f"❌ Ошибка перестройки: {result.error_message}")
    
    except Exception as e:
        st.error(f"❌ Ошибка загрузки статуса памяти: {e}")

def show_telemetry_status():
    """Статус телеметрии"""
    
    try:
        from telemetry import get_telemetry_manager
        
        telemetry_manager = get_telemetry_manager()
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("**📊 Статус телеметрии**")
            if telemetry_manager.is_enabled():
                st.success("✅ Включена")
                
                trace_files = telemetry_manager.get_trace_files()
                st.metric("Файлы трасс", len(trace_files))
                
                if trace_files:
                    total_events = sum(f.get("events_count", 0) for f in trace_files)
                    st.metric("Всего событий", total_events)
                    
                    # Последний файл
                    latest_file = trace_files[0] if trace_files else None
                    if latest_file:
                        st.info(f"Последний: {latest_file['run_id']}")
                        st.info(f"Время: {latest_file['modified_time'].strftime('%H:%M:%S')}")
            else:
                st.warning("⚠️ Отключена")
        
        with col2:
            st.markdown("**📁 Файлы трасс**")
            
            if telemetry_manager.is_enabled():
                trace_files = telemetry_manager.get_trace_files()
                
                if trace_files:
                    for file_info in trace_files[:5]:  # Показываем 5 последних
                        with st.expander(f"🔍 {file_info['run_id']}"):
                            st.write(f"**События:** {file_info['events_count']}")
                            st.write(f"**Размер:** {file_info['size_bytes']} байт")
                            st.write(f"**Изменен:** {file_info['modified_time']}")
                else:
                    st.info("Нет файлов трасс")
        
        # Управление телеметрией
        st.markdown("**⚙️ Управление**")
        mgmt_col1, mgmt_col2 = st.columns(2)
        
        with mgmt_col1:
            if telemetry_manager.is_enabled():
                if st.button("⏹️ Отключить телеметрию"):
                    telemetry_manager.disable()
                    st.success("Телеметрия отключена")
                    st.rerun()
            else:
                if st.button("▶️ Включить телеметрию"):
                    telemetry_manager.enable()
                    st.success("Телеметрия включена")
                    st.rerun()
        
        with mgmt_col2:
            if st.button("🧹 Очистить старые трассы"):
                telemetry_manager.cleanup_old_traces(max_age_days=7)
                st.success("Старые трассы очищены")
                st.rerun()
    
    except Exception as e:
        st.error(f"❌ Ошибка загрузки статуса телеметрии: {e}")

if __name__ == "__main__":
    main()
