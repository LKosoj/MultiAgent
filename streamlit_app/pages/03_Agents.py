"""
Страница управления Agents (агентами)
=====================================
"""

import streamlit as st
import sys
from pathlib import Path
import json
from datetime import datetime
import time
import uuid

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

def get_agent_manager():
    """Получить AgentManager с глобальным состоянием"""
    from agent_streamlit_api import AgentManager
    return AgentManager()

def main():
    st.set_page_config(
        page_title="Agents - MultiAgent System",
        page_icon="🤖",
        layout="wide"
    )
    
    st.title("🤖 Управление агентами")
    st.markdown("---")
    
    # Инициализация состояния
    init_session_state()
    
    # Главные вкладки
    tab1, tab2, tab3 = st.tabs(["📋 Профили агентов", "🚀 Запуск агента", "📊 Мониторинг"])
    
    with tab1:
        show_agent_profiles()
    
    with tab2:
        show_agent_execution()
    
    with tab3:
        show_agent_monitoring()

def init_session_state():
    """Инициализация состояния сессии"""
    if "selected_agent_profile" not in st.session_state:
        st.session_state.selected_agent_profile = None
    if "agent_runs" not in st.session_state:
        st.session_state.agent_runs = {}
    if "auto_refresh_agents" not in st.session_state:
        st.session_state.auto_refresh_agents = False

def show_agent_profiles():
    """Отображение профилей агентов"""
    
    st.markdown("## 📋 Доступные профили агентов")
    
    try:
        from agent_streamlit_api import AgentManager
        
        agent_manager = get_agent_manager()
        profiles = agent_manager.list_agents()
        
        if not profiles:
            st.warning("📭 Не найдено профилей агентов")
            st.info("Убедитесь, что файлы профилей существуют в папке agent_profiles/")
            return
        
        # Фильтры
        col1, col2, col3 = st.columns(3)
        
        with col1:
            types = ["Все"] + list(set(profile.type for profile in profiles))
            selected_type = st.selectbox("⚙️ Тип агента", types)
        
        with col2:
            models = ["Все"] + list(set(profile.model for profile in profiles if profile.model))
            selected_model = st.selectbox("🧠 Модель", models)
        
        with col3:
            search_term = st.text_input("🔍 Поиск по имени")
        
        # Фильтрация
        filtered_profiles = profiles
        
        if selected_type != "Все":
            filtered_profiles = [p for p in filtered_profiles if p.type == selected_type]
        
        if selected_model != "Все":
            filtered_profiles = [p for p in filtered_profiles if p.model == selected_model]
        
        if search_term:
            filtered_profiles = [p for p in filtered_profiles 
                               if search_term.lower() in p.name.lower() or 
                                  search_term.lower() in p.description.lower()]
        
        # Отображение профилей
        for profile in filtered_profiles:
            with st.expander(f"🤖 {profile.name}", expanded=False):
                col1, col2 = st.columns([2, 1])
                
                with col1:
                    st.markdown(f"**Описание:** {profile.description or 'Нет описания'}")
                    st.markdown(f"**Тип:** {profile.type}")
                    st.markdown(f"**Модель:** {profile.model or 'Не указана'}")
                    st.markdown(f"**Макс. шагов:** {profile.max_steps}")
                    
                    if profile.planning_interval:
                        st.markdown(f"**Интервал планирования:** {profile.planning_interval}")
                    
                    if profile.tools:
                        st.markdown("**Инструменты:**")
                        tools_str = ", ".join(profile.tools[:5])
                        if len(profile.tools) > 5:
                            tools_str += f" (+{len(profile.tools) - 5} еще)"
                        st.markdown(f"- {tools_str}")
                    
                    # Политика памяти
                    if profile.memory_policy:
                        with st.expander("🧠 Политика памяти"):
                            st.json(profile.memory_policy)
                
                with col2:
                    st.markdown(f"**Тип:** `{profile.type}`")
                    
                    if st.button(f"📋 Выбрать для запуска", key=f"select_agent_{profile.name}"):
                        st.session_state.selected_agent_profile = profile
                        st.success(f"✅ Выбран агент: {profile.name}")
                        st.rerun()
                    
                    if st.button(f"🧪 Тестовый запуск", key=f"test_{profile.name}"):
                        run_test_agent(agent_manager, profile)
                    
                    # Создание экземпляра агента
                    if st.button(f"⚡ Создать экземпляр", key=f"create_{profile.name}"):
                        try:
                            session_id = f"run-{uuid.uuid4().hex[:16]}"
                            agent_id = agent_manager.create_agent(profile.name, session_id)
                            st.success(f"✅ Создан экземпляр: {agent_id}")
                            
                            # Сохраняем в состоянии
                            if "created_agents" not in st.session_state:
                                st.session_state.created_agents = {}
                            st.session_state.created_agents[agent_id] = {
                                "profile_name": profile.name,
                                "session_id": session_id,
                                "created_time": datetime.now()
                            }
                        except Exception as e:
                            st.error(f"❌ Ошибка создания агента: {e}")
    
    except Exception as e:
        st.error(f"❌ Ошибка загрузки профилей: {e}")

def run_test_agent(agent_manager, profile):
    """Запуск тестового агента"""
    
    test_tasks = {
        "researcher": "Найди информацию о последних достижениях в области ИИ",
        "analyst": "Проанализируй текущие тренды на рынке технологий",
        "manager": "Создай план работы команды на неделю",
        "code_executor": "Напиши простую Python функцию для сортировки списка",
        "validator": "Проверь корректность данного JSON: {'test': 'value'}",
        "visualizer": "Создай диаграмму процесса разработки ПО"
    }
    
    # Подбираем тестовую задачу
    test_task = test_tasks.get(profile.name, "Выполни простую тестовую задачу")
    
    with st.spinner(f"Тестовый запуск агента {profile.name}..."):
        try:
            session_id = f"run-{uuid.uuid4().hex[:16]}"
            
            # Callback для отслеживания прогресса
            progress_container = st.container()
            
            def test_callback(run_id, event_type, data):
                with progress_container:
                    if event_type == "started":
                        st.info(f"🚀 Запуск тестового агента")
                    elif event_type == "completed":
                        st.success(f"✅ Тест завершен")
                        if data.get("result"):
                            st.markdown("**Результат:**")
                            st.text(str(data["result"])[:500] + "..." if len(str(data["result"])) > 500 else str(data["result"]))
                    elif event_type == "failed":
                        st.error(f"❌ Тест не удался: {data.get('error', 'Unknown')}")
            
            # Не генерируем run_id — используем тот, что вернет менеджер
            run_id = agent_manager.run_agent(
                agent_id_or_profile=profile.name,
                task=test_task,
                session_id=session_id,
                callback=test_callback
            )
            # Пробрасываем run_id локально через контекст и логируем старт
            try:
                from unified_logging import get_run_logger, run_id_context
                with run_id_context(run_id):
                    _rlog = get_run_logger(run_id, __name__)
                    _rlog.info(f"Старт тестового агента: {profile.name}")
            except Exception:
                pass
            
            # Ждем завершения (упрощенно)
            max_wait = 30  # 30 секунд
            waited = 0
            
            while waited < max_wait:
                status = agent_manager.get_agent_status(run_id)
                if status and status.status in ["completed", "failed", "cancelled"]:
                    break
                time.sleep(1)
                waited += 1
            
            # Показываем финальный результат
            result = agent_manager.get_agent_result(run_id)
            if result and result.final_output:
                st.markdown("**💡 Результат тестового запуска:**")
                st.info(str(result.final_output)[:1000] + "..." if len(str(result.final_output)) > 1000 else str(result.final_output))
        
        except Exception as e:
            st.error(f"❌ Ошибка тестового запуска: {e}")

def show_agent_execution():
    """Отображение интерфейса запуска агента"""
    
    st.markdown("## 🚀 Запуск агента")
    
    if not st.session_state.selected_agent_profile:
        st.info("📋 Выберите профиль агента на вкладке 'Профили агентов'")
        return
    
    profile = st.session_state.selected_agent_profile
    
    # Информация о выбранном агенте
    with st.container():
        st.markdown(f"### 🤖 Выбранный агент: **{profile.name}**")
        
        col1, col2, col3 = st.columns(3)
        with col1:
            st.info(f"**Тип:** {profile.type}")
        with col2:
            st.info(f"**Модель:** {profile.model or 'Default'}")
        with col3:
            st.info(f"**Макс. шагов:** {profile.max_steps}")
        
        st.markdown(f"**Описание:** {profile.description}")
    
    st.markdown("---")
    
    # Форма запуска
    with st.form("agent_execution_form"):
        st.markdown("### ⚙️ Параметры запуска")
        
        col1, col2 = st.columns(2)
        
        with col1:
            session_id = st.text_input(
                "🆔 Session ID",
                value=f"streamlit_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                help="Уникальный идентификатор сессии"
            )
            
            task = st.text_area(
                "📝 Задача для агента",
                height=150,
                placeholder="Опишите задачу, которую должен выполнить агент...",
                help="Подробное описание задачи для агента"
            )
        
        with col2:
            use_existing_agent = st.checkbox(
                "🔄 Использовать существующий экземпляр",
                help="Использовать ранее созданный экземпляр агента"
            )
            
            if use_existing_agent and "created_agents" in st.session_state:
                available_agents = [
                    f"{agent_id} ({info['profile_name']})" 
                    for agent_id, info in st.session_state.created_agents.items()
                    if info['profile_name'] == profile.name
                ]
                
                if available_agents:
                    selected_agent = st.selectbox(
                        "Выберите экземпляр",
                        available_agents
                    )
                    agent_id_to_use = selected_agent.split(" ")[0]
                else:
                    st.warning("Нет доступных экземпляров для этого профиля")
                    use_existing_agent = False
            
            enable_telemetry = st.checkbox(
                "📊 Включить телеметрию",
                value=True,
                help="Записывать трассы выполнения агента"
            )
            
            enable_memory = st.checkbox(
                "🧠 Включить память",
                value=True,
                help="Сохранять результаты в память агента"
            )
        
        # Кнопка запуска
        submitted = st.form_submit_button("🚀 Запустить агента", type="primary")
        
        if submitted and task:
            try:
                from agent_streamlit_api import AgentManager
                from telemetry import configure_telemetry
                
                # Настраиваем телеметрию
                if enable_telemetry:
                    configure_telemetry(enabled=True)
                
                agent_manager = get_agent_manager()
                
                # Определяем, какого агента использовать
                if use_existing_agent and "agent_id_to_use" in locals():
                    agent_to_run = agent_id_to_use
                else:
                    agent_to_run = profile.name
                
                # Callback для прогресса
                progress_placeholder = st.empty()
                result_placeholder = st.empty()
                
                def progress_callback(run_id, event_type, data):
                    with progress_placeholder.container():
                        if event_type == "started":
                            st.info(f"🚀 Запуск агента {data.get('profile_name', 'Unknown')}")
                        elif event_type == "completed":
                            st.success(f"✅ Агент завершил работу")
                            with result_placeholder.container():
                                if data.get("result"):
                                    st.markdown("### 💡 Результат выполнения:")
                                    st.markdown(str(data["result"]))
                        elif event_type == "failed":
                            st.error(f"❌ Ошибка выполнения агента: {data.get('error', 'Unknown')}")
                
                # Запускаем агента с единым run_id и логированием
                with st.spinner("Запуск агента..."):
                    # Не генерируем run_id заранее — берем его из менеджера
                    run_id = agent_manager.run_agent(
                        agent_id_or_profile=agent_to_run,
                        task=task,
                        session_id=session_id,
                        callback=progress_callback
                    )
                    # Локально подцепляем полученный run_id и пишем стартовый лог
                    try:
                        from unified_logging import get_run_logger, run_id_context
                        with run_id_context(run_id):
                            _rlog = get_run_logger(run_id, __name__)
                            _rlog.info(f"Старт агента: {agent_to_run}")
                    except Exception:
                        pass
                
                st.success(f"✅ Агент запущен с ID: `{run_id}`")
                # Сохраняем последний run_id для отображения кнопки вне формы
                st.session_state.last_agent_run_id = run_id
                
                # Сохраняем в состоянии
                st.session_state.agent_runs[run_id] = {
                    "profile_name": profile.name,
                    "start_time": datetime.now(),
                    "session_id": session_id,
                    "task": task
                }
                
                # Переключаемся на мониторинг
                time.sleep(1)
                st.rerun()
            
            except Exception as e:
                st.error(f"❌ Ошибка запуска агента: {e}")
                st.exception(e)
        
        elif submitted and not task:
            st.error("❌ Пожалуйста, введите задачу для агента")

    # Кнопка открытия трасс последнего запуска (вне формы)
    if "last_agent_run_id" in st.session_state:
        last_run_id = st.session_state.last_agent_run_id
        open_col1, open_col2 = st.columns([1, 3])
        with open_col1:
            if st.button("🔍 Открыть трассы", key=f"open_traces_{last_run_id}"):
                try:
                    st.query_params["run_id"] = last_run_id
                except Exception:
                    pass
                st.switch_page("pages/08_Logs_Traces.py")

def show_agent_monitoring():
    """Мониторинг выполнения агентов"""
    
    st.markdown("## 📊 Мониторинг агентов")
    
    try:
        from agent_streamlit_api import AgentManager
        
        agent_manager = get_agent_manager()
        
        # Автообновление
        col1, col2 = st.columns([3, 1])
        
        with col1:
            st.markdown("### 🔄 Активные агенты")
        
        with col2:
            auto_refresh = st.checkbox("🔄 Автообновление", value=st.session_state.auto_refresh_agents)
            st.session_state.auto_refresh_agents = auto_refresh
            
            # Правильная реализация автообновления
            if auto_refresh:
                import time
                # Инициализируем время последнего обновления
                if "last_refresh_time_agents" not in st.session_state:
                    st.session_state.last_refresh_time_agents = time.time()
                
                # Проверяем, прошло ли 3 секунды
                current_time = time.time()
                if current_time - st.session_state.last_refresh_time_agents >= 3:
                    st.session_state.last_refresh_time_agents = current_time
                    st.rerun()
                
                # Показываем индикатор автообновления
                next_refresh = 3 - (current_time - st.session_state.last_refresh_time_agents)
                if next_refresh > 0:
                    st.caption(f"⏱️ Обновление через {next_refresh:.1f}с")
        
        # Получаем статусы всех запусков
        if agent_manager.active_runs:
            for run_id, run_data in agent_manager.active_runs.items():
                with st.expander(f"🤖 {run_data.get('profile_name', 'Unknown')} - {run_id[:8]}", expanded=True):
                    
                    # Статус
                    status = agent_manager.get_agent_status(run_id)
                    
                    if status:
                        col1, col2, col3, col4 = st.columns(4)
                        
                        with col1:
                            status_icon = {
                                "running": "🟡",
                                "completed": "🟢",
                                "failed": "🔴",
                                "cancelled": "⚫"
                            }
                            st.metric(
                                "Статус",
                                f"{status_icon.get(status.status, '⚪')} {status.status}"
                            )
                        
                        with col2:
                            st.metric("Шагов", status.step_count)
                        
                        with col3:
                            if status.duration_seconds:
                                duration_str = f"{status.duration_seconds:.1f}s"
                            else:
                                duration_str = "В процессе..."
                            st.metric("Время", duration_str)
                        
                        with col4:
                            if status.current_step:
                                st.metric("Текущий шаг", status.current_step)
                            else:
                                st.metric("Шаг", "N/A")
                        
                        # Задача
                        if status.task:
                            st.markdown("**Задача:**")
                            st.info(status.task[:200] + "..." if len(status.task) > 200 else status.task)
                        
                        # Ошибки
                        if status.error_message:
                            st.error(f"❌ Ошибка: {status.error_message}")
                        
                        # Кнопки управления
                        action_col1, action_col2, action_col3 = st.columns(3)
                        
                        with action_col1:
                            if status.status == "running":
                                if st.button(f"⏹️ Отменить", key=f"cancel_agent_{run_id}"):
                                    if agent_manager.cancel_agent_run(run_id):
                                        st.success("✅ Агент отменен")
                                        st.rerun()
                                    else:
                                        st.error("❌ Не удалось отменить")
                            elif status.status == "cancelled":
                                st.caption("Отменено")
                        
                        with action_col2:
                            if st.button(f"📊 Результат", key=f"result_{run_id}"):
                                result = agent_manager.get_agent_result(run_id)
                                if result and result.final_output:
                                    st.markdown("**💡 Результат:**")
                                    st.text(str(result.final_output))
                                    
                                    if result.steps_history:
                                        with st.expander("📋 История шагов"):
                                            for i, step in enumerate(result.steps_history):
                                                st.markdown(f"**Шаг {i+1}:** {step}")
                                else:
                                    st.info("Результат пока недоступен")
                        
                        with action_col3:
                            if st.button(f"🔍 Трассы", key=f"traces_agent_{run_id}"):
                                st.switch_page("pages/08_Logs_Traces.py")
                    
                    else:
                        st.warning(f"⚠️ Не удалось получить статус для {run_id}")
        else:
            st.info("📭 Нет активных агентов")
        
        # Созданные экземпляры
        if "created_agents" in st.session_state and st.session_state.created_agents:
            st.markdown("### 🤖 Созданные экземпляры")
            
            for agent_id, agent_info in st.session_state.created_agents.items():
                col1, col2, col3, col4 = st.columns([2, 2, 2, 1])
                
                with col1:
                    st.text(f"ID: {agent_id[:12]}...")
                
                with col2:
                    st.text(f"Профиль: {agent_info['profile_name']}")
                
                with col3:
                    st.text(f"Создан: {agent_info['created_time'].strftime('%H:%M:%S')}")
                
                with col4:
                    if st.button("🗑️", key=f"delete_{agent_id}", help="Удалить экземпляр"):
                        del st.session_state.created_agents[agent_id]
                        st.success("✅ Экземпляр удален")
                        st.rerun()
        
        # История выполнений
        st.markdown("### 📚 История выполнений")
        
        if st.session_state.agent_runs:
            history_data = []
            for run_id, run_info in st.session_state.agent_runs.items():
                status = agent_manager.get_agent_status(run_id)
                
                history_data.append({
                    "Run ID": run_id[:8] + "...",
                    "Агент": run_info["profile_name"],
                    "Статус": status.status if status else "Unknown",
                    "Начат": run_info["start_time"].strftime("%H:%M:%S"),
                    "Задача": run_info["task"][:50] + "..." if len(run_info["task"]) > 50 else run_info["task"]
                })
            
            if history_data:
                st.dataframe(history_data, use_container_width=True)
        else:
            st.info("📭 История пуста")
        
        # Очистка истории
        if st.button("🧹 Очистить завершенные"):
            agent_manager.cleanup_completed_runs(max_age_hours=1)
            st.success("✅ Завершенные запуски очищены")
            st.rerun()
    
    except Exception as e:
        st.error(f"❌ Ошибка мониторинга агентов: {e}")

if __name__ == "__main__":
    main()
