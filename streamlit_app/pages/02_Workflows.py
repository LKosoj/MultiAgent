"""
Страница управления Workflows (пайплайнами)
==========================================
"""

import streamlit as st
import sys
import warnings
from pathlib import Path
import json
from datetime import datetime
import time
import uuid

# Фильтруем предупреждения Streamlit о ScriptRunContext в потоках
warnings.filterwarnings("ignore", message=".*missing ScriptRunContext.*")

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

def get_workflow_manager(use_enhanced: bool = True):
    """Получить WorkflowManager с глобальным состоянием"""
    from workflow.streamlit_api import WorkflowManager
    return WorkflowManager(use_enhanced=use_enhanced)

def show_workflow_artifacts(final_output, run_id):
    """Отобразить артефакты workflow в структурированном виде"""
    if not final_output:
        st.info("Результаты пока недоступны")
        return
    
    # Определяем тип результата
    if isinstance(final_output, dict):
        workflow_type = final_output.get("type", "unknown")
        
        if workflow_type == "workflow_result":
            # Результат от default aggregator
            st.markdown(f"### 📊 Результаты workflow: {final_output.get('workflow_name', 'Unknown')}")
            
            # Сводка
            col1, col2, col3 = st.columns(3)
            with col1:
                outputs_count = len(final_output.get("outputs", {}))
                st.metric("Шагов выполнено", outputs_count)
            with col2:
                avg_quality = final_output.get("quality_metrics", {}).get("average_quality", 0)
                st.metric("Средняя оценка", f"{avg_quality:.2f}")
            with col3:
                execution_path = final_output.get("execution_path", [])
                successful = len([s for s in execution_path if s.get("status") == "completed"])
                st.metric("Успешных шагов", successful)
            
            # Основные результаты
            st.markdown("#### 🎯 Основные результаты")
            outputs = final_output.get("outputs", {})
            
            # Показываем результат последнего шага отдельно
            if outputs:
                last_step_key = list(outputs.keys())[-1]
                last_output = outputs[last_step_key]["output"]
                
                st.markdown(f"**Финальный результат ({last_step_key}):**")
                if isinstance(last_output, str):
                    try:
                        import json
                        parsed = json.loads(last_output)
                        st.json(parsed)
                    except:
                        st.text(last_output)
                else:
                    st.json(last_output)
            
            # Детализация по шагам
            with st.expander("🔍 Детализация по шагам"):
                for step_id, step_data in outputs.items():
                    st.markdown(f"**{step_id}**")
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        output = step_data["output"]
                        if isinstance(output, str) and len(output) > 200:
                            st.text(output[:200] + "...")
                            if st.button(f"Показать полностью", key=f"expand_{step_id}_{run_id}"):
                                st.text(output)
                        else:
                            if isinstance(output, str):
                                try:
                                    import json
                                    parsed = json.loads(output)
                                    st.json(parsed)
                                except:
                                    st.text(output)
                            else:
                                st.json(output)
                    with col2:
                        quality = step_data.get("quality_score", 0)
                        duration = step_data.get("duration", 0)
                        st.caption(f"Качество: {quality:.2f}")
                        st.caption(f"Время: {duration:.1f}с")
                    st.divider()
            
        elif workflow_type in ["research_report", "analysis_report", "sql_generation"]:
            # Специализированные результаты
            st.markdown(f"### 📊 {workflow_type.replace('_', ' ').title()}")
            
            if "summary" in final_output and final_output["summary"]:
                st.markdown("#### 📝 Резюме")
                st.write(final_output["summary"])
            
            if "key_findings" in final_output and final_output["key_findings"]:
                st.markdown("#### 🔍 Ключевые находки")
                for finding in final_output["key_findings"]:
                    st.write(f"• {finding}")
            
            if "recommendations" in final_output and final_output["recommendations"]:
                st.markdown("#### 💡 Рекомендации")
                for rec in final_output["recommendations"]:
                    st.write(f"• {rec}")
            
            # Полный результат в свернутом виде
            with st.expander("🗃️ Полные данные"):
                st.json(final_output)
        else:
            # Неизвестный тип - показываем как JSON
            st.markdown("### 📊 Результаты workflow")
            st.json(final_output)
    else:
        # Не dict - показываем как есть
        st.markdown("### 📊 Результат workflow")
        st.write(final_output)

def main():
    st.set_page_config(
        page_title="Workflows - MultiAgent System",
        page_icon="🔄",
        layout="wide"
    )
    
    st.title("🔄 Управление Workflows")
    st.markdown("---")
    
    # Инициализация состояния
    init_session_state()
    
    # Главные вкладки
    tab1, tab2, tab3, tab4 = st.tabs(["📋 Доступные пайплайны", "🚀 Запуск", "📊 Мониторинг", "🛠️ Конструктор"])
    
    with tab1:
        show_available_workflows()
    
    with tab2:
        show_workflow_execution()
    
    with tab3:
        show_workflow_monitoring()
    
    with tab4:
        show_pipeline_constructor()

def init_session_state():
    """Инициализация состояния сессии"""
    if "selected_workflow" not in st.session_state:
        st.session_state.selected_workflow = None
    if "workflow_runs" not in st.session_state:
        st.session_state.workflow_runs = {}
    if "auto_refresh" not in st.session_state:
        st.session_state.auto_refresh = False
    if "constructor_steps" not in st.session_state:
        st.session_state.constructor_steps = []
    if "available_agents" not in st.session_state:
        st.session_state.available_agents = []
    
    # Состояния для редактирования
    if "editing_pipeline" not in st.session_state:
        st.session_state.editing_pipeline = None
    if "editing_pipeline_file" not in st.session_state:
        st.session_state.editing_pipeline_file = None
    if "show_save_as_dialog" not in st.session_state:
        st.session_state.show_save_as_dialog = False

def show_available_workflows():
    """Отображение доступных пайплайнов"""
    
    st.markdown("## 📋 Доступные пайплайны")
    
    try:
        from workflow.streamlit_api import WorkflowManager
        
        wf_manager = get_workflow_manager()
        workflows = wf_manager.list_workflows()
        
        if not workflows:
            st.warning("📭 Не найдено пайплайнов в директории workflow_pipelines/")
            st.info("Создайте YAML файлы пайплайнов в папке workflow_pipelines/")
            return
        
        # Фильтры
        col1, col2, col3 = st.columns(3)
        
        with col1:
            categories = ["Все"] + list(set(wf.category for wf in workflows))
            selected_category = st.selectbox("🏷️ Категория", categories)
        
        with col2:
            complexities = ["Все"] + list(set(wf.complexity for wf in workflows if wf.complexity != "неизвестно"))
            selected_complexity = st.selectbox("⚡ Сложность", complexities)
        
        with col3:
            search_term = st.text_input("🔍 Поиск по имени")
        
        # Фильтрация
        filtered_workflows = workflows
        
        if selected_category != "Все":
            filtered_workflows = [wf for wf in filtered_workflows if wf.category == selected_category]
        
        if selected_complexity != "Все":
            filtered_workflows = [wf for wf in filtered_workflows if wf.complexity == selected_complexity]
        
        if search_term:
            filtered_workflows = [wf for wf in filtered_workflows 
                                if search_term.lower() in wf.name.lower() or 
                                   search_term.lower() in wf.description.lower()]
        
        # Отображение пайплайнов
        for workflow in filtered_workflows:
            # Определяем тип движка из метаданных
            engine_type = "неизвестно"
            engine_icon = "⚪"
            
            # Попробуем извлечь тип движка из файла
            try:
                import yaml
                with open(workflow.file_path, 'r', encoding='utf-8') as f:
                    yaml_data = yaml.safe_load(f)
                    metadata = yaml_data.get('metadata', {})
                    engine_type = metadata.get('engine_type', 'неизвестно')
                    
                if engine_type == 'simple':
                    engine_icon = "🔵"
                    engine_type = "Простой"
                elif engine_type == 'enhanced':
                    engine_icon = "🟢"
                    engine_type = "Расширенный"
            except:
                pass
            
            with st.expander(f"🔄 {workflow.name} (v{workflow.version}) {engine_icon}", expanded=False):
                col1, col2 = st.columns([2, 1])
                
                with col1:
                    st.markdown(f"**Описание:** {workflow.description or 'Нет описания'}")
                    st.markdown(f"**Категория:** {workflow.category}")
                    st.markdown(f"**Тип движка:** {engine_icon} {engine_type}")
                    st.markdown(f"**Сложность:** {workflow.complexity}")
                    st.markdown(f"**Шагов:** {workflow.steps_count}")
                    st.markdown(f"**Ожидаемое время:** {workflow.estimated_duration}")
                    
                    if workflow.agents_used:
                        st.markdown(f"**Используемые агенты:** {', '.join(workflow.agents_used)}")
                    
                    # Показываем inputs из YAML файла
                    try:
                        import yaml
                        with open(workflow.file_path, 'r', encoding='utf-8') as f:
                            yaml_data = yaml.safe_load(f)
                            pipeline_inputs = yaml_data.get('inputs', {})
                        
                        if pipeline_inputs:
                            st.markdown("**Входные параметры:**")
                            for param_name, default_value in pipeline_inputs.items():
                                default_text = f" (по умолчанию: {default_value})" if default_value else " (обязательный)"
                                st.markdown(f"- `{param_name}`: {default_text}")
                        else:
                            st.markdown("**❌ Входные параметры:** Секция 'inputs' отсутствует")
                    except Exception as e:
                        st.markdown(f"**❌ Входные параметры:** Ошибка чтения: {e}")
                
                with col2:
                    st.markdown(f"**Файл:** `{Path(workflow.file_path).name}`")
                    
                    if st.button(f"📋 Выбрать для запуска", key=f"select_{workflow.name}"):
                        st.session_state.selected_workflow = workflow
                        st.success(f"✅ Выбран пайплайн: {workflow.name}")
                        st.switch_page("pages/02_Workflows.py")
                    
                    if st.button(f"✏️ Редактировать", key=f"edit_{workflow.name}"):
                        try:
                            # Загружаем пайплайн для редактирования
                            pipeline_data = load_pipeline_for_editing(workflow.file_path)
                            if pipeline_data:
                                st.session_state.editing_pipeline = pipeline_data
                                st.session_state.editing_pipeline_file = workflow.file_path
                                st.success(f"✅ Пайплайн '{workflow.name}' загружен для редактирования")
                                st.switch_page("pages/02_Workflows.py")
                        except Exception as e:
                            st.error(f"Ошибка загрузки пайплайна: {e}")
                    
                    if st.button(f"👁️ Просмотр YAML", key=f"view_{workflow.name}"):
                        try:
                            with open(workflow.file_path, 'r', encoding='utf-8') as f:
                                yaml_content = f.read()
                            st.code(yaml_content, language='yaml')
                        except Exception as e:
                            st.error(f"Ошибка чтения файла: {e}")
    
    except Exception as e:
        st.error(f"❌ Ошибка загрузки пайплайнов: {e}")

def show_workflow_execution():
    """Отображение интерфейса запуска пайплайна"""
    
    st.markdown("## 🚀 Запуск пайплайна")
    
    if not st.session_state.selected_workflow:
        st.info("📋 Выберите пайплайн на вкладке 'Доступные пайплайны'")
        return
    
    workflow = st.session_state.selected_workflow
    
    # Информация о выбранном пайплайне
    with st.container():
        st.markdown(f"### 📄 Выбранный пайплайн: **{workflow.name}**")
        
        col1, col2, col3 = st.columns(3)
        with col1:
            st.info(f"**Версия:** {workflow.version}")
        with col2:
            st.info(f"**Шагов:** {workflow.steps_count}")
        with col3:
            st.info(f"**Время:** {workflow.estimated_duration}")
        
        st.markdown(f"**Описание:** {workflow.description}")
    
    st.markdown("---")
    
    # Форма параметров
    with st.form("workflow_execution_form"):
        st.markdown("### ⚙️ Параметры выполнения")
        
        col1, col2 = st.columns(2)
        
        with col1:
            use_enhanced = st.checkbox(
                "⚡ Использовать Enhanced Engine",
                value=True,
                help="Использовать расширенный движок с дополнительными возможностями"
            )
        
        with col2:
            enable_telemetry = st.checkbox(
                "📊 Включить телеметрию",
                value=True,
                help="Записывать трассы выполнения"
            )
        
        # Параметры пайплайна из секции inputs
        st.markdown("### 📝 Параметры пайплайна")
        
        # Читаем inputs из YAML файла пайплайна
        pipeline_inputs = {}
        try:
            import yaml
            with open(workflow.file_path, 'r', encoding='utf-8') as f:
                yaml_data = yaml.safe_load(f)
                pipeline_inputs = yaml_data.get('inputs', {})
        except Exception as e:
            st.warning(f"⚠️ Не удалось прочитать секцию inputs: {e}")
        
        pipeline_params = {}
        
        if pipeline_inputs:
            st.markdown("**Входные параметры:**")
            for param_name, default_value in pipeline_inputs.items():
                # Определяем тип поля и помощь
                param_help = f"Входной параметр {param_name}"
                param_placeholder = ""
                
                # Специальная обработка для известных параметров
                if param_name == "topic":
                    param_help = "Тема для исследования или анализа"
                    param_placeholder = "Введите тему для исследования..."
                elif param_name == "task":
                    param_help = "Текстовый промпт или задача для выполнения"
                    param_placeholder = "Введите описание задачи..."
                elif param_name == "analysis_request":
                    param_help = "Запрос на анализ данных"
                    param_placeholder = "Введите запрос для анализа..."
                elif param_name == "project_path":
                    param_help = "Путь к проекту для анализа"
                    param_placeholder = "/path/to/project"
                elif param_name == "image_prompt":
                    param_help = "Промпт для генерации изображения"
                    param_placeholder = "Опишите желаемое изображение..."
                elif param_name == "research_topic":
                    param_help = "Тема для исследования"
                    param_placeholder = "Введите тему для исследования..."
                elif param_name == "end_date":
                    param_help = "Дата в формате dd/mm/yyyy"
                    param_placeholder = "01/01/2024"
                elif param_name == "session_id":
                    # session_id автогенерируется, показываем только для информации
                    param_help = "Автоматически генерируется (можно оставить пустым)"
                    param_placeholder = "Будет сгенерирован автоматически"
                elif param_name == "project_id":
                    param_help = "Идентификатор проекта"
                    param_placeholder = "my_project"
                
                # Отображаем поле ввода
                param_value = st.text_input(
                    f"📋 {param_name}",
                    value=str(default_value) if default_value else "",
                    placeholder=param_placeholder,
                    help=param_help,
                    key=f"param_{param_name}"
                )
                
                # Добавляем в параметры, если значение введено или есть дефолт
                if param_value.strip():
                    # Для некоторых параметров конвертируем тип
                    if param_name in ["end_date"] and param_value.strip() == "today":
                        from datetime import datetime
                        pipeline_params[param_name] = datetime.now().strftime("%d/%m/%Y")
                    else:
                        pipeline_params[param_name] = param_value.strip()
                elif default_value and str(default_value).strip():
                    # Если ничего не введено, но есть дефолт - используем его
                    pipeline_params[param_name] = str(default_value).strip()
        else:
            # Если нет inputs - показываем ошибку
            st.error("❌ Этот пайплайн не имеет секции 'inputs'. Пожалуйста, обновите пайплайн.")
            st.info("💡 Добавьте секцию 'inputs' в YAML файл пайплайна или используйте конструктор для обновления.")
        
        # Кнопка запуска
        submitted = st.form_submit_button("🚀 Запустить пайплайн", type="primary")
        
        if submitted:
            # Проверяем обязательные параметры
            missing_params = []
            if pipeline_inputs:
                for param_name, default_value in pipeline_inputs.items():
                    # Параметры с пустыми дефолтами считаются обязательными
                    if not default_value and param_name not in pipeline_params:
                        missing_params.append(param_name)
            # Если нет inputs - это ошибка, не можем проверить параметры
            else:
                missing_params.append("inputs секция отсутствует")
            
            if missing_params:
                st.error(f"❌ Пожалуйста, заполните обязательные параметры: {', '.join(missing_params)}")
                return
            
            # Проверяем, что есть inputs секция
            if not pipeline_inputs:
                st.error("❌ Невозможно запустить пайплайн без секции 'inputs'")
                return
                
            try:
                from workflow.streamlit_api import WorkflowManager
                from telemetry import configure_telemetry
                from unified_logging import get_logging_manager
                
                # Настраиваем телеметрию
                if enable_telemetry:
                    configure_telemetry(enabled=True)
                
                # Создаем менеджер
                wf_manager = get_workflow_manager(use_enhanced=use_enhanced)
                
                # Настраиваем логирование
                logging_manager = get_logging_manager()
                
                # Callback для прогресса
                progress_placeholder = st.empty()
                logs_placeholder = st.empty()
                
                def progress_callback(run_id, event_type, data):
                    with progress_placeholder.container():
                        if event_type == "started":
                            st.info(f"🚀 Запуск пайплайна {data.get('workflow_name', 'Unknown')}")
                        elif event_type == "step":
                            st.info(f"⚡ Выполняется шаг: {data.get('step_name', 'Unknown')}")
                        elif event_type == "completed":
                            st.success(f"✅ Пайплайн завершен успешно")
                        elif event_type == "failed":
                            st.error(f"❌ Ошибка выполнения: {data.get('error', 'Unknown')}")
                
                def log_callback(run_id, level, message, timestamp):
                    with logs_placeholder.container():
                        if level in ["ERROR", "CRITICAL"]:
                            st.error(f"🔴 [{timestamp}] {message}")
                        elif level == "WARNING":
                            st.warning(f"🟡 [{timestamp}] {message}")
                        else:
                            st.info(f"ℹ️ [{timestamp}] {message}")
                
                # Запускаем пайплайн с единым run_id и явным логгером
                with st.spinner("Запуск пайплайна..."):
                    # Не генерируем run_id на UI — используем тот, что вернет менеджер
                    # session_id и client_id теперь автогенерируются в движке
                    run_id = wf_manager.start_workflow(
                        workflow_name=workflow.name,
                        parameters=pipeline_params,
                        progress_callback=progress_callback,
                        log_callback=log_callback
                    )
                    # Локально логируем старт без утечки RUN_ID
                    try:
                        from unified_logging import get_run_logger, run_id_context
                        with run_id_context(run_id):
                            _rlog = get_run_logger(run_id, __name__)
                            _rlog.info(f"Старт UI запуска workflow '{workflow.name}'")
                    except Exception:
                        pass
                
                st.success(f"✅ Пайплайн запущен с ID: `{run_id}`")
                # Сохраняем run_id, чтобы показать кнопку вне формы
                st.session_state.last_workflow_run_id = run_id
                
                # Сохраняем в состоянии
                from datetime import datetime
                st.session_state.workflow_runs[run_id] = {
                    "workflow_name": workflow.name,
                    "start_time": datetime.now(),
                    "parameters": pipeline_params
                }
                
                # Переключаемся на мониторинг
                time.sleep(1)
                st.switch_page("pages/02_Workflows.py")
            
            except Exception as e:
                st.error(f"❌ Ошибка запуска пайплайна: {e}")
                st.exception(e)

    # Кнопка открытия трасс последнего запуска (вне формы)
    if "last_workflow_run_id" in st.session_state:
        rid = st.session_state.last_workflow_run_id
        tr_col1, tr_col2 = st.columns([1, 3])
        with tr_col1:
            if st.button("🔍 Открыть трассы", key=f"wf_traces_{rid}"):
                try:
                    st.query_params["run_id"] = rid
                except Exception:
                    pass
                st.switch_page("pages/08_Logs_Traces.py")

def show_workflow_monitoring():
    """Мониторинг выполнения пайплайнов"""
    
    st.markdown("## 📊 Мониторинг выполнения")
    
    try:
        from workflow.streamlit_api import WorkflowManager
        
        wf_manager = get_workflow_manager()
        
        # Автообновление
        col1, col2 = st.columns([3, 1])
        
        with col1:
            st.markdown("### 🔄 Активные выполнения")
        
        with col2:
            auto_refresh = st.checkbox("🔄 Автообновление", value=st.session_state.auto_refresh)
            st.session_state.auto_refresh = auto_refresh
            
            # Правильная реализация автообновления
            if auto_refresh:
                import time
                # Инициализируем время последнего обновления
                if "last_refresh_time_workflows" not in st.session_state:
                    st.session_state.last_refresh_time_workflows = time.time()
                
                # Проверяем, прошло ли 3 секунды
                current_time = time.time()
                if current_time - st.session_state.last_refresh_time_workflows >= 3:
                    st.session_state.last_refresh_time_workflows = current_time
                    st.rerun()
                
                # Показываем индикатор автообновления
                next_refresh = 3 - (current_time - st.session_state.last_refresh_time_workflows)
                if next_refresh > 0:
                    st.caption(f"⏱️ Обновление через {next_refresh:.1f}с")
        
        # Получаем статусы всех запусков
        if wf_manager.active_runs:
            for run_id, run_data in wf_manager.active_runs.items():
                with st.expander(f"🔄 {run_data.get('workflow_name', 'Unknown')} - {run_id[:8]}", expanded=True):
                    
                    # Статус
                    status = wf_manager.get_workflow_status(run_id)
                    
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
                            if status.progress_percentage:
                                st.metric("Прогресс", f"{status.progress_percentage:.1f}%")
                            else:
                                st.metric("Прогресс", "N/A")
                        
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
                                st.metric("Шаг", f"{status.current_step_index}/{status.total_steps}")
                        
                        # Прогресс-бар
                        if status.progress_percentage > 0:
                            st.progress(status.progress_percentage / 100.0)
                        
                        # Ошибки
                        if status.error_message:
                            st.error(f"❌ Ошибка: {status.error_message}")
                        
                        # Кнопки управления
                        action_col1, action_col2, action_col3 = st.columns(3)
                        
                        with action_col1:
                            if status.status == "running":
                                if st.button(f"⏹️ Отменить", key=f"cancel_{run_id}"):
                                    if wf_manager.cancel_workflow(run_id):
                                        st.success("✅ Пайплайн отменен")
                                        st.rerun()
                                    else:
                                        st.error("❌ Не удалось отменить")
                            elif status.status == "cancelled":
                                st.caption("Отменено")
                        
                        with action_col2:
                            if st.button(f"📊 Артефакты", key=f"artifacts_{run_id}"):
                                artifacts = wf_manager.get_workflow_artifacts(run_id)
                                if artifacts and artifacts.final_output:
                                    show_workflow_artifacts(artifacts.final_output, run_id)
                                else:
                                    st.info("Артефакты пока недоступны")
                        
                        with action_col3:
                            if st.button(f"🔍 Трассы", key=f"traces_{run_id}"):
                                st.switch_page("pages/08_Logs_Traces.py")
                    
                    else:
                        st.warning(f"⚠️ Не удалось получить статус для {run_id}")
        else:
            st.info("📭 Нет активных выполнений")
        
        # История выполнений
        st.markdown("### 📚 История выполнений")
        
        if st.session_state.workflow_runs:
            history_data = []
            for run_id, run_info in st.session_state.workflow_runs.items():
                status = wf_manager.get_workflow_status(run_id)
                
                history_data.append({
                    "Run ID": run_id[:8] + "...",
                    "Пайплайн": run_info["workflow_name"],
                    "Статус": status.status if status else "Unknown",
                    "Начат": run_info["start_time"].strftime("%H:%M:%S"),
                    "Session ID": run_info["session_id"]
                })
            
            if history_data:
                st.dataframe(history_data, use_container_width=True)
        else:
            st.info("📭 История пуста")
        
        # Очистка истории
        if st.button("🧹 Очистить завершенные"):
            from workflow.streamlit_api import WorkflowManager
            wf_manager = get_workflow_manager()
            wf_manager.cleanup_completed_runs(max_age_hours=1)
            st.success("✅ Завершенные запуски очищены")
            st.rerun()
    
    except Exception as e:
        st.error(f"❌ Ошибка мониторинга: {e}")

def show_pipeline_constructor():
    """Конструктор пайплайнов с возможностью сохранения"""
    
    # Проверяем режим редактирования
    is_editing = "editing_pipeline" in st.session_state and st.session_state.editing_pipeline is not None
    
    if is_editing:
        st.markdown("## ✏️ Редактирование пайплайна")
        
        # Безопасно получаем имя пайплайна
        pipeline_name = "Unknown"
        try:
            if isinstance(st.session_state.editing_pipeline, dict):
                if 'pipeline_info' in st.session_state.editing_pipeline:
                    pipeline_name = st.session_state.editing_pipeline['pipeline_info'].get('name', 'Unknown')
                else:
                    pipeline_name = st.session_state.editing_pipeline.get('name', 'Unknown')
        except Exception:
            pipeline_name = "Unknown"
        
        st.markdown(f"Редактируете: **{pipeline_name}**")
        
        # Кнопка отмены редактирования
        if st.button("❌ Отменить редактирование"):
            if "editing_pipeline" in st.session_state:
                del st.session_state.editing_pipeline
            if "editing_pipeline_file" in st.session_state:
                del st.session_state.editing_pipeline_file
            if "pipeline_info" in st.session_state:
                del st.session_state.pipeline_info
            st.session_state.constructor_steps = []
            st.success("✅ Редактирование отменено")
            st.rerun()
    else:
        st.markdown("## 🛠️ Конструктор пайплайнов")
        st.markdown("Создайте новый пайплайн с помощью визуального конструктора")
    
    # Информация о типах движков
    with st.expander("ℹ️ Информация о типах пайплайнов", expanded=False):
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("### 🔵 Простой пайплайн")
            st.markdown("- Использует базовый `WorkflowEngine`")
            st.markdown("- Стандартная функциональность")
            st.markdown("- Быстрое выполнение")
            st.markdown("- Подходит для простых задач")
        
        with col2:
            st.markdown("### 🟢 Расширенный пайплайн")
            st.markdown("- Использует `EnhancedWorkflowEngine`")
            st.markdown("- Дополнительные возможности")
            st.markdown("- Умная оптимизация")
            st.markdown("- Для сложных workflow")
    
    # Загрузка доступных агентов и инструментов
    if not st.session_state.available_agents:
        try:
            # Получаем список агентов из профилей
            from pathlib import Path
            agent_profiles_dir = Path.cwd() / "agent_profiles"
            agents = []
            if agent_profiles_dir.exists():
                for yaml_file in agent_profiles_dir.glob("*.yaml"):
                    agent_name = yaml_file.stem
                    if agent_name not in ["agent_constructor"]:  # Исключаем служебные
                        agents.append(agent_name)
            
            # Добавляем доступные инструменты
            tools = [
                "web_search_tool",
                "generate_image_tool", 
                "analyze_image_tool",
                "code_interpreter_tool",
                "file_operations_tool"
            ]
            
            st.session_state.available_agents = sorted(agents)
            st.session_state.available_tools = sorted(tools)
            
        except Exception as e:
            st.error(f"❌ Ошибка загрузки агентов: {e}")
            st.session_state.available_agents = ["researcher", "analyst", "manager"]
            st.session_state.available_tools = ["web_search_tool", "generate_image_tool"]
    
    # Предзагрузка данных для режима редактирования
    if is_editing and st.session_state.editing_pipeline:
        try:
            editing_data = st.session_state.editing_pipeline
            if isinstance(editing_data, dict) and "pipeline_info" in editing_data:
                pipeline_info = editing_data["pipeline_info"]
                editing_steps = editing_data.get("steps", [])
                
                # Загружаем данные в session_state если они еще не загружены
                if "pipeline_info" not in st.session_state:
                    st.session_state.pipeline_info = pipeline_info
                if not st.session_state.constructor_steps:
                    st.session_state.constructor_steps = editing_steps
            else:
                # Если структура данных неправильная, очищаем режим редактирования
                st.warning("⚠️ Ошибка в данных редактирования. Переключаемся в режим создания.")
                if "editing_pipeline" in st.session_state:
                    del st.session_state.editing_pipeline
                if "editing_pipeline_file" in st.session_state:
                    del st.session_state.editing_pipeline_file
                is_editing = False
        except Exception as e:
            st.error(f"❌ Ошибка при загрузке данных для редактирования: {e}")
            # Очищаем состояние редактирования
            if "editing_pipeline" in st.session_state:
                del st.session_state.editing_pipeline
            if "editing_pipeline_file" in st.session_state:
                del st.session_state.editing_pipeline_file
            is_editing = False
    
    # Основная информация о пайплайне
    with st.form("pipeline_info_form"):
        st.markdown("### 📝 Основная информация")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Безопасно получаем значения для редактирования
            default_name = ""
            if is_editing and "pipeline_info" in st.session_state:
                default_name = st.session_state.pipeline_info.get("name", "")
            
            pipeline_name = st.text_input(
                "Название пайплайна *",
                value=default_name,
                placeholder="my_custom_pipeline",
                help="Уникальное имя пайплайна (будет использовано как имя файла)"
            )
            
            # Безопасно получаем версию
            default_version = "1.0"
            if is_editing and "pipeline_info" in st.session_state:
                default_version = st.session_state.pipeline_info.get("version", "1.0")
            
            pipeline_version = st.text_input(
                "Версия",
                value=default_version,
                help="Версия пайплайна"
            )
            
            # Безопасно определяем индекс типа
            type_index = 0
            if is_editing and "pipeline_info" in st.session_state:
                current_type = st.session_state.pipeline_info.get("type", "simple")
                type_index = 0 if current_type == "simple" else 1
            
            pipeline_type = st.selectbox(
                "Тип пайплайна *",
                ["simple", "enhanced"],
                index=type_index,
                format_func=lambda x: {
                    "simple": "🔵 Простой (WorkflowEngine)",
                    "enhanced": "🟢 Расширенный (EnhancedWorkflowEngine)"
                }[x],
                help="Выберите тип движка для выполнения пайплайна"
            )
            
            categories = ["general", "research", "analysis", "content", "financial", "technical", "demo"]
            category_index = 0
            if is_editing and "pipeline_info" in st.session_state:
                current_category = st.session_state.pipeline_info.get("category", "general")
                category_index = categories.index(current_category) if current_category in categories else 0
            
            pipeline_category = st.selectbox(
                "Категория",
                categories,
                index=category_index,
                help="Категория для группировки пайплайнов"
            )
        
        with col2:
            # Безопасно получаем описание
            default_description = ""
            if is_editing and "pipeline_info" in st.session_state:
                default_description = st.session_state.pipeline_info.get("description", "")
            
            pipeline_description = st.text_area(
                "Описание *",
                value=default_description,
                placeholder="Описание того, что делает этот пайплайн...",
                help="Подробное описание назначения пайплайна"
            )
            
            # Безопасно получаем время выполнения
            default_duration = "5-10 minutes"
            if is_editing and "pipeline_info" in st.session_state:
                default_duration = st.session_state.pipeline_info.get("estimated_duration", "5-10 minutes")
            
            estimated_duration = st.text_input(
                "Ожидаемое время выполнения",
                value=default_duration,
                help="Примерное время выполнения пайплайна"
            )
            
            # Безопасно определяем сложность
            complexities = ["simple", "medium", "complex"]
            complexity_index = 0
            if is_editing and "pipeline_info" in st.session_state:
                current_complexity = st.session_state.pipeline_info.get("complexity", "simple")
                complexity_index = complexities.index(current_complexity) if current_complexity in complexities else 0
            
            complexity = st.selectbox(
                "Сложность",
                complexities,
                index=complexity_index,
                help="Уровень сложности пайплайна"
            )
        
        # Конфигурация входных параметров
        with st.expander("📝 Входные параметры (inputs)", expanded=True):
            st.markdown("**Настройте входные параметры пайплайна:**")
            
            # Загружаем существующие inputs если в режиме редактирования
            existing_inputs = {}
            if is_editing and "pipeline_info" in st.session_state:
                existing_inputs = st.session_state.pipeline_info.get("inputs", {})
            
            # Основные распространенные параметры
            common_params = [
                ("topic", "Тема для исследования"),
                ("task", "Задача или промпт"),
                ("analysis_request", "Запрос на анализ"),
                ("project_path", "Путь к проекту"),
                ("project_id", "Идентификатор проекта"),
                ("image_prompt", "Промпт для изображения"),
                ("research_topic", "Тема исследования"),
                ("end_date", "Дата окончания"),
                ("session_id", "ID сессии (автоген)")
            ]
            
            inputs_config = {}
            
            st.markdown("**Выберите нужные параметры:**")
            for param_name, param_desc in common_params:
                col1, col2, col3 = st.columns([1, 2, 2])
                
                with col1:
                    # Чекбокс для включения параметра
                    is_enabled = st.checkbox(
                        param_name,
                        value=param_name in existing_inputs,
                        key=f"enable_{param_name}"
                    )
                
                with col2:
                    # Описание
                    st.markdown(f"*{param_desc}*")
                
                with col3:
                    # Значение по умолчанию
                    if is_enabled:
                        default_val = existing_inputs.get(param_name, "")
                        if param_name == "session_id":
                            default_val = ""  # session_id всегда пустой
                        elif param_name == "end_date":
                            default_val = existing_inputs.get(param_name, "today")
                        elif param_name == "project_id":
                            default_val = existing_inputs.get(param_name, pipeline_name.strip() if pipeline_name.strip() else "my_project")
                        
                        default_value = st.text_input(
                            "Значение по умолчанию",
                            value=str(default_val),
                            placeholder="Оставьте пустым для обязательного параметра",
                            key=f"default_{param_name}",
                            help="Пустое значение = обязательный параметр"
                        )
                        inputs_config[param_name] = default_value
            
            # Дополнительные кастомные параметры
            st.markdown("**Дополнительные параметры:**")
            
            # Загружаем кастомные параметры из существующих inputs
            custom_params = {}
            if existing_inputs:
                common_param_names = [name for name, _ in common_params]
                for param_name, param_value in existing_inputs.items():
                    if param_name not in common_param_names:
                        custom_params[param_name] = param_value
            
            # Показываем существующие кастомные параметры
            custom_params_to_remove = []
            for i, (param_name, param_value) in enumerate(custom_params.items()):
                col1, col2, col3 = st.columns([2, 2, 1])
                with col1:
                    new_name = st.text_input(
                        "Имя параметра",
                        value=param_name,
                        key=f"custom_name_{i}"
                    )
                with col2:
                    new_value = st.text_input(
                        "Значение по умолчанию",
                        value=str(param_value),
                        key=f"custom_value_{i}"
                    )
                with col3:
                    if st.button("🗑️", key=f"remove_custom_{i}", help="Удалить параметр"):
                        custom_params_to_remove.append(param_name)
                
                # Обновляем если изменилось
                if new_name != param_name or new_value != param_value:
                    if param_name in custom_params:
                        del custom_params[param_name]
                    if new_name.strip():
                        custom_params[new_name.strip()] = new_value
            
            # Удаляем отмеченные параметры
            for param_to_remove in custom_params_to_remove:
                if param_to_remove in custom_params:
                    del custom_params[param_to_remove]
            
            # Объединяем все параметры
            final_inputs = {}
            final_inputs.update(inputs_config)
            final_inputs.update(custom_params)
        
        # Глобальные настройки
        with st.expander("⚙️ Глобальные настройки", expanded=False):
            col1, col2 = st.columns(2)
            
            with col1:
                st.markdown("**Политика повторов:**")
                # Безопасно получаем значения повторов
                default_retries = 2
                default_base_delay = 1.0
                default_max_delay = 30.0
                if is_editing and "pipeline_info" in st.session_state:
                    default_retries = st.session_state.pipeline_info.get("max_retries", 2)
                    default_base_delay = st.session_state.pipeline_info.get("base_delay", 1.0)
                    default_max_delay = st.session_state.pipeline_info.get("max_delay", 30.0)
                
                max_retries = st.number_input(
                    "Максимум повторов", 
                    value=default_retries,
                    min_value=0, max_value=10
                )
                base_delay = st.number_input(
                    "Базовая задержка (сек)", 
                    value=default_base_delay,
                    min_value=0.1, max_value=60.0
                )
                max_delay = st.number_input(
                    "Максимальная задержка (сек)", 
                    value=default_max_delay,
                    min_value=1.0, max_value=300.0
                )
            
            with col2:
                st.markdown("**Ограничения ресурсов:**")
                # Безопасно получаем ограничения ресурсов
                default_duration = 600
                default_api_calls = 15
                if is_editing and "pipeline_info" in st.session_state:
                    default_duration = st.session_state.pipeline_info.get("max_duration", 600)
                    default_api_calls = st.session_state.pipeline_info.get("max_api_calls", 15)
                
                max_duration = st.number_input(
                    "Максимальное время (сек)", 
                    value=default_duration,
                    min_value=30, max_value=3600
                )
                max_api_calls = st.number_input(
                    "Максимум API вызовов/мин", 
                    value=default_api_calls,
                    min_value=1, max_value=100
                )
        
        # Расширенные настройки пайплайна
        with st.expander("🔧 Расширенные настройки пайплайна", expanded=False):
            col1, col2 = st.columns(2)
            
            with col1:
                st.markdown("**Уведомления:**")
                notifications_enabled = st.checkbox("Включить уведомления")
                if notifications_enabled:
                    notification_emails = st.text_area(
                        "Email адреса (по одному на строку)",
                        placeholder="admin@company.com\nteam@company.com",
                        help="Список email адресов для уведомлений"
                    )
                    notification_slack = st.text_input(
                        "Slack канал (опционально)",
                        placeholder="#workflow-notifications",
                        help="Канал Slack для уведомлений"
                    )
                    notification_webhook = st.text_input(
                        "Webhook URL (опционально)",
                        placeholder="https://api.company.com/webhook",
                        help="URL для webhook уведомлений"
                    )
                else:
                    notification_emails = ""
                    notification_slack = ""
                    notification_webhook = ""
                
                st.markdown("**Параллельное выполнение:**")
                parallel_groups_enabled = st.checkbox("Использовать параллельные группы")
                if parallel_groups_enabled:
                    parallel_groups_config = st.text_area(
                        "Конфигурация параллельных групп",
                        placeholder="group1: step1,step2\ngroup2: step3,step4",
                        help="Группы шагов для параллельного выполнения (группа: шаги через запятую)"
                    )
                else:
                    parallel_groups_config = ""
            
            with col2:
                st.markdown("**Обработка ошибок:**")
                error_handling_strategy = st.selectbox(
                    "Стратегия при ошибках",
                    ["continue", "pause_and_debug", "rollback", "continue_partial", "pause_and_notify"],
                    help="Что делать при возникновении ошибок"
                )
                
                auto_retry_transient = st.checkbox(
                    "Автоматически повторять временные ошибки",
                    value=True
                )
                
                save_partial_results = st.checkbox(
                    "Сохранять частичные результаты",
                    value=True
                )
                
                checkpoint_interval = st.number_input(
                    "Интервал чекпоинтов (сек)",
                    value=300,
                    min_value=60,
                    max_value=3600,
                    help="Как часто сохранять прогресс"
                )
                
                st.markdown("**Эскалация:**")
                escalation_enabled = st.checkbox("Включить эскалацию ошибок")
                if escalation_enabled:
                    escalation_levels = st.number_input(
                        "Уровней эскалации",
                        value=3,
                        min_value=1,
                        max_value=5
                    )
                    escalation_wait_time = st.number_input(
                        "Время ожидания (мин)",
                        value=5,
                        min_value=1,
                        max_value=60
                    )
                else:
                    escalation_levels = 0
                    escalation_wait_time = 0
        
        submitted_info = st.form_submit_button("💾 Сохранить основную информацию", type="primary")
        
        if submitted_info:
            if not pipeline_name.strip():
                st.error("❌ Название пайплайна обязательно!")
            elif not pipeline_description.strip():
                st.error("❌ Описание пайплайна обязательно!")
            else:
                # Сохраняем основную информацию в session_state
                st.session_state.pipeline_info = {
                    "name": pipeline_name.strip(),
                    "version": pipeline_version.strip(),
                    "description": pipeline_description.strip(),
                    "type": pipeline_type,
                    "category": pipeline_category,
                    "estimated_duration": estimated_duration.strip(),
                    "complexity": complexity,
                    "max_retries": max_retries,
                    "base_delay": base_delay,
                    "max_delay": max_delay,
                    "max_duration": max_duration,
                    "max_api_calls": max_api_calls,
                    
                    # Входные параметры
                    "inputs": final_inputs,
                    
                    # Расширенные настройки
                    "notifications_enabled": notifications_enabled,
                    "notification_emails": notification_emails,
                    "notification_slack": notification_slack,
                    "notification_webhook": notification_webhook,
                    "parallel_groups_enabled": parallel_groups_enabled,
                    "parallel_groups_config": parallel_groups_config,
                    "error_handling_strategy": error_handling_strategy,
                    "auto_retry_transient": auto_retry_transient,
                    "save_partial_results": save_partial_results,
                    "checkpoint_interval": checkpoint_interval,
                    "escalation_enabled": escalation_enabled,
                    "escalation_levels": escalation_levels,
                    "escalation_wait_time": escalation_wait_time
                }
                st.success("✅ Основная информация сохранена!")
    
    # Отдельная форма для добавления кастомных параметров (вне основной формы)
    if "pipeline_info" in st.session_state:
        st.markdown("---")
        st.markdown("### ➕ Добавить кастомный параметр")
        
        with st.form("add_custom_param"):
            st.markdown("**Добавить новый входной параметр:**")
            col1, col2, col3 = st.columns([2, 2, 1])
            
            with col1:
                new_param_name = st.text_input("Имя нового параметра", placeholder="my_parameter")
            with col2:
                new_param_value = st.text_input("Значение по умолчанию", placeholder="")
            with col3:
                st.markdown(" ")  # Отступ для выравнивания
                if st.form_submit_button("➕ Добавить"):
                    if new_param_name.strip():
                        # Обновляем inputs в session_state
                        if "inputs" not in st.session_state.pipeline_info:
                            st.session_state.pipeline_info["inputs"] = {}
                        st.session_state.pipeline_info["inputs"][new_param_name.strip()] = new_param_value
                        st.success(f"✅ Параметр '{new_param_name}' добавлен!")
                        st.rerun()
                    else:
                        st.error("❌ Введите имя параметра!")
    
    # Конструктор шагов
    st.markdown("---")
    st.markdown("### 🔗 Конструктор шагов")
    
    if "pipeline_info" not in st.session_state:
        st.info("📝 Сначала заполните и сохраните основную информацию о пайплайне")
        return
    
    # Добавление нового шага
    with st.expander("➕ Добавить новый шаг", expanded=True):
        col1, col2 = st.columns(2)
        
        with col1:
            step_id = st.text_input(
                "ID шага *",
                placeholder="step_1",
                help="Уникальный идентификатор шага"
            )
            
            step_type = st.selectbox(
                "Тип шага *",
                ["agent", "tool"],
                help="Выберите тип выполнения шага"
            )
            
            if step_type == "agent":
                step_executor = st.selectbox(
                    "Агент *",
                    st.session_state.available_agents,
                    help="Выберите агента для выполнения шага"
                )
            else:
                step_executor = st.selectbox(
                    "Инструмент *",
                    st.session_state.available_tools,
                    help="Выберите инструмент для выполнения шага"
                )
        
        with col2:
            step_task = st.text_area(
                "Задача *",
                placeholder="Описание задачи для этого шага...",
                help="Подробное описание того, что должен делать этот шаг"
            )
            
            step_timeout = st.number_input(
                "Таймаут (сек)",
                value=120,
                min_value=10,
                max_value=1800,
                help="Максимальное время выполнения шага"
            )
            
            depends_on = st.multiselect(
                "Зависит от шагов",
                [step["id"] for step in st.session_state.constructor_steps],
                help="Выберите шаги, которые должны завершиться перед этим"
            )
        
        # Расширенные настройки шага
        with st.expander("🔧 Расширенные настройки шага", expanded=False):
            col1, col2 = st.columns(2)
            
            with col1:
                st.markdown("**Условие выполнения:**")
                step_condition = st.text_input(
                    "Условие (опционально)",
                    placeholder="например: prev_step.output.status == 'success'",
                    help="Условие для выполнения шага"
                )
                
                st.markdown("**Действие при откате:**")
                rollback_action = st.text_input(
                    "Действие отката (опционально)",
                    placeholder="например: revert_to_checkpoint",
                    help="Действие при необходимости отката"
                )
                
                # Индивидуальная политика повторов
                st.markdown("**Индивидуальная политика повторов:**")
                use_custom_retry = st.checkbox("Использовать индивидуальные настройки повторов")
                
                if use_custom_retry:
                    step_max_retries = st.number_input(
                        "Макс. повторов для шага", 
                        value=2, min_value=0, max_value=10
                    )
                    step_backoff_strategy = st.selectbox(
                        "Стратегия задержки",
                        ["exponential", "linear", "fixed"]
                    )
                    step_base_delay = st.number_input(
                        "Базовая задержка (сек)", 
                        value=1.0, min_value=0.1, max_value=60.0
                    )
                else:
                    step_max_retries = None
                    step_backoff_strategy = None
                    step_base_delay = None
            
            with col2:
                # Ограничения ресурсов для шага
                st.markdown("**Ограничения ресурсов для шага:**")
                use_step_limits = st.checkbox("Использовать индивидуальные ограничения")
                
                if use_step_limits:
                    step_max_duration = st.number_input(
                        "Макс. время шага (сек)", 
                        value=300, min_value=10, max_value=3600
                    )
                    step_max_memory = st.number_input(
                        "Макс. память (MB)", 
                        value=512, min_value=64, max_value=4096
                    )
                    step_max_api_calls = st.number_input(
                        "Макс. API вызовов/мин", 
                        value=5, min_value=1, max_value=50
                    )
                else:
                    step_max_duration = None
                    step_max_memory = None
                    step_max_api_calls = None
                
                # Метаданные шага
                st.markdown("**Метаданные шага:**")
                step_priority = st.selectbox(
                    "Приоритет",
                    ["low", "normal", "high"],
                    index=1
                )
                
                step_tags = st.text_input(
                    "Теги (через запятую)",
                    placeholder="research, analysis, critical",
                    help="Теги для категоризации шага"
                )
        
        # Специальные параметры для инструментов
        if step_type == "tool":
            with st.expander("🛠️ Параметры инструмента", expanded=False):
                st.markdown("**Дополнительные параметры для инструмента:**")
                
                # Основные параметры, которые часто используются
                tool_session_id = st.text_input(
                    "Session ID",
                    value="{session_id}",
                    help="ID сессии для инструмента"
                )
                
                if step_executor == "web_search_tool":
                    tool_query = st.text_input(
                        "Поисковый запрос",
                        value="{topic}",
                        help="Запрос для веб-поиска"
                    )
                    tool_max_results = st.number_input(
                        "Максимум результатов",
                        value=5, min_value=1, max_value=20
                    )
                elif step_executor == "generate_image_tool":
                    tool_prompt = st.text_input(
                        "Промпт для изображения",
                        value="Generate image based on: {topic}",
                        help="Описание для генерации изображения"
                    )
                    tool_number = st.number_input(
                        "Количество изображений",
                        value=1, min_value=1, max_value=5
                    )
                elif step_executor == "code_interpreter_tool":
                    tool_code_prompt = st.text_area(
                        "Код/Промпт",
                        placeholder="Опишите, какой код нужно выполнить...",
                        help="Описание кода для выполнения"
                    )
                    tool_data_path = st.text_input(
                        "Путь к данным (опционально)",
                        help="Путь к файлам данных"
                    )
        
        # Специальные параметры для менеджеров
        elif step_type == "agent" and step_executor == "manager":
            with st.expander("👥 Настройки менеджера", expanded=False):
                st.markdown("**Предзагруженная команда агентов:**")
                
                available_agents_for_manager = [
                    agent for agent in st.session_state.available_agents 
                    if agent != "manager"
                ]
                
                preload_agents = st.multiselect(
                    "Выберите агентов для команды",
                    available_agents_for_manager,
                    help="Агенты, которые будут доступны менеджеру"
                )
                
                pipeline_type_for_manager = st.selectbox(
                    "Тип пайплайна для менеджера",
                    ["general_tasks", "educational_content", "data_analysis", "research"],
                    help="Специализация команды менеджера"
                )
        
        if st.button("➕ Добавить шаг"):
            if not step_id.strip():
                st.error("❌ ID шага обязателен!")
            elif not step_task.strip():
                st.error("❌ Описание задачи обязательно!")
            elif any(step["id"] == step_id.strip() for step in st.session_state.constructor_steps):
                st.error("❌ Шаг с таким ID уже существует!")
            else:
                new_step = {
                    "id": step_id.strip(),
                    "step_type": step_type,
                    "executor": step_executor,
                    "task": step_task.strip(),
                    "timeout": step_timeout,
                    "depends_on": depends_on
                }
                
                # Добавляем расширенные параметры
                if step_condition:
                    new_step["condition"] = step_condition
                if rollback_action:
                    new_step["rollback_action"] = rollback_action
                
                # Индивидуальная политика повторов
                if use_custom_retry:
                    new_step["retry_policy"] = {
                        "max_retries": step_max_retries,
                        "backoff_strategy": step_backoff_strategy,
                        "base_delay": step_base_delay
                    }
                
                # Ограничения ресурсов шага
                if use_step_limits:
                    new_step["resource_limits"] = {
                        "max_duration_seconds": step_max_duration,
                        "max_memory_mb": step_max_memory,
                        "max_api_calls_per_minute": step_max_api_calls
                    }
                
                # Метаданные шага
                step_metadata = {
                    "priority": step_priority
                }
                if step_tags:
                    step_metadata["tags"] = [tag.strip() for tag in step_tags.split(",") if tag.strip()]
                
                # Специальные параметры для инструментов
                if step_type == "tool":
                    tool_params = {"session_id": tool_session_id}
                    
                    if step_executor == "web_search_tool":
                        tool_params.update({
                            "query": tool_query,
                            "max_results": tool_max_results
                        })
                    elif step_executor == "generate_image_tool":
                        tool_params.update({
                            "prompt": tool_prompt,
                            "number": tool_number
                        })
                    elif step_executor == "code_interpreter_tool":
                        tool_params["code_prompt"] = tool_code_prompt
                        if tool_data_path:
                            tool_params["data_path"] = tool_data_path
                    
                    new_step["tool_params"] = tool_params
                
                # Специальные параметры для менеджеров
                elif step_type == "agent" and step_executor == "manager":
                    if preload_agents:
                        step_metadata["preload_agents"] = preload_agents
                    step_metadata["pipeline_type"] = pipeline_type_for_manager
                
                new_step["metadata"] = step_metadata
                
                st.session_state.constructor_steps.append(new_step)
                st.success(f"✅ Шаг '{step_id}' добавлен!")
                st.rerun()
    
    # Отображение текущих шагов с расширенным управлением
    if st.session_state.constructor_steps:
        st.markdown("### 📋 Текущие шаги пайплайна")
        st.markdown("*Используйте кнопки для изменения порядка или вставки новых шагов*")
        
        # Функция для перемещения шага с валидацией зависимостей
        def move_step(from_index, to_index):
            if 0 <= from_index < len(st.session_state.constructor_steps) and 0 <= to_index < len(st.session_state.constructor_steps):
                # Получаем шаг который перемещаем
                moving_step = st.session_state.constructor_steps[from_index]
                
                # Проверяем конфликты зависимостей
                conflicts = validate_step_dependencies_after_move(from_index, to_index)
                
                if conflicts:
                    st.warning(f"⚠️ Обнаружены конфликты зависимостей: {', '.join(conflicts)}")
                    if st.button("🔧 Исправить автоматически", key=f"fix_deps_{from_index}_{to_index}"):
                        fix_dependencies_after_move(from_index, to_index)
                        step = st.session_state.constructor_steps.pop(from_index)
                        st.session_state.constructor_steps.insert(to_index, step)
                        st.success("✅ Зависимости исправлены автоматически!")
                        return True
                    return False
                else:
                    # Нет конфликтов, безопасно перемещаем
                    step = st.session_state.constructor_steps.pop(from_index)
                    st.session_state.constructor_steps.insert(to_index, step)
                    return True
            return False
        
        # Функция валидации зависимостей
        def validate_step_dependencies_after_move(from_index, to_index):
            conflicts = []
            moving_step = st.session_state.constructor_steps[from_index]
            
            # Создаем временный список для проверки
            temp_steps = st.session_state.constructor_steps.copy()
            step = temp_steps.pop(from_index)
            temp_steps.insert(to_index, step)
            
            # Проверяем все шаги на конфликты зависимостей
            for i, current_step in enumerate(temp_steps):
                if current_step.get('depends_on'):
                    for dep_id in current_step['depends_on']:
                        # Ищем индекс зависимости
                        dep_index = None
                        for j, dep_step in enumerate(temp_steps):
                            if dep_step['id'] == dep_id:
                                dep_index = j
                                break
                        
                        # Если зависимость находится после текущего шага - конфликт
                        if dep_index is not None and dep_index > i:
                            conflicts.append(f"{current_step['id']} зависит от {dep_id}")
            
            return conflicts
        
        # Функция автоматического исправления зависимостей
        def fix_dependencies_after_move(from_index, to_index):
            moving_step = st.session_state.constructor_steps[from_index]
            
            # Создаем временный список
            temp_steps = st.session_state.constructor_steps.copy()
            step = temp_steps.pop(from_index)
            temp_steps.insert(to_index, step)
            
            # Исправляем конфликты
            for i, current_step in enumerate(temp_steps):
                if current_step.get('depends_on'):
                    valid_deps = []
                    for dep_id in current_step['depends_on']:
                        # Ищем индекс зависимости
                        dep_index = None
                        for j, dep_step in enumerate(temp_steps):
                            if dep_step['id'] == dep_id:
                                dep_index = j
                                break
                        
                        # Добавляем только валидные зависимости (которые идут раньше)
                        if dep_index is not None and dep_index < i:
                            valid_deps.append(dep_id)
                    
                    # Обновляем зависимости шага
                    current_step['depends_on'] = valid_deps
        
        # Функция для вставки нового шага в позицию
        def insert_step_at(position):
            new_step = {
                "id": f"new_step_{len(st.session_state.constructor_steps) + 1}",
                "step_type": "agent",
                "executor": "researcher",
                "task": "Новая задача",
                "timeout": 120,
                "depends_on": []
            }
            st.session_state.constructor_steps.insert(position, new_step)
            return True
        
        # Отображение шагов с кнопками управления
        for i, step in enumerate(st.session_state.constructor_steps):
            # Контейнер для каждого шага
            step_container = st.container()
            
            with step_container:
                # Заголовок с номером позиции
                col_header, col_actions = st.columns([3, 1])
                with col_header:
                    st.markdown(f"**{i+1}.** 🔗 **{step['id']}** ({step['step_type']})")
                
                with col_actions:
                    # Кнопки управления в одной строке
                    action_col1, action_col2, action_col3, action_col4, action_col5, action_col6 = st.columns(6)
                    
                    with action_col1:
                        # Кнопка "Вверх"
                        if i > 0:  # Не для первого элемента
                            if st.button("⬆️", key=f"up_{i}", help="Переместить вверх"):
                                move_step(i, i-1)
                                st.rerun()
                        else:
                            st.markdown("⬆️", help="Первый шаг")
                    
                    with action_col2:
                        # Кнопка "Вниз"
                        if i < len(st.session_state.constructor_steps) - 1:  # Не для последнего элемента
                            if st.button("⬇️", key=f"down_{i}", help="Переместить вниз"):
                                move_step(i, i+1)
                                st.rerun()
                        else:
                            st.markdown("⬇️", help="Последний шаг")
                    
                    with action_col3:
                        # Кнопка "Вставить до"
                        if st.button("➕⬆️", key=f"insert_before_{i}", help="Вставить шаг перед этим"):
                            insert_step_at(i)
                            st.rerun()
                    
                    with action_col4:
                        # Кнопка "Вставить после"
                        if st.button("➕⬇️", key=f"insert_after_{i}", help="Вставить шаг после этого"):
                            insert_step_at(i + 1)
                            st.rerun()
                    
                    with action_col5:
                        # Кнопка "Редактировать"
                        if st.button("✏️", key=f"edit_{i}", help="Редактировать шаг"):
                            st.session_state[f"editing_step_{i}"] = True
                            st.rerun()
                    
                    with action_col6:
                        # Кнопка "Удалить"
                        if st.button("🗑️", key=f"delete_{i}", help="Удалить шаг"):
                            deleted_step_id = step['id']
                            st.session_state.constructor_steps.pop(i)
                            st.success(f"✅ Шаг '{deleted_step_id}' удален!")
                            st.rerun()
                
                # Проверяем, находится ли шаг в режиме редактирования
                if f"editing_step_{i}" in st.session_state and st.session_state[f"editing_step_{i}"]:
                    # Форма редактирования шага
                    with st.form(f"edit_step_form_{i}"):
                        st.markdown(f"#### ✏️ Редактирование шага #{i+1}")
                        
                        edit_col1, edit_col2 = st.columns(2)
                        
                        with edit_col1:
                            new_id = st.text_input("ID шага", value=step['id'], key=f"edit_id_{i}")
                            new_step_type = st.selectbox("Тип шага", ["agent", "tool"], 
                                                       index=0 if step['step_type'] == 'agent' else 1, 
                                                       key=f"edit_type_{i}")
                            if new_step_type == "agent":
                                new_executor = st.selectbox("Агент", st.session_state.available_agents,
                                                          index=st.session_state.available_agents.index(step['executor']) 
                                                          if step['executor'] in st.session_state.available_agents else 0,
                                                          key=f"edit_executor_{i}")
                            else:
                                new_executor = st.selectbox("Инструмент", st.session_state.available_tools,
                                                          index=st.session_state.available_tools.index(step['executor']) 
                                                          if step['executor'] in st.session_state.available_tools else 0,
                                                          key=f"edit_tool_{i}")
                        
                        with edit_col2:
                            new_task = st.text_area("Задача", value=step['task'], key=f"edit_task_{i}")
                            new_timeout = st.number_input("Таймаут (сек)", value=step['timeout'], 
                                                        min_value=10, max_value=3600, key=f"edit_timeout_{i}")
                        
                        # Зависимости
                        available_step_ids = [s['id'] for j, s in enumerate(st.session_state.constructor_steps) if j != i]
                        if available_step_ids:
                            new_depends_on = st.multiselect("Зависимости", available_step_ids, 
                                                          default=step.get('depends_on', []), 
                                                          key=f"edit_depends_{i}")
                        else:
                            new_depends_on = []
                            st.info("Нет доступных шагов для зависимостей")
                        
                        # Кнопки
                        save_col, cancel_col = st.columns(2)
                        with save_col:
                            if st.form_submit_button("💾 Сохранить изменения", type="primary"):
                                # Обновляем шаг
                                st.session_state.constructor_steps[i] = {
                                    "id": new_id,
                                    "step_type": new_step_type,
                                    "executor": new_executor,
                                    "task": new_task,
                                    "timeout": new_timeout,
                                    "depends_on": new_depends_on
                                }
                                # Выходим из режима редактирования
                                del st.session_state[f"editing_step_{i}"]
                                st.success(f"✅ Шаг '{new_id}' обновлен!")
                                st.rerun()
                        
                        with cancel_col:
                            if st.form_submit_button("❌ Отменить"):
                                del st.session_state[f"editing_step_{i}"]
                                st.rerun()
                
                else:
                    # Обычное отображение информации о шаге
                    display_col1, display_col2 = st.columns(2)
                    
                    with display_col1:
                        st.markdown(f"**Исполнитель:** {step['executor']}")
                        st.markdown(f"**Таймаут:** {step['timeout']}с")
                    
                    with display_col2:
                        st.markdown(f"**Задача:** {step['task']}")
                        if step.get('depends_on'):
                            st.markdown(f"**Зависимости:** {', '.join(step['depends_on'])}")
                
                # Разделитель между шагами
                st.markdown("---")
        
        # Кнопка добавления шага в конец
        if st.button("➕ Добавить шаг в конец", key="add_step_end"):
            new_step = {
                "id": f"step_{len(st.session_state.constructor_steps) + 1}",
                "step_type": "agent",
                "executor": "researcher",
                "task": "Новая задача",
                "timeout": 120,
                "depends_on": []
            }
            st.session_state.constructor_steps.append(new_step)
            st.success("✅ Новый шаг добавлен в конец!")
            st.rerun()
        
        # Предварительный просмотр YAML
        st.markdown("---")
        st.markdown("### 👁️ Предварительный просмотр YAML")
        
        try:
            yaml_content = generate_pipeline_yaml(
                st.session_state.pipeline_info,
                st.session_state.constructor_steps
            )
            st.code(yaml_content, language='yaml')
            
            # Кнопки сохранения
            if is_editing:
                col1, col2, col3 = st.columns([1, 1, 2])
            else:
                col1, col2 = st.columns([1, 3])
            
            with col1:
                save_button_text = "💾 Обновить пайплайн" if is_editing else "💾 Сохранить пайплайн"
                if st.button(save_button_text, type="primary"):
                    if is_editing:
                        # Режим редактирования - перезаписываем существующий файл
                        success, message = save_pipeline_to_file_edit_mode(
                            st.session_state.editing_pipeline_file,
                            yaml_content
                        )
                    else:
                        # Режим создания нового пайплайна
                        success, message = save_pipeline_to_file(
                            st.session_state.pipeline_info["name"],
                            yaml_content
                        )
                    
                    if success:
                        st.success(f"✅ {message}")
                        # Очищаем конструктор
                        st.session_state.constructor_steps = []
                        if "pipeline_info" in st.session_state:
                            del st.session_state.pipeline_info
                        if is_editing:
                            del st.session_state.editing_pipeline
                            del st.session_state.editing_pipeline_file
                        # Обновляем список пайплайнов
                        st.cache_data.clear()
                        st.success("🔄 Список пайплайнов обновлен!")
                        time.sleep(1)
                        st.switch_page("pages/02_Workflows.py")
                    else:
                        st.error(f"❌ {message}")
            
            # Кнопка "Сохранить как" только в режиме редактирования
            if is_editing:
                with col2:
                    if st.button("📄 Сохранить как...", help="Создать копию с новым именем"):
                        st.session_state.show_save_as_dialog = True
                        st.rerun()
            
            with col2 if not is_editing else col3:
                if st.button("🗑️ Очистить конструктор"):
                    st.session_state.constructor_steps = []
                    if "pipeline_info" in st.session_state:
                        del st.session_state.pipeline_info
                    st.success("✅ Конструктор очищен!")
                    st.rerun()
        
        except Exception as e:
            st.error(f"❌ Ошибка генерации YAML: {e}")
        
        # Диалог "Сохранить как"
        if st.session_state.get("show_save_as_dialog", False):
            st.markdown("---")
            with st.form("save_as_form"):
                st.markdown("### 📄 Сохранить как")
                st.markdown("Создайте копию пайплайна с новым именем")
                
                # Получаем текущее имя для предложения
                current_name = ""
                if "pipeline_info" in st.session_state:
                    current_name = st.session_state.pipeline_info.get("name", "")
                
                new_name = st.text_input(
                    "Новое имя пайплайна *",
                    value=f"{current_name}_copy" if current_name else "pipeline_copy",
                    placeholder="my_pipeline_v2",
                    help="Имя файла должно быть уникальным"
                )
                
                # Проверяем существование файла
                file_exists = False
                if new_name:
                    import os
                    target_path = f"workflow_pipelines/{new_name}.yaml"
                    file_exists = os.path.exists(target_path)
                
                if file_exists:
                    st.warning(f"⚠️ Файл '{new_name}.yaml' уже существует!")
                
                form_col1, form_col2 = st.columns(2)
                
                with form_col1:
                    if st.form_submit_button("💾 Сохранить копию", 
                                            type="primary", 
                                            disabled=not new_name or file_exists):
                        if new_name and not file_exists:
                            # Создаем копию pipeline_info с новым именем
                            pipeline_info_copy = st.session_state.pipeline_info.copy()
                            pipeline_info_copy["name"] = new_name
                            
                            # Генерируем YAML с новым именем
                            yaml_content = generate_pipeline_yaml(
                                pipeline_info_copy,
                                st.session_state.constructor_steps
                            )
                            
                            # Сохраняем как новый файл
                            success, message = save_pipeline_to_file(new_name, yaml_content)
                            
                            if success:
                                st.success(f"✅ Копия сохранена как '{new_name}.yaml'")
                                # Не очищаем конструктор, продолжаем редактирование исходного
                                st.session_state.show_save_as_dialog = False
                                st.rerun()
                            else:
                                st.error(f"❌ {message}")
                
                with form_col2:
                    if st.form_submit_button("❌ Отмена"):
                        st.session_state.show_save_as_dialog = False
                        st.rerun()
    
    else:
        st.info("📝 Добавьте шаги для создания пайплайна")

def generate_pipeline_yaml(pipeline_info, steps):
    """Генерация YAML файла пайплайна"""
    import yaml
    
    # Структура пайплайна
    pipeline_data = {
        "name": pipeline_info["name"],
        "version": pipeline_info["version"],
        "description": pipeline_info["description"],
        
        # Добавляем секцию inputs из настроек
        "inputs": pipeline_info.get("inputs", {"topic": ""}),
        
        "global_retry_policy": {
            "max_retries": pipeline_info["max_retries"],
            "backoff_strategy": "exponential",
            "base_delay": pipeline_info["base_delay"],
            "max_delay": pipeline_info["max_delay"],
            "retry_on_errors": [
                "network_error",
                "rate_limit", 
                "timeout"
            ]
        },
        "global_resource_limits": {
            "max_duration_seconds": pipeline_info["max_duration"],
            "max_api_calls_per_minute": pipeline_info["max_api_calls"]
        },
        "steps": []
    }
    
    # Добавляем шаги
    for step in steps:
        step_data = {
            "id": step["id"],
            "step_type": step["step_type"],
            "task": step["task"],
            "timeout": step["timeout"]
        }
        
        if step["step_type"] == "agent":
            step_data["agent_type"] = step["executor"]
        else:
            step_data["tool_name"] = step["executor"]
            # Используем сохраненные tool_params если есть
            if "tool_params" in step:
                step_data["tool_params"] = step["tool_params"]
            else:
                step_data["tool_params"] = {"session_id": "{session_id}"}
        
        if step["depends_on"]:
            step_data["depends_on"] = step["depends_on"]
        
        # Добавляем расширенные параметры шага
        if "condition" in step:
            step_data["condition"] = step["condition"]
        if "rollback_action" in step:
            step_data["rollback_action"] = step["rollback_action"]
        if "retry_policy" in step:
            step_data["retry_policy"] = step["retry_policy"]
        if "resource_limits" in step:
            step_data["resource_limits"] = step["resource_limits"]
        if "metadata" in step:
            step_data["metadata"] = step["metadata"]
        
        pipeline_data["steps"].append(step_data)
    
    # Добавляем параллельные группы если они настроены
    if pipeline_info.get("parallel_groups_enabled") and pipeline_info.get("parallel_groups_config"):
        parallel_groups = []
        for line in pipeline_info["parallel_groups_config"].split("\n"):
            if ":" in line:
                group_name, steps_str = line.split(":", 1)
                group_steps = [s.strip() for s in steps_str.split(",") if s.strip()]
                if group_steps:
                    parallel_groups.append({
                        "name": group_name.strip(),
                        "steps": group_steps
                    })
        if parallel_groups:
            pipeline_data["parallel_groups"] = parallel_groups
    
    # Добавляем уведомления если они настроены
    if pipeline_info.get("notifications_enabled"):
        notifications = []
        
        if pipeline_info.get("notification_emails"):
            for email in pipeline_info["notification_emails"].split("\n"):
                email = email.strip()
                if email:
                    notifications.append(f"email:{email}")
        
        if pipeline_info.get("notification_slack"):
            notifications.append(f"slack:{pipeline_info['notification_slack']}")
        
        if pipeline_info.get("notification_webhook"):
            notifications.append(f"webhook:{pipeline_info['notification_webhook']}")
        
        if notifications:
            pipeline_data["notifications"] = notifications
    
    # Добавляем обработку ошибок
    error_handling = {
        "on_failure": pipeline_info.get("error_handling_strategy", "continue"),
        "auto_retry_transient": pipeline_info.get("auto_retry_transient", True),
        "save_partial_results": pipeline_info.get("save_partial_results", True),
        "checkpoint_strategy": "after_each_step",
        "save_checkpoint_interval": pipeline_info.get("checkpoint_interval", 300)
    }
    
    # Добавляем эскалацию если настроена
    if pipeline_info.get("escalation_enabled"):
        escalation_policy = []
        levels = pipeline_info.get("escalation_levels", 3)
        wait_time = pipeline_info.get("escalation_wait_time", 5)
        
        for level in range(1, levels + 1):
            escalation_policy.append({
                "level": level,
                "wait_minutes": wait_time * level,
                "action": "auto_retry" if level == 1 else "notify_admin" if level == 2 else "manual_intervention"
            })
        
        error_handling["escalation_policy"] = escalation_policy
    
    pipeline_data["error_handling"] = error_handling
    
    # Метаданные
    pipeline_data["metadata"] = {
        "author": "Pipeline Constructor",
        "category": pipeline_info["category"],
        "estimated_duration": pipeline_info["estimated_duration"],
        "complexity": pipeline_info["complexity"],
        "engine_type": pipeline_info["type"],
        "tags": ["constructed", pipeline_info["category"], f"engine_{pipeline_info['type']}"]
    }
    
    return yaml.dump(pipeline_data, default_flow_style=False, allow_unicode=True, sort_keys=False)

def save_pipeline_to_file(pipeline_name, yaml_content):
    """Сохранение пайплайна в файл"""
    try:
        from pathlib import Path
        
        # Проверяем имя файла
        safe_name = "".join(c for c in pipeline_name if c.isalnum() or c in "._-").lower()
        if not safe_name:
            return False, "Недопустимое имя пайплайна"
        
        # Путь к файлу
        pipelines_dir = Path.cwd() / "workflow_pipelines"
        pipelines_dir.mkdir(exist_ok=True)
        
        pipeline_file = pipelines_dir / f"{safe_name}.yaml"
        
        # Проверяем, не существует ли уже файл
        if pipeline_file.exists():
            return False, f"Пайплайн с именем '{safe_name}' уже существует"
        
        # Сохраняем файл
        with open(pipeline_file, 'w', encoding='utf-8') as f:
            f.write(yaml_content)
        
        return True, f"Пайплайн сохранен как '{safe_name}.yaml'"
        
    except Exception as e:
        return False, f"Ошибка сохранения: {e}"

def save_pipeline_to_file_edit_mode(existing_file_path, yaml_content):
    """Сохранение пайплайна в режиме редактирования (перезапись существующего файла)"""
    try:
        from pathlib import Path
        
        pipeline_file = Path(existing_file_path)
        
        # Создаем резервную копию
        backup_file = pipeline_file.with_suffix(f".backup_{int(time.time())}.yaml")
        if pipeline_file.exists():
            with open(pipeline_file, 'r', encoding='utf-8') as f:
                backup_content = f.read()
            with open(backup_file, 'w', encoding='utf-8') as f:
                f.write(backup_content)
        
        # Сохраняем обновленный файл
        with open(pipeline_file, 'w', encoding='utf-8') as f:
            f.write(yaml_content)
        
        return True, f"Пайплайн обновлен: '{pipeline_file.name}' (резервная копия: '{backup_file.name}')"
        
    except Exception as e:
        return False, f"Ошибка обновления: {e}"

def load_pipeline_for_editing(file_path):
    """Загрузка пайплайна для редактирования"""
    try:
        import yaml
        from pathlib import Path
        
        with open(file_path, 'r', encoding='utf-8') as f:
            yaml_data = yaml.safe_load(f)
        
        # Извлекаем основную информацию
        pipeline_info = {
            "name": yaml_data.get("name", ""),
            "version": yaml_data.get("version", "1.0"),
            "description": yaml_data.get("description", ""),
            "type": yaml_data.get("metadata", {}).get("engine_type", "simple"),
            "category": yaml_data.get("metadata", {}).get("category", "general"),
            "estimated_duration": yaml_data.get("metadata", {}).get("estimated_duration", "5 minutes"),
            "complexity": yaml_data.get("metadata", {}).get("complexity", "simple"),
        }
        
        # Извлекаем inputs
        pipeline_inputs = yaml_data.get("inputs", {})
        pipeline_info["inputs"] = pipeline_inputs
        
        # Извлекаем глобальные настройки
        global_retry = yaml_data.get("global_retry_policy", {})
        global_limits = yaml_data.get("global_resource_limits", {})
        
        pipeline_info.update({
            "max_retries": global_retry.get("max_retries", 2),
            "base_delay": global_retry.get("base_delay", 1.0),
            "max_delay": global_retry.get("max_delay", 30.0),
            "max_duration": global_limits.get("max_duration_seconds", 600),
            "max_api_calls": global_limits.get("max_api_calls_per_minute", 15)
        })
        
        # Извлекаем шаги
        steps = []
        for step_data in yaml_data.get("steps", []):
            step = {
                "id": step_data.get("id", ""),
                "task": step_data.get("task", ""),
                "timeout": step_data.get("timeout", 120),
                "depends_on": step_data.get("depends_on", [])
            }
            
            # Определяем тип шага и исполнителя
            if "agent_type" in step_data:
                step["step_type"] = "agent"
                step["executor"] = step_data["agent_type"]
            elif "tool_name" in step_data:
                step["step_type"] = "tool"
                step["executor"] = step_data["tool_name"]
            else:
                # Попробуем определить по step_type
                step_type = step_data.get("step_type", "agent")
                step["step_type"] = step_type
                if step_type == "tool":
                    step["executor"] = step_data.get("tool_name", "unknown_tool")
                else:
                    step["executor"] = step_data.get("agent_type", "unknown_agent")
            
            steps.append(step)
        
        return {
            "pipeline_info": pipeline_info,
            "steps": steps,
            "original_file": file_path
        }
        
    except Exception as e:
        st.error(f"Ошибка загрузки пайплайна: {e}")
        return None

if __name__ == "__main__":
    main()
