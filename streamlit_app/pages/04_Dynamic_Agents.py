"""
Страница конструктора динамических агентов
=========================================
"""

import streamlit as st
import sys
from pathlib import Path
import json
from datetime import datetime
import yaml
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
        page_title="Dynamic Agents - MultiAgent System",
        page_icon="🔧",
        layout="wide"
    )
    
    st.title("🔧 Конструктор динамических агентов")
    st.markdown("---")
    
    # Инициализация состояния
    init_session_state()
    
    # Главные вкладки
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["🔧 Конструктор", "👥 Команда менеджера", "⚡ Активные запуски", "📊 Управление", "💾 Импорт/Экспорт"])
    
    with tab1:
        show_agent_constructor()
    
    with tab2:
        show_manager_team_builder()
    
    with tab3:
        show_active_runs_monitoring()
    
    with tab4:
        show_dynamic_agent_management()
    
    with tab5:
        show_import_export()

def init_session_state():
    """Инициализация состояния сессии"""
    if "dynamic_agent_draft" not in st.session_state:
        st.session_state.dynamic_agent_draft = None
    if "team_composition" not in st.session_state:
        st.session_state.team_composition = []
    if "saved_definitions" not in st.session_state:
        st.session_state.saved_definitions = {}

def show_agent_constructor():
    """Конструктор динамического агента"""
    
    st.markdown("## 🔧 Создание динамического агента")
    st.info("Создайте агента 'на лету' с собственными параметрами и инструментами")
    
    with st.form("dynamic_agent_form"):
        # Основные параметры
        st.markdown("### ⚙️ Основные параметры")
        
        col1, col2 = st.columns(2)
        
        with col1:
            agent_name = st.text_input(
                "🤖 Имя агента",
                placeholder="Например: CustomAnalyst",
                help="Уникальное имя для агента"
            )
            
            agent_type = st.selectbox(
                "⚙️ Тип агента",
                ["code", "tool_calling", "multi_step"],
                help="Тип агента определяет способ выполнения задач"
            )
            
            description = st.text_area(
                "📝 Описание",
                height=100,
                placeholder="Краткое описание назначения агента...",
                help="Описание роли и назначения агента"
            )
        
        with col2:
            model = st.text_input(
                "🧠 Модель LLM",
                value="gpt-4",
                help="Модель языковой модели для агента"
            )
            
            max_steps = st.number_input(
                "🔢 Максимум шагов",
                min_value=1,
                max_value=100,
                value=20,
                help="Максимальное количество шагов выполнения"
            )
            
            planning_interval = st.number_input(
                "📋 Интервал планирования",
                min_value=0,
                max_value=50,
                value=0,
                help="Интервал для пересмотра плана (0 = отключено)"
            )
        
        # Инструкции
        st.markdown("### 📋 Инструкции и промпт")
        
        instructions = st.text_area(
            "📝 Системные инструкции",
            height=200,
            placeholder="""Ты специализированный агент для выполнения задач...

Твои основные обязанности:
1. Анализ входящих данных
2. Выполнение специфических операций
3. Предоставление детальных отчетов

Всегда следуй принципам:
- Точность и внимательность к деталям
- Структурированность ответов
- Прозрачность в рассуждениях""",
            help="Детальные инструкции для агента"
        )
        
        # Выбор инструментов
        st.markdown("### 🛠️ Инструменты")
        
        # Получаем доступные инструменты
        available_tools = get_available_tools()
        
        selected_tools = st.multiselect(
            "🔧 Выберите инструменты",
            options=available_tools,
            help="Инструменты, которые будут доступны агенту"
        )
        
        # Дополнительные настройки
        with st.expander("🔧 Дополнительные настройки"):
            col1, col2 = st.columns(2)
            
            with col1:
                enable_memory = st.checkbox(
                    "🧠 Включить память",
                    value=True,
                    help="Сохранять историю выполнения задач"
                )
                
                provide_run_summary = st.checkbox(
                    "📊 Предоставлять сводку запуска",
                    value=False,
                    help="Генерировать сводку после выполнения"
                )
            
            with col2:
                max_tool_threads = st.number_input(
                    "🔀 Макс. потоков инструментов",
                    min_value=1,
                    max_value=10,
                    value=1,
                    help="Максимальное количество одновременных вызовов инструментов"
                )
        
        # Кнопки действий
        col1, col2, col3 = st.columns(3)
        
        with col1:
            preview_clicked = st.form_submit_button("👁️ Предпросмотр", type="secondary")
        
        with col2:
            create_clicked = st.form_submit_button("✅ Создать агента", type="primary")
        
        with col3:
            save_clicked = st.form_submit_button("💾 Сохранить шаблон", type="secondary")
        
        # Обработка действий
        if preview_clicked and agent_name:
            show_agent_preview(agent_name, agent_type, description, model, max_steps, 
                             planning_interval, instructions, selected_tools, 
                             enable_memory, provide_run_summary, max_tool_threads)
        
        if create_clicked and agent_name:
            create_dynamic_agent(agent_name, agent_type, description, model, max_steps,
                                planning_interval, instructions, selected_tools,
                                enable_memory, provide_run_summary, max_tool_threads)
        
        if save_clicked and agent_name:
            save_agent_template(agent_name, agent_type, description, model, max_steps,
                              planning_interval, instructions, selected_tools,
                              enable_memory, provide_run_summary, max_tool_threads)

def get_available_tools():
    """Получение списка доступных инструментов"""
    try:
        # Получаем инструменты из tool_definitions
        tool_dir = project_root / "tool_definitions"
        available_tools = []
        
        if tool_dir.exists():
            for yaml_file in tool_dir.glob("*.yaml"):
                try:
                    with open(yaml_file, 'r', encoding='utf-8') as f:
                        tool_config = yaml.safe_load(f)
                    tool_name = tool_config.get('name', yaml_file.stem)
                    available_tools.append(tool_name)
                except Exception:
                    continue
        
        # Добавляем стандартные инструменты
        standard_tools = [
            "web_search", "webpage_content", "file_read", "file_write",
            "natural_language_processing", "intent_extraction", "schema_linking",
            "sql_generation_plugin", "sql_safety_check", "secure_db_executor"
        ]
        
        available_tools.extend(standard_tools)
        return sorted(list(set(available_tools)))
    
    except Exception as e:
        st.error(f"❌ Ошибка получения инструментов: {e}")
        return ["web_search", "file_read", "file_write"]

def show_agent_preview(name, agent_type, description, model, max_steps,
                      planning_interval, instructions, tools, enable_memory,
                      provide_run_summary, max_tool_threads):
    """Предпросмотр конфигурации агента"""
    
    st.markdown("### 👁️ Предпросмотр агента")
    
    # Создаем определение агента
    agent_definition = {
        "name": name,
        "type": agent_type,
        "description": description,
        "model": model,
        "tools": tools,
        "instructions": instructions,
        "max_steps": max_steps,
        "planning_interval": planning_interval if planning_interval > 0 else None,
        "memory_policy": {
            "enable_memory": enable_memory,
            "provide_run_summary": provide_run_summary
        },
        "max_tool_threads": max_tool_threads,
        "metadata": {
            "created_at": datetime.now().isoformat(),
            "created_by": "streamlit_constructor"
        }
    }
    
    # Отображаем в виде JSON
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("**🔧 Конфигурация агента:**")
        st.json(agent_definition)
    
    with col2:
        st.markdown("**📊 Сводка:**")
        st.info(f"**Имя:** {name}")
        st.info(f"**Тип:** {agent_type}")
        st.info(f"**Модель:** {model}")
        st.info(f"**Инструментов:** {len(tools)}")
        st.info(f"**Макс. шагов:** {max_steps}")
        
        if tools:
            st.markdown("**🛠️ Инструменты:**")
            for tool in tools[:5]:
                st.markdown(f"- {tool}")
            if len(tools) > 5:
                st.markdown(f"- ... и еще {len(tools) - 5}")
    
    # Сохраняем в черновик
    st.session_state.dynamic_agent_draft = agent_definition

def create_dynamic_agent(name, agent_type, description, model, max_steps,
                        planning_interval, instructions, tools, enable_memory,
                        provide_run_summary, max_tool_threads):
    """Создание динамического агента"""
    
    try:
        from agent_streamlit_api import AgentManager, DynamicAgentDefinition
        
        # Создаем определение агента
        agent_definition = DynamicAgentDefinition(
            name=name,
            type=agent_type,
            description=description,
            model=model,
            tools=tools,
            instructions=instructions,
            max_steps=max_steps,
            planning_interval=planning_interval if planning_interval > 0 else None,
            memory_policy={
                "enable_memory": enable_memory,
                "provide_run_summary": provide_run_summary
            },
            metadata={
                "created_at": datetime.now().isoformat(),
                "created_by": "streamlit_constructor",
                "max_tool_threads": max_tool_threads
            }
        )
        
        agent_manager = get_agent_manager()
        
        # Регистрируем динамический профиль
        success = agent_manager.register_dynamic_profile(name, agent_definition)
        
        if success:
            # Создаем экземпляр агента
            session_id = f"run-{uuid.uuid4().hex[:16]}"
            agent_id = agent_manager.create_dynamic_agent(agent_definition, session_id)
            
            st.success(f"✅ Динамический агент '{name}' создан успешно!")
            st.info(f"🆔 ID агента: `{agent_id}`")
            st.info(f"🆔 Session ID: `{session_id}`")
            
            # Предлагаем тестовый запуск
            if st.button("🧪 Запустить тестовую задачу"):
                test_task = "Представься и расскажи о своих возможностях"
                
                def test_callback(run_id, event_type, data):
                    if event_type == "completed":
                        st.markdown("**💡 Результат тестового запуска:**")
                        st.info(str(data.get("result", "Нет результата")))
                
                # Не генерируем run_id заранее — используем возвращенный менеджером
                run_id = agent_manager.run_agent(
                    agent_id_or_profile=agent_id,
                    task=test_task,
                    session_id=session_id,
                    callback=test_callback
                )
                # Пробрасываем локально и логируем (без утечки RUN_ID)
                try:
                    from unified_logging import get_run_logger, run_id_context
                    with run_id_context(run_id):
                        _rlog = get_run_logger(run_id, __name__)
                        _rlog.info(f"Старт тестового динамического агента: {agent_id}")
                except Exception:
                    pass
                
                st.info(f"🚀 Тестовая задача запущена с ID: `{run_id}`")
        else:
            st.error("❌ Не удалось создать динамический агент")
    
    except Exception as e:
        st.error(f"❌ Ошибка создания агента: {e}")
        st.exception(e)

def save_agent_template(name, agent_type, description, model, max_steps,
                       planning_interval, instructions, tools, enable_memory,
                       provide_run_summary, max_tool_threads):
    """Сохранение шаблона агента"""
    
    template = {
        "name": name,
        "type": agent_type,
        "description": description,
        "model": model,
        "tools": tools,
        "instructions": instructions,
        "max_steps": max_steps,
        "planning_interval": planning_interval if planning_interval > 0 else None,
        "memory_policy": {
            "enable_memory": enable_memory,
            "provide_run_summary": provide_run_summary
        },
        "max_tool_threads": max_tool_threads,
        "metadata": {
            "template": True,
            "created_at": datetime.now().isoformat(),
            "created_by": "streamlit_constructor"
        }
    }
    
    # Сохраняем в состоянии сессии
    st.session_state.saved_definitions[name] = template
    
    st.success(f"💾 Шаблон '{name}' сохранен")

def show_manager_team_builder():
    """Конструктор команды для менеджера"""
    
    st.markdown("## 👥 Создание команды для менеджера")
    st.info("Соберите команду агентов для работы под управлением менеджера")
    
    col1, col2 = st.columns([1, 2])
    
    with col1:
        st.markdown("### 🤖 Доступные агенты")
        
        try:
            from agent_streamlit_api import AgentManager
            
            agent_manager = get_agent_manager()
            
            # Стандартные профили
            profiles = agent_manager.list_agents()
            st.markdown(f"**📋 Стандартные профили ({len(profiles)}):**")
            
            # Показываем все профили без ограничений
            for profile in profiles:
                if st.button(f"➕ {profile.name}", key=f"add_standard_{profile.name}"):
                    if profile.name not in [agent["name"] for agent in st.session_state.team_composition]:
                        st.session_state.team_composition.append({
                            "name": profile.name,
                            "type": "standard",
                            "description": profile.description,
                            "role": profile.type
                        })
                        st.rerun()
            
            # Динамические профили
            dynamic_profiles = agent_manager.list_dynamic_profiles()
            if dynamic_profiles:
                st.markdown("**🔧 Динамические профили:**")
                for profile in dynamic_profiles:
                    if st.button(f"➕ {profile.name}", key=f"add_dynamic_{profile.name}"):
                        if profile.name not in [agent["name"] for agent in st.session_state.team_composition]:
                            st.session_state.team_composition.append({
                                "name": profile.name,
                                "type": "dynamic",
                                "description": profile.description,
                                "role": profile.type
                            })
                            st.rerun()
            
            # Сохраненные шаблоны
            if st.session_state.saved_definitions:
                st.markdown("**💾 Сохраненные шаблоны:**")
                for template_name, template in st.session_state.saved_definitions.items():
                    if st.button(f"➕ {template_name}", key=f"add_template_{template_name}"):
                        if template_name not in [agent["name"] for agent in st.session_state.team_composition]:
                            st.session_state.team_composition.append({
                                "name": template_name,
                                "type": "template",
                                "description": template.get("description", ""),
                                "role": template.get("type", "")
                            })
                            st.rerun()
        
        except Exception as e:
            st.error(f"❌ Ошибка загрузки агентов: {e}")
    
    with col2:
        st.markdown("### 👥 Состав команды")
        
        if st.session_state.team_composition:
            for i, agent in enumerate(st.session_state.team_composition):
                with st.container():
                    col_info, col_remove = st.columns([4, 1])
                    
                    with col_info:
                        type_icon = {"standard": "📋", "dynamic": "🔧", "template": "💾"}
                        st.markdown(f"**{type_icon.get(agent['type'], '🤖')} {agent['name']}**")
                        st.caption(f"Тип: {agent['role']} | {agent['description'][:50]}...")
                    
                    with col_remove:
                        if st.button("❌", key=f"remove_{i}", help="Удалить из команды"):
                            st.session_state.team_composition.pop(i)
                            st.rerun()
            
            st.markdown("---")
            
            # Форма запуска команды
            with st.form("team_execution_form"):
                st.markdown("### 🚀 Запуск команды менеджера")
                
                team_task = st.text_area(
                    "📝 Задача для команды",
                    height=150,
                    placeholder="Опишите задачу, которую должна выполнить команда под руководством менеджера...",
                    help="Общая задача, которая будет распределена между членами команды"
                )
                
                col1, col2 = st.columns(2)
                
                with col1:
                    manager_type = st.selectbox(
                        "👨‍💼 Тип менеджера",
                        ["manager", "project_manager", "custom"],
                        help="Профиль менеджера для координации команды"
                    )
                    
                    # Генерируем session_id автоматически в формате run-xxxxx для консистентности
                    session_id = f"run-{uuid.uuid4().hex[:16]}"
                
                with col2:
                    enable_telemetry = st.checkbox(
                        "📊 Включить телеметрию",
                        value=True,
                        help="Записывать трассы выполнения команды"
                    )
                    
                    max_parallel = st.number_input(
                        "🔀 Макс. параллельных задач",
                        min_value=1,
                        max_value=5,
                        value=2,
                        help="Максимальное количество агентов, работающих одновременно"
                    )
                
                submitted = st.form_submit_button("🚀 Запустить команду", type="primary")
                
                if submitted and team_task:
                    try:
                        from agent_streamlit_api import AgentManager
                        from telemetry import configure_telemetry
                        
                        if enable_telemetry:
                            configure_telemetry(enabled=True)
                        
                        agent_manager = get_agent_manager()
                        
                        # Подготавливаем список агентов команды
                        team_names = [agent["name"] for agent in st.session_state.team_composition]
                        
                        # Callback для прогресса
                        progress_placeholder = st.empty()
                        
                        def team_progress_callback(run_id, event_type, data):
                            with progress_placeholder.container():
                                if event_type == "started":
                                    st.info(f"🚀 Запуск команды из {len(team_names)} агентов")
                                elif event_type == "completed":
                                    st.success(f"✅ Команда завершила работу")
                                    if data.get("result"):
                                        st.markdown("**💡 Результат команды:**")
                                        st.text(str(data["result"])[:1000] + "..." if len(str(data["result"])) > 1000 else str(data["result"]))
                                elif event_type == "failed":
                                    st.error(f"❌ Ошибка выполнения команды: {data.get('error', 'Unknown')}")
                        
                        # Запускаем команду, run_id берем из менеджера
                        with st.spinner("Запуск команды менеджера..."):
                            run_id = agent_manager.run_manager_with_team(
                                manager_definition_or_name=manager_type,
                                team_definitions_or_names=team_names,
                                task=team_task,
                                session_id=session_id,
                                callback=team_progress_callback
                            )
                            try:
                                from unified_logging import get_run_logger, run_id_context
                                with run_id_context(run_id):
                                    _rlog = get_run_logger(run_id, __name__)
                                    _rlog.info(f"Старт команды менеджера: {manager_type} -> {team_names}")
                            except Exception:
                                pass
                        
                        st.success(f"✅ Команда запущена с ID: `{run_id}`")
                        st.info(f"👥 Состав команды: {', '.join(team_names)}")
                    
                    except Exception as e:
                        st.error(f"❌ Ошибка запуска команды: {e}")
                        st.exception(e)
                
                elif submitted and not team_task:
                    st.error("❌ Пожалуйста, введите задачу для команды")
            
        else:
            st.info("👥 Команда пуста. Можно запустить менеджера без предзаданной команды — он подберёт её автоматически.")

            # Форма запуска без предзаданной команды (автоподбор менеджером)
            with st.form("team_execution_form_autoselect"):
                st.markdown("### 🚀 Запуск менеджера (автоподбор команды)")

                team_task = st.text_area(
                    "📝 Задача для менеджера",
                    height=150,
                    placeholder="Опишите задачу; менеджер сам подберёт команду под задачу...",
                    help="Менеджер выполнит разбиение задачи и подберёт нужных агентов"
                )

                col1, col2 = st.columns(2)

                with col1:
                    manager_type = st.selectbox(
                        "👨‍💼 Тип менеджера",
                        ["manager", "project_manager", "custom"],
                        help="Профиль менеджера для координации"
                    )

                    # Генерируем session_id автоматически в формате run-xxxxx для консистентности
                    session_id = f"run-{uuid.uuid4().hex[:16]}"

                with col2:
                    enable_telemetry = st.checkbox(
                        "📊 Включить телеметрию",
                        value=True,
                        help="Записывать трассы выполнения"
                    )

                    max_parallel = st.number_input(
                        "🔀 Макс. параллельных задач",
                        min_value=1,
                        max_value=5,
                        value=2,
                        help="Ограничение параллелизма (при необходимости)"
                    )

                submitted = st.form_submit_button("🚀 Запустить без команды", type="primary")

                if submitted and team_task:
                    try:
                        from telemetry import configure_telemetry
                        if enable_telemetry:
                            configure_telemetry(enabled=True)

                        agent_manager = get_agent_manager()

                        # Callback для прогресса
                        progress_placeholder = st.empty()

                        def team_progress_callback(run_id, event_type, data):
                            with progress_placeholder.container():
                                if event_type == "started":
                                    st.info("🚀 Запуск менеджера без предзаданной команды (автоподбор)")
                                elif event_type == "completed":
                                    st.success("✅ Выполнение завершено")
                                    if data.get("result"):
                                        st.markdown("**💡 Результат:**")
                                        st.text(str(data["result"])[:1000] + "..." if len(str(data["result"])) > 1000 else str(data["result"]))
                                elif event_type == "failed":
                                    st.error(f"❌ Ошибка выполнения: {data.get('error', 'Unknown')}")

                        # Запускаем менеджера без предзаданной команды (передаём пустой список)
                        with st.spinner("Запуск менеджера..."):
                            run_id = agent_manager.run_manager_with_team(
                                manager_definition_or_name=manager_type,
                                team_definitions_or_names=[],  # пустой список — автоподбор
                                task=team_task,
                                session_id=session_id,
                                callback=team_progress_callback
                            )
                            try:
                                from unified_logging import get_run_logger, run_id_context
                                with run_id_context(run_id):
                                    _rlog = get_run_logger(run_id, __name__)
                                    _rlog.info(f"Старт менеджера без команды: {manager_type}")
                            except Exception:
                                pass

                        st.success(f"✅ Запуск выполнен, ID: `{run_id}`")
                        st.info("🤝 Команда будет подобрана менеджером автоматически")

                    except Exception as e:
                        st.error(f"❌ Ошибка запуска: {e}")
                        st.exception(e)

                elif submitted and not team_task:
                    st.error("❌ Пожалуйста, введите задачу")

            if st.button("🧹 Очистить команду"):
                st.session_state.team_composition = []
                st.rerun()

def show_dynamic_agent_management():
    """Управление динамическими агентами"""
    
    st.markdown("## 📊 Управление динамическими агентами")
    
    try:
        from agent_streamlit_api import AgentManager
        
        agent_manager = get_agent_manager()
        dynamic_profiles = agent_manager.list_dynamic_profiles()
        
        if dynamic_profiles:
            st.markdown("### 🔧 Зарегистрированные динамические профили")
            
            for profile in dynamic_profiles:
                with st.expander(f"🔧 {profile.name}", expanded=False):
                    col1, col2 = st.columns([2, 1])
                    
                    with col1:
                        st.markdown(f"**Описание:** {profile.description}")
                        st.markdown(f"**Тип:** {profile.type}")
                        st.markdown(f"**Модель:** {profile.model}")
                        st.markdown(f"**Инструментов:** {len(profile.tools)}")
                        
                        if profile.tools:
                            with st.expander("🛠️ Инструменты"):
                                for tool in profile.tools:
                                    st.markdown(f"- {tool}")
                    
                    with col2:
                        if st.button(f"🚀 Запустить", key=f"run_dynamic_{profile.name}"):
                            # Переходим на страницу агентов для запуска
                            st.switch_page("pages/03_Agents.py")
                        
                        if st.button(f"📋 Экспорт", key=f"export_dynamic_{profile.name}"):
                            profile_dict = profile.to_profile_dict()
                            st.json(profile_dict)
                        
                        if st.button(f"🗑️ Удалить", key=f"delete_dynamic_{profile.name}"):
                            # Здесь можно добавить логику удаления
                            st.warning("Функция удаления будет реализована")
        else:
            st.info("📭 Нет зарегистрированных динамических профилей")
        
        # Сохраненные шаблоны
        if st.session_state.saved_definitions:
            st.markdown("### 💾 Сохраненные шаблоны")
            
            for template_name, template in st.session_state.saved_definitions.items():
                with st.expander(f"💾 {template_name}", expanded=False):
                    col1, col2 = st.columns([2, 1])
                    
                    with col1:
                        st.markdown(f"**Описание:** {template.get('description', 'N/A')}")
                        st.markdown(f"**Тип:** {template.get('type', 'N/A')}")
                        st.markdown(f"**Модель:** {template.get('model', 'N/A')}")
                        st.markdown(f"**Создан:** {template.get('metadata', {}).get('created_at', 'N/A')}")
                    
                    with col2:
                        if st.button(f"✅ Создать агента", key=f"create_from_template_{template_name}"):
                            # Создаем агента из шаблона
                            try:
                                from agent_streamlit_api import DynamicAgentDefinition
                                
                                definition = DynamicAgentDefinition(
                                    name=template_name,
                                    type=template.get('type', 'code'),
                                    description=template.get('description', ''),
                                    model=template.get('model', 'gpt-4'),
                                    tools=template.get('tools', []),
                                    instructions=template.get('instructions', ''),
                                    max_steps=template.get('max_steps', 20),
                                    planning_interval=template.get('planning_interval'),
                                    memory_policy=template.get('memory_policy', {}),
                                    metadata=template.get('metadata', {})
                                )
                                
                                success = agent_manager.register_dynamic_profile(template_name, definition)
                                
                                if success:
                                    st.success(f"✅ Агент '{template_name}' создан из шаблона")
                                else:
                                    st.error("❌ Не удалось создать агента")
                            
                            except Exception as e:
                                st.error(f"❌ Ошибка создания: {e}")
                        
                        if st.button(f"📋 Показать JSON", key=f"show_template_{template_name}"):
                            st.json(template)
                        
                        if st.button(f"🗑️ Удалить шаблон", key=f"delete_template_{template_name}"):
                            del st.session_state.saved_definitions[template_name]
                            st.success(f"✅ Шаблон '{template_name}' удален")
                            st.rerun()
    
    except Exception as e:
        st.error(f"❌ Ошибка управления агентами: {e}")

def show_import_export():
    """Импорт/экспорт конфигураций агентов"""
    
    st.markdown("## 💾 Импорт/Экспорт конфигураций")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("### 📤 Экспорт")
        
        # Экспорт всех сохраненных шаблонов
        if st.session_state.saved_definitions:
            if st.button("📦 Экспортировать все шаблоны"):
                export_data = {
                    "templates": st.session_state.saved_definitions,
                    "exported_at": datetime.now().isoformat(),
                    "version": "1.0"
                }
                
                st.download_button(
                    label="💾 Скачать templates.json",
                    data=json.dumps(export_data, indent=2, ensure_ascii=False),
                    file_name=f"agent_templates_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                    mime="application/json"
                )
        else:
            st.info("📭 Нет сохраненных шаблонов для экспорта")
        
        # Экспорт состава команды
        if st.session_state.team_composition:
            if st.button("👥 Экспортировать состав команды"):
                team_data = {
                    "team_composition": st.session_state.team_composition,
                    "exported_at": datetime.now().isoformat()
                }
                
                st.download_button(
                    label="💾 Скачать team.json",
                    data=json.dumps(team_data, indent=2, ensure_ascii=False),
                    file_name=f"team_composition_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                    mime="application/json"
                )
        else:
            st.info("👥 Нет команды для экспорта")
    
    with col2:
        st.markdown("### 📥 Импорт")
        
        # Импорт шаблонов
        uploaded_file = st.file_uploader(
            "📁 Загрузить файл шаблонов",
            type=['json'],
            help="Файл JSON с экспортированными шаблонами агентов"
        )
        
        if uploaded_file is not None:
            try:
                import_data = json.load(uploaded_file)
                
                if "templates" in import_data:
                    st.markdown("**📋 Найденные шаблоны:**")
                    
                    for template_name, template in import_data["templates"].items():
                        st.markdown(f"- **{template_name}**: {template.get('description', 'N/A')}")
                    
                    if st.button("✅ Импортировать шаблоны"):
                        # Объединяем с существующими
                        st.session_state.saved_definitions.update(import_data["templates"])
                        st.success(f"✅ Импортировано {len(import_data['templates'])} шаблонов")
                        st.rerun()
                
                elif "team_composition" in import_data:
                    st.markdown("**👥 Найденный состав команды:**")
                    
                    for agent in import_data["team_composition"]:
                        st.markdown(f"- **{agent['name']}** ({agent['type']})")
                    
                    if st.button("✅ Импортировать команду"):
                        st.session_state.team_composition = import_data["team_composition"]
                        st.success(f"✅ Импортирован состав команды из {len(import_data['team_composition'])} агентов")
                        st.rerun()
                
                else:
                    st.error("❌ Неправильный формат файла")
            
            except Exception as e:
                st.error(f"❌ Ошибка импорта: {e}")
        
        # Импорт из YAML (профили агентов)
        st.markdown("---")
        
        yaml_file = st.file_uploader(
            "📁 Загрузить YAML профиль",
            type=['yaml', 'yml'],
            help="YAML файл с профилем агента"
        )
        
        if yaml_file is not None:
            try:
                yaml_content = yaml.safe_load(yaml_file)
                
                st.markdown("**📋 Содержимое YAML:**")
                st.json(yaml_content)
                
                # Поля для преобразования
                agent_name = st.text_input("🤖 Имя агента для импорта")
                
                if agent_name and st.button("✅ Импортировать как шаблон"):
                    # Преобразуем YAML в формат шаблона
                    template = {
                        "name": agent_name,
                        "type": yaml_content.get('type', 'code'),
                        "description": yaml_content.get('description', ''),
                        "model": yaml_content.get('model', 'gpt-4'),
                        "tools": yaml_content.get('tools', []),
                        "instructions": yaml_content.get('prompt_templates', ''),
                        "max_steps": yaml_content.get('max_steps', 20),
                        "planning_interval": yaml_content.get('planning_interval'),
                        "memory_policy": yaml_content.get('memory_policy', {}),
                        "metadata": {
                            "imported_from_yaml": True,
                            "imported_at": datetime.now().isoformat()
                        }
                    }
                    
                    st.session_state.saved_definitions[agent_name] = template
                    st.success(f"✅ YAML импортирован как шаблон '{agent_name}'")
                    st.rerun()
            
            except Exception as e:
                st.error(f"❌ Ошибка импорта YAML: {e}")

def show_active_runs_monitoring():
    """Мониторинг активных запусков команд и агентов"""
    
    st.markdown("## ⚡ Мониторинг активных запусков")
    st.info("Здесь отображаются все активные запуски команд менеджера и отдельных агентов")
    
    try:
        from agent_streamlit_api import AgentManager
        import time
        
        agent_manager = get_agent_manager()
        
        # Автообновление
        col1, col2 = st.columns([3, 1])
        
        with col1:
            st.markdown("### 🏃‍♂️ Запущенные агенты")
        
        with col2:
            if "auto_refresh_dynamic" not in st.session_state:
                st.session_state.auto_refresh_dynamic = False
                
            auto_refresh = st.checkbox("🔄 Автообновление", value=st.session_state.auto_refresh_dynamic, key="auto_refresh_dynamic_agents")
            st.session_state.auto_refresh_dynamic = auto_refresh
            
            # Правильная реализация автообновления
            if auto_refresh:
                # Инициализируем время последнего обновления
                if "last_refresh_time_dynamic" not in st.session_state:
                    st.session_state.last_refresh_time_dynamic = time.time()
                
                # Проверяем, прошло ли 3 секунды
                current_time = time.time()
                if current_time - st.session_state.last_refresh_time_dynamic >= 3:
                    st.session_state.last_refresh_time_dynamic = current_time
                    st.rerun()
                
                # Показываем индикатор автообновления
                next_refresh = 3 - (current_time - st.session_state.last_refresh_time_dynamic)
                if next_refresh > 0:
                    st.caption(f"⏱️ Обновление через {next_refresh:.1f}с")
        
        # Показываем активные запуски
        if agent_manager.active_runs:
            st.markdown(f"**📊 Всего активных запусков: {len(agent_manager.active_runs)}**")
            
            for run_id, run_data in agent_manager.active_runs.items():
                # Определяем тип запуска
                is_team = "team_profiles" in run_data
                
                if is_team:
                    icon = "👥"
                    title_prefix = "Команда"
                    team_info = f" ({len(run_data.get('team_profiles', []))} агентов)"
                else:
                    icon = "🤖"
                    title_prefix = "Агент"
                    team_info = ""
                
                with st.expander(
                    f"{icon} {title_prefix}: {run_data.get('profile_name', 'Unknown')}{team_info} - {run_id[:8]}", 
                    expanded=True
                ):
                    
                    # Получаем статус
                    status = agent_manager.get_agent_status(run_id)
                    events = agent_manager.get_agent_events(run_id)
                    
                    # Основная информация
                    col1, col2, col3, col4 = st.columns(4)
                    
                    with col1:
                        status_icon = {
                            "running": "🟡",
                            "completed": "🟢",
                            "failed": "🔴",
                            "cancelled": "⚫"
                        }
                        current_status = run_data.get("status", "unknown")
                        st.metric(
                            "Статус",
                            f"{status_icon.get(current_status, '⚪')} {current_status}"
                        )
                    
                    with col2:
                        start_time = run_data.get("start_time")
                        if start_time:
                            elapsed = datetime.now() - start_time
                            st.metric("Время выполнения", f"{elapsed.seconds}s")
                        else:
                            st.metric("Время выполнения", "N/A")
                    
                    with col3:
                        if is_team:
                            st.metric("Менеджер", run_data.get("manager_profile", "manager"))
                        else:
                            st.metric("Профиль", run_data.get("profile_name", "Unknown"))
                    
                    with col4:
                        session_id = run_data.get("session_id", "")
                        st.metric("Session", session_id[:8] + "..." if len(session_id) > 8 else session_id)
                    
                    # Задача
                    task = run_data.get("task", "")
                    if task:
                        st.markdown("**📝 Задача:**")
                        st.info(task[:300] + "..." if len(task) > 300 else task)
                    
                    # Команда (для команд менеджера)
                    if is_team:
                        team_profiles = run_data.get("team_profiles", [])
                        if team_profiles:
                            st.markdown("**👥 Состав команды:**")
                            team_str = ", ".join(team_profiles)
                            st.code(team_str)
                    
                    # События
                    if events:
                        st.markdown("**📊 События:**")
                        for event_type, event_data in events.items():
                            timestamp = event_data.get("timestamp", "")
                            st.text(f"• {event_type}: {timestamp}")
                    
                    # Управление
                    col1, col2, col3 = st.columns(3)
                    
                    with col1:
                        if st.button(f"🛑 Остановить", key=f"stop_{run_id}"):
                            success = agent_manager.cancel_agent_run(run_id)
                            if success:
                                st.success("✅ Запуск остановлен")
                                st.rerun()
                            else:
                                st.error("❌ Не удалось остановить")
                    
                    with col2:
                        if st.button(f"📊 Детали", key=f"details_{run_id}"):
                            st.json(run_data)
                    
                    with col3:
                        if status and hasattr(status, 'run_id'):
                            if st.button(f"📋 Статус", key=f"status_{run_id}"):
                                st.json(status.__dict__)
        else:
            st.info("📭 Нет активных запусков")
            st.markdown("**💡 Подсказка:** Запустите команду на вкладке '👥 Команда менеджера' чтобы увидеть активные запуски здесь")
        
        # Очистка завершенных
        if st.button("🧹 Очистить завершенные запуски"):
            completed_count = 0
            to_remove = []
            
            for run_id, run_data in agent_manager.active_runs.items():
                if run_data.get("status") in ["completed", "failed", "cancelled"]:
                    to_remove.append(run_id)
                    completed_count += 1
            
            for run_id in to_remove:
                del agent_manager.active_runs[run_id]
            
            if completed_count > 0:
                st.success(f"✅ Очищено {completed_count} завершенных запусков")
                st.rerun()
            else:
                st.info("ℹ️ Нет завершенных запусков для очистки")
    
    except Exception as e:
        st.error(f"❌ Ошибка мониторинга: {e}")
        st.exception(e)

if __name__ == "__main__":
    main()
