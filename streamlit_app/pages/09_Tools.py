"""
Страница инструментов и утилит
==============================
"""

import streamlit as st
import sys
from pathlib import Path
import json
from datetime import datetime
import base64

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

def main():
    st.set_page_config(
        page_title="Tools - MultiAgent System",
        page_icon="🛠️",
        layout="wide"
    )
    
    st.title("🛠️ Инструменты и утилиты")
    st.markdown("---")
    
    # Главные вкладки
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["📊 Диаграммы", "🎨 Изображения", "🔧 Утилиты", "📋 Доступные инструменты", "🤖 Создать агента"])
    
    with tab1:
        show_diagram_tools()
    
    with tab2:
        show_image_tools()
    
    with tab3:
        show_utility_tools()
    
    with tab4:
        show_available_tools()

    with tab5:
        show_agent_constructor_tab()

def show_diagram_tools():
    """Инструменты для создания диаграмм"""
    
    st.markdown("## 📊 Создание диаграмм через ИИ")
    st.markdown("Опишите, какую диаграмму вы хотите создать, и специализированный агент создаст её для вас!")
    
    with st.form("diagram_generation_form"):
        # Промпт пользователя
        prompt = st.text_area(
            "📝 Опишите диаграмму, которую хотите создать",
            height=120,
            placeholder="Например:\n- Создай блок-схему процесса авторизации пользователя\n- Нужна диаграмма классов для системы управления заказами\n- Покажи последовательность взаимодействия между клиентом, сервером и базой данных\n- ER-диаграмма для интернет-магазина с товарами, заказами и пользователями",
            help="Опишите максимально подробно, какую диаграмму вы хотите получить"
        )
        
        # Выбор типа диаграммы и агента
        col1, col2 = st.columns(2)
        with col1:
            diagram_type = st.selectbox(
                "🎨 Тип диаграммы",
                ["Mermaid", "PlantUML", "Автоопределение"],
                help="Выберите формат диаграммы или доверьте выбор агенту"
            )
        
        with col2:
            include_examples = st.checkbox(
                "📚 Включить примеры",
                value=True,
                help="Агент покажет похожие примеры диаграмм"
            )
        
        # Дополнительные параметры
        with st.expander("🔧 Дополнительные настройки"):
            
            detail_level = st.selectbox(
                "📊 Уровень детализации",
                ["Высокий", "Средний", "Базовый"],
                help="Насколько подробной должна быть диаграмма"
            )
            
        # Кнопка генерации
        submitted = st.form_submit_button("🤖 Создать диаграмму через ИИ", type="primary")
        
        if submitted:
            if prompt.strip():
                generate_diagram_from_prompt(
                    prompt=prompt,
                    diagram_type=diagram_type,
                    detail_level=detail_level,
                    include_examples=include_examples,
                )
            else:
                st.error("❌ Пожалуйста, опишите, какую диаграмму вы хотите создать")

def generate_diagram_from_prompt(prompt, diagram_type, detail_level, include_examples):
    """Генерация диаграммы по промпту пользователя"""
    
    st.markdown("### 🤖 Создание диаграммы по вашему описанию")
    
    try:
        import uuid
        import time
        import os
        import glob
        from agent_streamlit_api import AgentManager
        
        # Генерируем уникальный session_id в формате run-xxxxx
        session_id = f"run-{uuid.uuid4().hex[:16]}"
        
        # Определяем агента в зависимости от типа диаграммы
        if diagram_type == "PlantUML":
            agent_profile = "plantuml_creator"
            file_extension = "puml"
        elif diagram_type == "Mermaid":
            agent_profile = "diagram_creator"
            file_extension = "mmd"
        else:  # Автоопределение
            agent_profile = "diagram_creator"  # Он умеет определять подходящий тип
            file_extension = "mmd"
        
        with st.spinner(f"🤖 {agent_profile} создаёт диаграмму по вашему описанию..."):
            # Формируем детальную задачу для агента
            task = f"""Создай диаграмму по следующему описанию пользователя:

"{prompt}"

Требования к выполнению:
1. Уровень детализации: {detail_level}
2. Включить примеры: {'Да' if include_examples else 'Нет'}

Задачи:
1. Проанализируй описание и определи наиболее подходящий тип диаграммы
2. Создай структурную диаграмму, полно отражающую описание
3. Добавь профессиональное оформление в выбранном стиле
4. Если выбрана Mermaid - используй validate_mermaid_diagram для проверки
5. Верни:
   - Финальный код диаграммы
   - Объяснение структуры и элементов
   - Рекомендации по использованию

"""

            # Запускаем агента
            manager = AgentManager()
            run_id = manager.run_agent(
                agent_id_or_profile=agent_profile,
                task=task,
                session_id=session_id
            )
            
            # Ожидаем завершения
            max_wait_seconds = 180  # Больше времени для создания с нуля
            start_time = time.time()
            status_placeholder = st.empty()
            progress_placeholder = st.empty()
            
            while time.time() - start_time < max_wait_seconds:
                status = manager.get_agent_status(run_id)
                elapsed = int(time.time() - start_time)
                progress_placeholder.progress(min(elapsed / max_wait_seconds, 0.95))
                
                if status and getattr(status, 'status', '') in ("completed", "failed"):
                    break
                    
                status_placeholder.info(f"⏳ {agent_profile} создаёт диаграмму... ({elapsed}с)")
                time.sleep(1)
                
            status_placeholder.empty()
            progress_placeholder.empty()
            
            # Получаем результат
            result_obj = manager.get_agent_result(run_id)
            final_output = getattr(result_obj, 'final_output', None) if result_obj else None
            
            if final_output:
                st.success("✅ Диаграмма успешно создана!")
                
                # Показываем описание пользователя
                st.markdown("**📝 Ваше описание:**")
                st.info(prompt)
                
                # Показываем результат работы агента
                st.markdown("**🤖 Результат работы агента:**")
                st.text(final_output)
                
                # Ищем созданные файлы диаграмм
                diagram_files = []
                for ext in ['*.mmd', '*.puml', '*.svg', '*.png']:
                    diagram_files.extend(glob.glob(f"diagram_{session_id}*{ext}"))
                
                if diagram_files:
                    st.markdown("**🎨 Созданные диаграммы:**")
                    
                    for diagram_file in diagram_files:
                        st.markdown(f"**📁 Файл: {os.path.basename(diagram_file)}**")
                        try:
                            with open(diagram_file, 'r', encoding='utf-8') as f:
                                diagram_content = f.read()
                            
                            # Показываем диаграмму
                            if diagram_file.endswith('.mmd'):
                                st.markdown("**🔍 Предпросмотр:**")
                                st.markdown(f"```mermaid\n{diagram_content}\n```")
                                st.markdown("**📋 Код Mermaid:**")
                                st.code(diagram_content, language='text')
                            elif diagram_file.endswith('.puml'):
                                st.markdown("**📋 Код PlantUML:**")
                                st.code(diagram_content, language='text')
                            elif diagram_file.endswith(('.svg', '.png')):
                                st.image(diagram_file, caption=f"Диаграмма: {os.path.basename(diagram_file)}")
                            else:
                                st.code(diagram_content, language='text')
                                
                            # Кнопка скачивания
                            if diagram_file.endswith(('.mmd', '.puml')):
                                st.download_button(
                                    label=f"💾 Скачать {os.path.basename(diagram_file)}",
                                    data=diagram_content,
                                    file_name=os.path.basename(diagram_file),
                                    mime="text/plain",
                                    key=f"download_{os.path.basename(diagram_file)}_{session_id}"
                                )
                            
                        except Exception as read_error:
                            st.error(f"❌ Ошибка чтения файла {diagram_file}: {read_error}")
                            
                    # Информация о параметрах
                    with st.expander("ℹ️ Параметры создания", expanded=False):
                        st.markdown(f"**🎨 Тип диаграммы:** {diagram_type}")
                        st.markdown(f"**📊 Детализация:** {detail_level}")
                        st.markdown(f"**🤖 Агент:** {agent_profile}")
                        st.markdown(f"**🆔 Session ID:** {session_id}")
                        
                else:
                    st.warning("⚠️ Агент выполнил задачу, но файлы диаграмм не найдены")
                    
            else:
                st.error("❌ Агент не смог создать диаграмму")
                
    except Exception as e:
        st.error(f"❌ Ошибка создания диаграммы: {e}")
        import traceback
        with st.expander("🔍 Детали ошибки", expanded=False):
            st.code(traceback.format_exc())

def show_mermaid_editor():
    """Редактор Mermaid диаграмм"""
    
    st.markdown("### 📊 Mermaid диаграммы")
    
    # Шаблоны
    templates = {
        "Пустая": "",
        "Граф": """graph TD
    A[Начало] --> B[Процесс]
    B --> C{Решение?}
    C -->|Да| D[Действие 1]
    C -->|Нет| E[Действие 2]
    D --> F[Конец]
    E --> F""",
        "Последовательность": """sequenceDiagram
    participant A as Пользователь
    participant B as Агент
    participant C as БД
    
    A->>B: Запрос
    B->>C: Запрос данных
    C-->>B: Данные
    B-->>A: Ответ""",
        "Диаграмма классов": """classDiagram
    class Agent {
        +String name
        +String type
        +execute(task)
        +getStatus()
    }
    
    class Workflow {
        +String name
        +List steps
        +run()
    }
    
    Agent --|> Workflow : uses""",
        "Архитектура системы": """graph TB
    subgraph "Frontend"
        A[Streamlit UI]
        B[Agent API]
    end
    
    subgraph "Backend"
        C[Agent Factory]
        D[Workflow Engine]
        E[DB Plugins]
    end
    
    subgraph "Storage"
        F[SQLite]
        G[ChromaDB]
    end
    
    A --> B
    B --> C
    B --> D
    D --> E
    C --> F
    C --> G"""
    }
    
    col1, col2 = st.columns([1, 2])
    
    with col1:
        st.markdown("**🎨 Шаблоны:**")
        selected_template = st.selectbox("Выберите шаблон", list(templates.keys()))
        
        if st.button("📋 Загрузить шаблон"):
            st.session_state.mermaid_code = templates[selected_template]
            st.rerun()
    
    with col2:
        # Редактор кода
        if "mermaid_code" not in st.session_state:
            st.session_state.mermaid_code = templates["Граф"]
        
        mermaid_code = st.text_area(
            "Код Mermaid диаграммы",
            value=st.session_state.mermaid_code,
            height=300,
            help="Введите код Mermaid диаграммы"
        )
        
        st.session_state.mermaid_code = mermaid_code
        
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("🔍 Предпросмотр диаграммы"):
                if mermaid_code.strip():
                    try:
                        st.markdown("### 📊 Предпросмотр:")
                        st.markdown(f"```mermaid\n{mermaid_code}\n```")
                    except Exception as e:
                        st.error(f"❌ Ошибка рендеринга: {e}")
                else:
                    st.warning("⚠️ Введите код диаграммы")
        
        with col2:
            if st.button("🤖 Генерация через агента"):
                generate_diagram_with_agent(mermaid_code, "mermaid")

def show_plantuml_editor():
    """Редактор PlantUML диаграмм"""
    
    st.markdown("### 🌱 PlantUML диаграммы")
    
    # Шаблоны PlantUML
    plantuml_templates = {
        "Пустая": "@startuml\n\n@enduml",
        "Диаграмма классов": """@startuml
class Agent {
  - name: String
  - type: String
  + execute(task: String): Result
  + getStatus(): Status
}

class Workflow {
  - steps: List<Step>
  + run(): Result
}

Agent --> Workflow
@enduml""",
        "Диаграмма последовательности": """@startuml
actor User
participant Agent
participant Database

User -> Agent: Запрос
Agent -> Database: Запрос данных
Database --> Agent: Данные
Agent --> User: Результат
@enduml""",
        "Диаграмма компонентов": """@startuml
package "MultiAgent System" {
  [Agent Factory]
  [Workflow Engine]
  [DB Plugins]
  [Memory System]
}

[Streamlit UI] --> [Agent Factory]
[Streamlit UI] --> [Workflow Engine]
[Agent Factory] --> [Memory System]
[Workflow Engine] --> [DB Plugins]
@enduml"""
    }
    
    col1, col2 = st.columns([1, 2])
    
    with col1:
        st.markdown("**🎨 Шаблоны PlantUML:**")
        selected_template = st.selectbox("Выберите шаблон", list(plantuml_templates.keys()))
        
        if st.button("📋 Загрузить шаблон PlantUML"):
            st.session_state.plantuml_code = plantuml_templates[selected_template]
            st.rerun()
    
    with col2:
        # Редактор кода PlantUML
        if "plantuml_code" not in st.session_state:
            st.session_state.plantuml_code = plantuml_templates["Диаграмма классов"]
        
        plantuml_code = st.text_area(
            "Код PlantUML диаграммы",
            value=st.session_state.plantuml_code,
            height=300,
            help="Введите код PlantUML диаграммы"
        )
        
        st.session_state.plantuml_code = plantuml_code
        
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("🔍 Сохранить как текст"):
                if plantuml_code.strip():
                    st.download_button(
                        label="💾 Скачать PlantUML",
                        data=plantuml_code,
                        file_name=f"diagram_{datetime.now().strftime('%Y%m%d_%H%M%S')}.puml",
                        mime="text/plain"
                    )
                else:
                    st.warning("⚠️ Введите код диаграммы")
        
        with col2:
            if st.button("🎨 Генерация через PlantUML агента"):
                generate_diagram_with_agent(plantuml_code, "plantuml")

def show_flowchart_creator():
    """Создатель блок-схем"""
    
    st.markdown("### 🔄 Конструктор блок-схем")
    
    st.info("💡 Используйте форму ниже для создания блок-схемы процесса")
    
    # Форма создания блок-схемы
    with st.form("flowchart_form"):
        process_title = st.text_input("📋 Название процесса")
        
        process_description = st.text_area(
            "📝 Описание процесса",
            height=100,
            placeholder="Опишите процесс, для которого нужно создать блок-схему..."
        )
        
        include_decision_points = st.checkbox("❓ Включить точки принятия решений")
        include_parallel_processes = st.checkbox("🔀 Включить параллельные процессы")
        
        flowchart_style = st.selectbox(
            "🎨 Стиль диаграммы",
            ["Простой", "Детальный", "Архитектурный"]
        )
        
        if st.form_submit_button("🎨 Создать блок-схему"):
            if process_title and process_description:
                create_flowchart(
                    process_title, process_description, include_decision_points,
                    include_parallel_processes, flowchart_style
                )
            else:
                st.error("❌ Заполните название и описание процесса")

def create_flowchart(title, description, include_decisions, include_parallel, style):
    """Создание блок-схемы"""
    
    st.markdown(f"### 🔄 Блок-схема: {title}")
    
    # Простой генератор Mermaid блок-схемы
    mermaid_code = f"""graph TD
    A[📍 Начало: {title}] --> B[📋 {description[:30]}...]
    B --> C{{❓ Проверка условий}}
    """
    
    if include_decisions:
        mermaid_code += """
    C -->|✅ Условие выполнено| D[✔️ Основной процесс]
    C -->|❌ Условие не выполнено| E[⚠️ Альтернативный процесс]
    D --> F[📤 Результат]
    E --> F
        """
    else:
        mermaid_code += """
    C --> D[✔️ Основной процесс]
    D --> F[📤 Результат]
        """
    
    if include_parallel:
        mermaid_code += """
    F --> G[🔀 Параллельный процесс 1]
    F --> H[🔀 Параллельный процесс 2]
    G --> I[🏁 Завершение]
    H --> I
        """
    else:
        mermaid_code += """
    F --> I[🏁 Завершение]
        """
    
    # Отображаем сгенерированную диаграмму
    st.markdown("**📊 Сгенерированная блок-схема:**")
    st.markdown(f"```mermaid\n{mermaid_code}\n```")
    
    # Код для копирования
    st.markdown("**📋 Код Mermaid:**")
    st.code(mermaid_code, language="text")

def show_architecture_diagram():
    """Диаграмма архитектуры системы"""
    
    st.markdown("### 🏗️ Диаграмма архитектуры")
    
    # Готовые архитектурные диаграммы
    arch_diagrams = {
        "MultiAgent System": """graph TB
    subgraph "🖥️ Frontend Layer"
        A[Streamlit UI]
        B[Agent Management]
        C[Workflow Control]
    end
    
    subgraph "⚙️ API Layer"
        D[Agent API]
        E[Workflow API]
        F[DB Plugin API]
        G[Memory API]
    end
    
    subgraph "🤖 Core Layer"
        H[Agent Factory]
        I[Workflow Engine]
        J[Text-to-SQL]
    end
    
    subgraph "🔌 Plugin Layer"
        K[PostgreSQL Plugin]
        L[MySQL Plugin]
        M[SQLite Plugin]
    end
    
    subgraph "💾 Storage Layer"
        N[SQLite DB]
        O[ChromaDB]
        P[File Storage]
    end
    
    A --> D
    B --> D
    C --> E
    D --> H
    E --> I
    F --> K
    F --> L
    F --> M
    G --> N
    G --> O
    H --> N
    I --> J
    J --> F""",
        
        "Text-to-SQL Pipeline": """graph LR
    A[📝 Natural Language Query] --> B[🔍 NLU Processing]
    B --> C[🎯 Schema Linking]
    C --> D[🔍 RAG Search]
    D --> E[🤖 SQL Generation]
    E --> F[🔒 Safety Validation]
    F --> G[📊 Query Execution]
    G --> H[📋 Result Formatting]
    
    subgraph "🗄️ Knowledge Base"
        I[Schema Cache]
        J[Query History]
        K[Best Practices]
    end
    
    C --> I
    D --> J
    E --> K""",
        
        "Agent Memory System": """graph TD
    A[🤖 Agent] --> B[💭 Memory Manager]
    B --> C[🎯 Tactical Memory]
    B --> D[🗺️ Strategic Memory]
    
    C --> E[💾 SQLite Storage]
    D --> E
    
    E --> F[🔍 ChromaDB]
    F --> G[📊 Vector Search]
    
    G --> H[🧠 RAG Retrieval]
    H --> A
    
    subgraph "🔧 Processing"
        I[📝 Embedding Model]
        J[🔄 Indexing]
        K[🧹 Cleanup]
    end
    
    F --> I
    I --> J
    J --> K"""
    }
    
    selected_arch = st.selectbox("Выберите архитектурную диаграмму", list(arch_diagrams.keys()))
    
    if selected_arch:
        st.markdown(f"### 🏗️ {selected_arch}")
        st.markdown(f"```mermaid\n{arch_diagrams[selected_arch]}\n```")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.download_button(
                label="💾 Скачать Mermaid код",
                data=arch_diagrams[selected_arch],
                file_name=f"{selected_arch.lower().replace(' ', '_')}_architecture.mmd",
                mime="text/plain"
            )
        
        with col2:
            if st.button("✏️ Редактировать диаграмму"):
                st.session_state.mermaid_code = arch_diagrams[selected_arch]
                st.info("💡 Диаграмма загружена в редактор Mermaid")

def show_er_diagram():
    """ER-диаграмма"""
    
    st.markdown("### 🗄️ ER-диаграмма базы данных")
    
    # Пример ER-диаграммы для системы памяти агентов
    er_diagram = """erDiagram
    AGENTS {
        string agent_id PK
        string name
        string type
        datetime created_at
        json config
    }
    
    SESSIONS {
        string session_id PK
        string agent_id FK
        datetime started_at
        datetime ended_at
        string status
    }
    
    TACTICAL_MEMORIES {
        int id PK
        string session_id FK
        string agent_id FK
        text content
        text context
        datetime created_at
        json metadata
    }
    
    STRATEGIC_MEMORIES {
        int id PK
        string agent_id FK
        text summary
        text insights
        datetime created_at
        datetime updated_at
    }
    
    WORKFLOW_RUNS {
        string run_id PK
        string workflow_name
        json parameters
        datetime started_at
        datetime completed_at
        string status
        json results
    }
    
    AGENTS ||--o{ SESSIONS : has
    SESSIONS ||--o{ TACTICAL_MEMORIES : generates
    AGENTS ||--o{ STRATEGIC_MEMORIES : accumulates
    SESSIONS ||--o{ WORKFLOW_RUNS : executes"""
    
    st.markdown("**🗄️ ER-диаграмма системы памяти агентов:**")
    st.markdown(f"```mermaid\n{er_diagram}\n```")
    
    # Объяснение структуры
    with st.expander("📋 Описание структуры БД"):
        st.markdown("""
        **🏗️ Структура базы данных MultiAgent System:**
        
        - **AGENTS**: Основная информация об агентах
        - **SESSIONS**: Сессии выполнения агентов
        - **TACTICAL_MEMORIES**: Краткосрочная память (конкретные действия)
        - **STRATEGIC_MEMORIES**: Долгосрочная память (обобщения и планы)
        - **WORKFLOW_RUNS**: История выполнения пайплайнов
        
        **🔗 Связи:**
        - Один агент может иметь много сессий
        - Одна сессия генерирует много тактических воспоминаний
        - Один агент накапливает стратегические воспоминания
        - Сессии могут выполнять пайплайны
        """)

def generate_diagram_with_agent(code, diagram_type):
    """Генерация диаграммы через агента"""
    
    st.markdown("### 🤖 Генерация через агента")
    
    try:
        import uuid
        import time
        import os
        import glob
        from agent_streamlit_api import AgentManager
        
        # Генерируем уникальный session_id в формате run-xxxxx
        session_id = f"run-{uuid.uuid4().hex[:16]}"
        
        with st.spinner("🤖 Создание диаграммы через специализированного агента..."):
            # Формируем задачу и выбираем подходящего агента
            if diagram_type == "mermaid":
                agent_profile = "diagram_creator"
                task = (
                    f"Проанализируй и улучши следующую Mermaid диаграмму:\n\n"
                    f"```mermaid\n{code}\n```\n\n"
                    f"Задачи:\n"
                    f"1. Проверь синтаксис диаграммы с помощью validate_mermaid_diagram\n"
                    f"2. Исправь найденные ошибки, если они есть\n"
                    f"3. Добавь стили и цветовое оформление для лучшей читаемости\n"
                    f"4. Сохрани улучшенную диаграмму в файл diagram_{session_id}.mmd\n"
                    f"5. Верни финальный код диаграммы с комментариями о внесенных улучшениях"
                )
            elif diagram_type == "plantuml":
                agent_profile = "plantuml_creator"
                task = (
                    f"Проанализируй и улучши следующую PlantUML диаграмму:\n\n"
                    f"```plantuml\n{code}\n```\n\n"
                    f"Задачи:\n"
                    f"1. Проверь синтаксис диаграммы\n"
                    f"2. Добавь стили и тему для лучшего внешнего вида\n"
                    f"3. Добавь оформление согласно лучшим практикам PlantUML\n"
                    f"4. Сохрани улучшенную диаграмму в файл diagram_{session_id}.puml\n"
                    f"5. Верни финальный код диаграммы с пояснениями"
                )
            else:
                agent_profile = "diagram_creator"  # Fallback для других типов
                task = (
                    f"Создай {diagram_type} диаграмму на основе следующего кода:\n\n"
                    f"{code}\n\n"
                    f"Оптимизируй структуру, добавь стили и сохрани в файл diagram_{session_id}.txt"
                )
            
            # Запускаем подходящего агента
            manager = AgentManager()
            run_id = manager.run_agent(
                agent_id_or_profile=agent_profile,
                task=task,
                session_id=session_id
            )
            
            # Ожидаем завершения
            max_wait_seconds = 120
            start_time = time.time()
            status_placeholder = st.empty()
            progress_placeholder = st.empty()
            
            while time.time() - start_time < max_wait_seconds:
                status = manager.get_agent_status(run_id)
                elapsed = int(time.time() - start_time)
                progress_placeholder.progress(min(elapsed / max_wait_seconds, 0.95))
                
                if status and getattr(status, 'status', '') in ("completed", "failed"):
                    break
                    
                status_placeholder.info(f"⏳ {agent_profile} анализирует и улучшает диаграмму... ({elapsed}с)")
                time.sleep(1)
                
            status_placeholder.empty()
            progress_placeholder.empty()
            
            # Получаем результат
            result_obj = manager.get_agent_result(run_id)
            final_output = getattr(result_obj, 'final_output', None) if result_obj else None
            
            if final_output:
                st.success("✅ Диаграмма успешно обработана агентом!")
                st.markdown("**🤖 Результат работы агента:**")
                st.text(final_output)
                
                # Ищем созданные файлы диаграмм
                diagram_files = []
                for ext in ['*.mmd', '*.puml', '*.txt']:
                    diagram_files.extend(glob.glob(f"diagram_{session_id}{ext}"))
                
                if diagram_files:
                    for diagram_file in diagram_files:
                        st.markdown(f"**📁 Созданный файл: {diagram_file}**")
                        try:
                            with open(diagram_file, 'r', encoding='utf-8') as f:
                                diagram_content = f.read()
                            
                            st.markdown("**🎨 Улучшенная диаграмма:**")
                            if diagram_file.endswith('.mmd'):
                                st.markdown(f"```mermaid\n{diagram_content}\n```")
                            else:
                                st.code(diagram_content, language='text')
                                
                            # Кнопка скачивания
                            st.download_button(
                                label=f"💾 Скачать {os.path.basename(diagram_file)}",
                                data=diagram_content,
                                file_name=os.path.basename(diagram_file),
                                mime="text/plain",
                                key=f"download_diagram_{session_id}"
                            )
                            
                        except Exception as read_error:
                            st.error(f"❌ Ошибка чтения файла {diagram_file}: {read_error}")
                else:
                    st.warning("⚠️ Агент не создал файл диаграммы, но обработка выполнена")
                    
            else:
                st.error("❌ Агент не смог обработать диаграмму")
    
    except Exception as e:
        st.error(f"❌ Ошибка генерации через агента: {e}")
        import traceback
        with st.expander("🔍 Детали ошибки", expanded=False):
            st.code(traceback.format_exc())

def show_image_tools():
    """Инструменты для работы с изображениями"""
    
    st.markdown("## 🎨 Работа с изображениями")
    
    # Вкладки для разных типов работы с изображениями
    img_tab1, img_tab2, img_tab3 = st.tabs(["🎨 Генерация", "✏️ Редактирование", "📊 Анализ"])
    
    with img_tab1:
        show_image_generation()
    
    with img_tab2:
        show_image_editing()
    
    with img_tab3:
        show_image_analysis()

def show_image_generation():
    """Генерация изображений"""
    
    st.markdown("### 🎨 Генерация изображений")
    
    # Форма генерации
    with st.form("image_generation_form"):
        st.markdown("#### 📝 Параметры генерации")
        
        col1, col2 = st.columns(2)
        
        with col1:
            prompt = st.text_area(
                "🎨 Описание изображения",
                height=100,
                placeholder="Опишите что вы хотите увидеть на изображении...",
                help="Детальное описание желаемого изображения"
            )
            
            style = st.selectbox(
                "🎭 Стиль",
                ["Реалистичный", "Художественный", "Мультяшный", "Схематичный", "Фотографический"]
            )
        
        with col2:
            size = st.selectbox(
                "📐 Размер",
                ["512x512", "1024x1024", "1024x768", "768x1024"]
            )
            
            quality = st.selectbox(
                "⭐ Качество",
                ["standard", "hd"]
            )
            
            n_images = st.number_input(
                "📊 Количество изображений",
                min_value=1,
                max_value=4,
                value=1
            )
        
        # Дополнительные параметры
        with st.expander("⚙️ Дополнительные параметры"):
            negative_prompt = st.text_input(
                "🚫 Негативный промпт",
                placeholder="Что НЕ должно быть на изображении...",
                help="Описание того, что нужно исключить из изображения"
            )
            
            seed = st.number_input(
                "🌱 Seed (для воспроизводимости)",
                min_value=0,
                max_value=999999,
                value=0,
                help="0 = случайный seed"
            )
        
        # Кнопка генерации
        generate_clicked = st.form_submit_button("🎨 Генерировать изображение", type="primary")
        
        if generate_clicked and prompt:
            # Сохраняем параметры для генерации вне формы
            st.session_state.generate_params = {
                'prompt': prompt,
                'style': style, 
                'size': size,
                'quality': quality,
                'n_images': n_images,
                'negative_prompt': negative_prompt,
                'seed': seed
            }
            st.session_state.should_generate = True
        elif generate_clicked and not prompt:
            st.error("❌ Введите описание изображения")
    
    # Генерация ВНЕ формы для возможности использования download кнопок
    if st.session_state.get('should_generate', False):
        params = st.session_state.get('generate_params', {})
        if params:
            generate_image_with_agent(
                params['prompt'], params['style'], params['size'], 
                params['quality'], params['n_images'], 
                params['negative_prompt'], params['seed']
            )
        st.session_state.should_generate = False

def generate_image_with_agent(prompt, style, size, quality, n_images, negative_prompt, seed):
    """Генерация изображения через агента"""
    
    st.markdown("### 🎨 Результат генерации")
    
    try:
        import uuid
        import time
        import os
        from datetime import datetime
        from agent_streamlit_api import AgentManager
        
        # Генерируем уникальный session_id в формате run-xxxxx
        session_id = f"run-{uuid.uuid4().hex[:16]}"
        
        # Создаем промпт с учетом стиля и других параметров
        enhanced_prompt = prompt
        if style != "Реалистичный":
            enhanced_prompt = f"{prompt}, {style.lower()} style"
        if negative_prompt:
            enhanced_prompt = f"{enhanced_prompt}, avoid: {negative_prompt}"
            
        with st.spinner("🎨 Генерация изображения через агента..."):
            # Формируем задачу для artist_agent
            task = (
                f"Сгенерируй {n_images} изображение(изображений) по описанию:\n"
                f"Промпт: {enhanced_prompt}\n"
                f"Размер: {size}\n"
                f"Качество: {quality}\n"
                f"Стиль: {style}\n"
            )
            if negative_prompt:
                task += f"Негативный промпт (чего избегать): {negative_prompt}\n"
            if seed:
                task += f"Seed для воспроизводимости: {seed}\n"
                
            task += (
                f"\nСохрани каждое изображение в файл с именем generated_image_{session_id}_{{номер}}.png\n"
                f"Верни список путей к созданным файлам."
            )
            
            # Запускаем агента
            manager = AgentManager()
            run_id = manager.run_agent(
                agent_id_or_profile="artist_agent",
                task=task,
                session_id=session_id
            )
            
            # Ожидаем завершения
            max_wait_seconds = 300  # Больше времени для генерации нескольких изображений
            start_time = time.time()
            status_placeholder = st.empty()
            progress_placeholder = st.empty()
            
            while time.time() - start_time < max_wait_seconds:
                status = manager.get_agent_status(run_id)
                elapsed = int(time.time() - start_time)
                progress_placeholder.progress(min(elapsed / max_wait_seconds, 0.95))
                
                if status and getattr(status, 'status', '') in ("completed", "failed"):
                    break
                    
                status_placeholder.info(f"⏳ Агент генерирует изображение... ({elapsed}с)")
                time.sleep(1)
                
            status_placeholder.empty()
            progress_placeholder.empty()
            
            # Получаем результат
            result_obj = manager.get_agent_result(run_id)
            final_output = getattr(result_obj, 'final_output', None) if result_obj else None
            
            if final_output:
                st.success("✅ Изображения успешно сгенерированы!")
                st.text(f"Результат: {final_output}")
                
                # Ищем сгенерированные файлы
                import glob
                generated_files = glob.glob(f"generated_image_{session_id}_*.png")
                
                if not generated_files:
                    # Возможно агент сохранил файлы с другим именем
                    generated_files = glob.glob(f"*{session_id}*.png")
                    
            else:
                st.error("❌ Агент не смог сгенерировать изображения")
                generated_files = []
        
        # Показываем параметры генерации
        st.markdown("**📋 Параметры генерации:**")
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.info(f"**Стиль:** {style}")
            st.info(f"**Размер:** {size}")
        
        with col2:
            st.info(f"**Качество:** {quality}")
            st.info(f"**Количество:** {n_images}")
        
        with col3:
            if seed > 0:
                st.info(f"**Seed:** {seed}")
            else:
                st.info("**Seed:** Случайный")
        
        # Отображаем созданные изображения
        if generated_files:
            st.markdown("**🖼️ Сгенерированные изображения:**")
            
            # Отображаем изображения в колонках
            if len(generated_files) == 1:
                cols = [st.container()]
            elif len(generated_files) == 2:
                cols = st.columns(2)
            else:
                cols = st.columns(min(3, len(generated_files)))
            
            for idx, filename in enumerate(generated_files):
                with cols[idx % len(cols)]:
                    try:
                        st.image(filename, caption=f"Изображение {idx+1}")
                        
                        # Показываем информацию о файле
                        file_size = os.path.getsize(filename) / 1024  # KB
                        st.caption(f"📁 {os.path.basename(filename)} ({file_size:.1f} KB)")
                        
                        # Кнопка скачивания (теперь работает, так как функция вызывается вне формы)
                        with open(filename, "rb") as file:
                            st.download_button(
                                label=f"💾 Скачать {idx+1}",
                                data=file.read(),
                                file_name=os.path.basename(filename),
                                mime="image/png",
                                key=f"download_{idx}_{session_id}"
                            )
                    except Exception as display_error:
                        st.error(f"❌ Ошибка отображения {filename}: {display_error}")
            
            # Информация о параметрах генерации
            with st.expander("ℹ️ Детали генерации", expanded=False):
                st.markdown(f"**📝 Оригинальный промпт:** {prompt}")
                st.markdown(f"**🎨 Улучшенный промпт:** {enhanced_prompt}")
                if negative_prompt:
                    st.markdown(f"**🚫 Исключения:** {negative_prompt}")
                st.markdown(f"**⏰ Время:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                st.markdown(f"**🆔 Session ID:** {session_id}")
                st.markdown(f"**📂 Файлы:** {', '.join([os.path.basename(f) for f in generated_files])}")
        else:
            st.error("❌ Не удалось создать ни одного изображения")
            
        # Информация о действиях (без кнопок, так как функция вызывается из формы)
        st.markdown("**💡 Доступные действия:**")
        st.info("💾 Скачать изображения (кнопки выше) • 🔄 Изменить параметры в форме • ✏️ Использовать редактор изображений")
        st.caption("Используйте форму выше для настройки и генерации новых изображений")
    
    except Exception as e:
        st.error(f"❌ Ошибка генерации изображения: {e}")
        # Показываем детали ошибки для отладки
        import traceback
        with st.expander("🔍 Детали ошибки", expanded=False):
            st.code(traceback.format_exc())

def show_image_editing():
    """Редактирование изображений (через агента и свободный промпт)"""
    
    st.markdown("### ✏️ Редактирование изображений")
    
    with st.form("image_editing_form"):
        prompt = st.text_area(
            "📝 Промпт редактирования",
            height=120,
            placeholder="Опишите на английском, какие изменения нужно внести (e.g., remove background, enhance sharpness, adjust brightness).",
            help="Лучшее качество достигается, если промпт на английском"
        )
        
        input_method = st.radio(
            "Источник изображений",
            ["📁 Загрузить файлы", "🌐 URL изображений"],
            key="edit_image_input_method"
        )
        
        image_files = []
        image_urls = []
        
        if input_method == "📁 Загрузить файлы":
            uploaded_files = st.file_uploader(
                "📁 Загрузить изображения (до 4 файлов)",
                type=['png', 'jpg', 'jpeg', 'gif', 'bmp'],
                accept_multiple_files=True,
                help="Поддерживаемые форматы: PNG, JPG, JPEG, GIF, BMP. Максимум 4 изображения"
            )
            image_files = uploaded_files if uploaded_files else []
            
            # Показываем превью загруженных изображений
            if image_files:
                if len(image_files) > 4:
                    st.warning("⚠️ Выбрано больше 4 изображений. Будут использованы только первые 4.")
                    image_files = image_files[:4]
                
                st.markdown(f"**📸 Загружено изображений: {len(image_files)}**")
                cols = st.columns(min(len(image_files), 4))
                for idx, img_file in enumerate(image_files):
                    with cols[idx]:
                        st.image(img_file, caption=f"{idx+1}. {img_file.name}", use_container_width=True)
        else:
            st.markdown("**🌐 URL изображений (по одному в строке, максимум 4):**")
            url_text = st.text_area(
                "URLs изображений",
                placeholder="https://example.com/image1.jpg\nhttps://example.com/image2.jpg",
                help="Введите URL изображений, каждый с новой строки. Максимум 4 URL.",
                height=100
            )
            
            if url_text.strip():
                urls = [url.strip() for url in url_text.strip().split('\n') if url.strip()]
                if len(urls) > 4:
                    st.warning("⚠️ Указано больше 4 URL. Будут использованы только первые 4.")
                    urls = urls[:4]
                
                image_urls = urls
                
                # Показываем превью URL изображений
                if image_urls:
                    st.markdown(f"**🌐 Найдено URL: {len(image_urls)}**")
                    cols = st.columns(min(len(image_urls), 4))
                    for idx, img_url in enumerate(image_urls):
                        with cols[idx]:
                            try:
                                st.image(img_url, caption=f"{idx+1}. {img_url[-30:]}", use_container_width=True)
                            except Exception as e:
                                st.error(f"❌ Ошибка загрузки {img_url[:50]}...")
        
        with st.expander("⚙️ Дополнительные параметры"):
            width = st.number_input("Ширина результата", min_value=512, max_value=2048, value=1024, step=64)
            height = st.number_input("Высота результата", min_value=512, max_value=2048, value=1024, step=64)
        
        do_edit = st.form_submit_button("✏️ Применить редактирование", type="primary")
    
    if do_edit:
        if not prompt:
            st.error("❌ Введите промпт редактирования")
            return
        if input_method == "📁 Загрузить файлы" and not image_files:
            st.error("❌ Загрузите файлы изображений")
            return
        if input_method == "🌐 URL изображений" and not image_urls:
            st.error("❌ Укажите URL изображений")
            return
        
        # Редактирование изображений использует AgentManager внутри,
        # который уже создает корректную телеметрию с agent_run_artist_agent
        edit_images_with_agent(
            image_inputs=image_files if image_files else image_urls,
            prompt=prompt,
            input_type="files" if image_files else "urls",
            width=width,
            height=height
        )

def edit_images_with_agent(image_inputs, prompt, input_type, width=1024, height=1024):
    """Редактирование множественных изображений через агента по свободному промпту"""
    
    st.markdown("### ✏️ Результат редактирования")
    
    try:
        import uuid
        import os
        import tempfile
        from datetime import datetime
        from agent_streamlit_api import AgentManager
        import time
        
        session_id = f"run-{uuid.uuid4().hex[:16]}"
        temp_image_paths = []
        original_captions = []
        
        images_count = len(image_inputs)
        st.info(f"🎨 Обрабатывается {images_count} изображение(й)")
        
        # Готовим локальные файлы для агента
        if input_type == "files":
            for idx, image_input in enumerate(image_inputs):
                with tempfile.NamedTemporaryFile(delete=False, suffix=f"_input_{idx+1}.png") as tmp_file:
                    tmp_file.write(image_input.read())
                    temp_image_paths.append(tmp_file.name)
                    original_captions.append(getattr(image_input, 'name', f'uploaded_image_{idx+1}'))
                    # Сбрасываем позицию файла для повторного чтения если нужно
                    image_input.seek(0)
        else:  # URLs
            import requests
            for idx, image_url in enumerate(image_inputs):
                try:
                    resp = requests.get(image_url, timeout=30)
                    resp.raise_for_status()
                    with tempfile.NamedTemporaryFile(delete=False, suffix=f"_input_{idx+1}.png") as tmp_file:
                        tmp_file.write(resp.content)
                        temp_image_paths.append(tmp_file.name)
                        original_captions.append(f"URL_{idx+1}: {image_url[-30:]}")
                except Exception as dl_err:
                    st.error(f"❌ Не удалось скачать изображение {idx+1} по URL: {dl_err}")
                    return

        # Создаем временное ожидаемое имя результата
        edited_filename = f"./edited_image_{images_count}imgs_{session_id}.png"
        
        with st.spinner(f"🎨 Применение редактирования к {images_count} изображению(ям) через агента..."):
            # Формируем задачу для artist_agent с указанием количества изображений
            paths_list = "\n".join([f"{idx+1}. {path}" for idx, path in enumerate(temp_image_paths)])
            
            task = (
                f"Отредактируй с помощью edit_image_vse_tool {images_count} изображение(й) по следующим путям (переданы как список):\n"
                f"{paths_list}\n\n"
                f"Применить изменения по пользовательскому промпту (переведи на английский): {prompt}\n\n"
                f"ВАЖНО: Работай с {images_count} изображениями одновременно. "
                f"Если передано несколько изображений - передавай их как список в параметр image_paths.\n"
                f"Сохранить результат в ./plots/edited_image_{images_count}imgs_{session_id}.png\n"
                f"Верни ТОЛЬКО путь к созданному файлу и краткое описание выполненных изменений."
            )
            
            # Запускаем агента с детальной диагностикой ошибок
            try:
                st.info("🔧 Создание AgentManager...")
                
                # Альтернативный подход: используем кэшированный singleton AgentManager
                @st.cache_resource
                def get_agent_manager():
                    try:
                        from agent_streamlit_api import AgentManager
                        return AgentManager()
                    except Exception as e:
                        st.error(f"Ошибка при создании кэшированного AgentManager: {e}")
                        raise
                
                try:
                    manager = get_agent_manager()
                    st.success("✅ AgentManager создан успешно (кэшированный)")
                except Exception as cache_error:
                    st.warning(f"⚠️ Кэшированный AgentManager не работает: {cache_error}")
                    st.info("🔄 Пробуем создать AgentManager напрямую...")
                    
                    # Fallback: создаём напрямую без кэша
                    from agent_streamlit_api import AgentManager
                    manager = AgentManager()
                    st.success("✅ AgentManager создан успешно (прямое создание)")
                
                st.info("🚀 Запуск агента artist_agent...")
                run_id = manager.run_agent(
                    agent_id_or_profile="artist_agent",
                    task=task,
                    session_id=session_id
                )
                st.success(f"✅ Агент запущен успешно для обработки {images_count} изображения(й)")
            except Exception as agent_error:
                st.error(f"❌ Ошибка при работе с агентом: {agent_error}")
                import traceback
                st.code(traceback.format_exc())
                return
            
            # Ожидаем завершения (больше времени для множественных изображений)
            max_wait_seconds = 240  # 4 минуты для множественных изображений
            start_time = time.time()
            status_placeholder = st.empty()
            progress_placeholder = st.empty()
            
            while time.time() - start_time < max_wait_seconds:
                status = manager.get_agent_status(run_id)
                elapsed = int(time.time() - start_time)
                progress_placeholder.progress(min(elapsed / max_wait_seconds, 0.95))
                
                if status and getattr(status, 'status', '') in ("completed", "failed"):
                    break
                status_placeholder.info(f"⏳ Агент редактирует {images_count} изображение(й)... ({elapsed}с)")
                time.sleep(2)
                
            status_placeholder.empty()
            progress_placeholder.empty()
            
            # Получаем результат
            result_obj = manager.get_agent_result(run_id)
            final_output = getattr(result_obj, 'final_output', None) if result_obj else None
            
            # Если final_output пустой, пытаемся извлечь ответ из трассы
            if not final_output:
                try:
                    # Используем обновленную функцию из страницы логов
                    import sys
                    import os
                    sys.path.append(os.path.dirname(__file__))
                    
                    import importlib.util
                    spec = importlib.util.spec_from_file_location("logs_traces", os.path.join(os.path.dirname(__file__), "08_Logs_Traces.py"))
                    logs_module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(logs_module)
                    
                    _get_final_answer_for_run = logs_module._get_final_answer_for_run
                    
                    from telemetry import get_telemetry_manager
                    telemetry_manager = get_telemetry_manager()
                    
                    trace_answer = _get_final_answer_for_run(telemetry_manager, run_id)
                    if trace_answer:
                        final_output = trace_answer
                
                except Exception as e:
                    st.error(f"❌ Ошибка при получении результата: {e}")
            
            # Извлекаем путь к файлу из ответа агента
            edited_filename = None
            if isinstance(final_output, str):
                text = final_output.strip()
                
                # Убираем префикс "Final answer:" если есть
                if text.lower().startswith("final answer:"):
                    text = text.split(":", 1)[1].strip()
                
                # Очищаем от кавычек и пробелов
                text = text.strip().strip('"').strip("'")
                
                # Ищем пути к файлам в тексте ответа с помощью регексов
                import re
                # Паттерны для поиска путей к PNG файлам
                path_patterns = [
                    r'(/[^\s<>"\']+\.png)',  # абсолютный путь
                    r'(\./[^\s<>"\']+\.png)',  # относительный путь ./
                    r'([^\s<>"\']*edited_image[^\s<>"\']*\.png)',  # файлы содержащие edited_image
                ]
                
                found_paths = []
                for pattern in path_patterns:
                    matches = re.findall(pattern, text, re.IGNORECASE)
                    found_paths.extend(matches)
                
                # Проверяем каждый найденный путь
                for potential_path in found_paths:
                    if os.path.exists(potential_path):
                        edited_filename = potential_path
                        break
                
                # Если ничего не найдено регексами, проверяем исходный текст как есть
                if not edited_filename and text:
                    if os.path.exists(text):
                        edited_filename = text
                    else:
                        # Попробуем найти файл по частичному пути или имени
                        import glob
                        filename_only = os.path.basename(text)
                        possible_paths = glob.glob(f"**/{filename_only}", recursive=True)
                        
                        if possible_paths:
                            edited_filename = possible_paths[0]
                        else:
                            # Финальная попытка - ищем любые файлы edited_image в plots с нужным session_id
                            plots_dir = "./plots"
                            if os.path.exists(plots_dir):
                                edited_files = glob.glob(f"{plots_dir}/edited_image*{session_id}*.png")
                                if not edited_files:
                                    # Поиск по количеству изображений
                                    edited_files = glob.glob(f"{plots_dir}/vse_edited_{images_count}imgs*.png")
                                if edited_files:
                                    # Берем самый новый файл
                                    edited_filename = max(edited_files, key=os.path.getmtime)
            
            # Если не нашли файл через парсинг, попробуем найти новый отредактированный файл
            if not edited_filename:
                import glob
                import time
                plots_dir = "./plots"
                if os.path.exists(plots_dir):
                    # Находим все файлы vse_edited с нужным количеством изображений
                    edited_files = glob.glob(f"{plots_dir}/vse_edited_{images_count}imgs*.png")
                    if not edited_files:
                        # Fallback: любые edited_image файлы
                        edited_files = glob.glob(f"{plots_dir}/edited_image*.png")
                    
                    if edited_files:
                        # Берем самый новый файл (созданный последним)
                        newest_file = max(edited_files, key=os.path.getmtime)
                        file_age = time.time() - os.path.getmtime(newest_file)
                        
                        # Если файл создан недавно (в течение последних 10 минут)
                        if file_age < 600:  # 10 минут
                            edited_filename = newest_file
                    
            if isinstance(edited_filename, str) and os.path.exists(edited_filename):
                st.success(f"✅ {images_count} изображение(й) успешно отредактировано")
                
                # Показываем оригиналы и результат
                st.markdown("#### 📷 Исходные изображения")
                orig_cols = st.columns(min(images_count, 4))
                for idx, (temp_path, caption) in enumerate(zip(temp_image_paths, original_captions)):
                    with orig_cols[idx % 4]:
                        st.image(temp_path, caption=f"{idx+1}. {caption}", use_container_width=True)
                        try:
                            orig_size = os.path.getsize(temp_path) / 1024
                            st.caption(f"📁 ({orig_size:.1f} KB)")
                        except Exception:
                            pass
                
                st.markdown("#### 🎨 Результат редактирования")
                st.image(edited_filename, caption=f"Результат обработки {images_count} изображения(й)", use_container_width=True)
                try:
                    edited_size = os.path.getsize(edited_filename) / 1024
                    st.caption(f"📁 {os.path.basename(edited_filename)} ({edited_size:.1f} KB)")
                except Exception:
                    pass
                
                with st.expander("ℹ️ Детали редактирования", expanded=False):
                    st.markdown(f"**🎯 Промпт:** {prompt}")
                    st.markdown(f"**📊 Обработано изображений:** {images_count}")
                    st.markdown(f"**⏰ Время:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    st.markdown(f"**🆔 Session ID:** {session_id}")
                    st.markdown(f"**📂 Результат:** {os.path.basename(edited_filename)}")
                    if final_output:
                        st.markdown("**🤖 Ответ агента:**")
                        st.text(final_output)
                
                col1, col2 = st.columns(2)
                with col1:
                    with open(edited_filename, "rb") as file:
                        st.download_button(
                            label="💾 Скачать результат",
                            data=file.read(),
                            file_name=os.path.basename(edited_filename),
                            mime="image/png",
                            key=f"download_edited_{session_id}"
                        )
                with col2:
                    st.info("🔁 Для нового редактирования измените промпт и повторите")
            else:
                st.error("❌ Агент не вернул путь к отредактированному файлу")
                if isinstance(final_output, str):
                    with st.expander("Ответ агента", expanded=False):
                        st.code(final_output)
        
        # Удаляем временные файлы
        try:
            for temp_path in temp_image_paths:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
        except Exception:
            pass
    
    except Exception as e:
        st.error(f"❌ Общая ошибка редактирования множественных изображений: {e}")
        import traceback
        with st.expander("🔍 Детали ошибки", expanded=False):
            st.code(traceback.format_exc())

def edit_image_with_agent(image_input, prompt, input_type, width=1024, height=1024):
    """Редактирование изображения через агента по свободному промпту"""
    
    st.markdown("### ✏️ Результат редактирования")
    
    try:
        import uuid
        import os
        import tempfile
        from datetime import datetime
        from agent_streamlit_api import AgentManager
        import time
        
        session_id = f"run-{uuid.uuid4().hex[:16]}"
        temp_image_path = None
        original_caption = ""
        
        # Готовим локальный файл для агента
        if input_type == "file":
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp_file:
                tmp_file.write(image_input.read())
                temp_image_path = tmp_file.name
            original_caption = getattr(image_input, 'name', 'uploaded_image')
        else:
            import requests
            try:
                resp = requests.get(image_input, timeout=30)
                resp.raise_for_status()
            except Exception as dl_err:
                st.error(f"❌ Не удалось скачать изображение по URL: {dl_err}")
                return
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp_file:
                tmp_file.write(resp.content)
                temp_image_path = tmp_file.name
            original_caption = image_input

        # Создаем временное ожидаемое имя результата (агент может вернуть относительный путь)
        edited_filename = f"./edited_image_{session_id}.png"
        
        with st.spinner("🎨 Применение редактирования через агента..."):
            # Формируем задачу для artist_agent
            task = (
                f"Отредактируй изображение по локальному пути '{temp_image_path}'.\n"
                f"Применить изменения по пользовательскому промпту, предварительно переведя его на английский: {prompt}.\n\n"
                f"Если необходимо, добавь негативный промпт, чтобы избежать ошибок в генерации.\n"
                f"Сохранить отредактированное изображение в ./edited_image_{session_id}.png\n"
                f"Верни ТОЛЬКО путь к созданному файлу."
            )
            
            # Запускаем агента с детальной диагностикой ошибок
            try:
                st.info("🔧 Создание AgentManager...")
                
                # Альтернативный подход: используем кэшированный singleton AgentManager
                @st.cache_resource
                def get_agent_manager():
                    try:
                        from agent_streamlit_api import AgentManager
                        return AgentManager()
                    except Exception as e:
                        st.error(f"Ошибка при создании кэшированного AgentManager: {e}")
                        raise
                
                try:
                    manager = get_agent_manager()
                    st.success("✅ AgentManager создан успешно (кэшированный)")
                except Exception as cache_error:
                    st.warning(f"⚠️ Кэшированный AgentManager не работает: {cache_error}")
                    st.info("🔄 Пробуем создать AgentManager напрямую...")
                    
                    # Fallback: создаём напрямую без кэша
                    from agent_streamlit_api import AgentManager
                    manager = AgentManager()
                    st.success("✅ AgentManager создан успешно (прямое создание)")
                
                st.info("🚀 Запуск агента artist_agent...")
                run_id = manager.run_agent(
                    agent_id_or_profile="artist_agent",
                    task=task,
                    session_id=session_id
                )
                st.success("✅ Агент запущен успешно")
            except Exception as agent_error:
                st.error(f"❌ Ошибка при работе с агентом: {agent_error}")
                import traceback
                st.code(traceback.format_exc())
                return
            
            # Ожидаем завершения
            max_wait_seconds = 180
            start_time = time.time()
            #edited_filename = None
            status_placeholder = st.empty()
            while time.time() - start_time < max_wait_seconds:
                status = manager.get_agent_status(run_id)
                if status and getattr(status, 'status', '') in ("completed", "failed"):
                    break
                status_placeholder.info("⏳ Агент редактирует изображение...")
                time.sleep(1)
            status_placeholder.empty()
            
            # Получаем результат
            result_obj = manager.get_agent_result(run_id)
            final_output = getattr(result_obj, 'final_output', None) if result_obj else None
            
            # Если final_output пустой, пытаемся извлечь ответ из трассы
            if not final_output:
                try:
                    # Используем обновленную функцию из страницы логов
                    import sys
                    import os
                    sys.path.append(os.path.dirname(__file__))
                    
                    import importlib.util
                    spec = importlib.util.spec_from_file_location("logs_traces", os.path.join(os.path.dirname(__file__), "08_Logs_Traces.py"))
                    logs_module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(logs_module)
                    
                    _get_final_answer_for_run = logs_module._get_final_answer_for_run
                    
                    from telemetry import get_telemetry_manager
                    telemetry_manager = get_telemetry_manager()
                    
                    trace_answer = _get_final_answer_for_run(telemetry_manager, run_id)
                    if trace_answer:
                        final_output = trace_answer
                
                except Exception as e:
                    st.error(f"❌ Ошибка при получении результата: {e}")
            
            # Извлекаем путь к файлу из ответа агента
            edited_filename = None
            if isinstance(final_output, str):
                text = final_output.strip()
                
                # Убираем префикс "Final answer:" если есть
                if text.lower().startswith("final answer:"):
                    text = text.split(":", 1)[1].strip()
                
                # Очищаем от кавычек и пробелов
                text = text.strip().strip('"').strip("'")
                
                # Ищем пути к файлам в тексте ответа с помощью регексов
                import re
                # Паттерны для поиска путей к PNG файлам
                path_patterns = [
                    r'(/[^\s<>"\']+\.png)',  # абсолютный путь
                    r'(\./[^\s<>"\']+\.png)',  # относительный путь ./
                    r'([^\s<>"\']*edited_image[^\s<>"\']*\.png)',  # файлы содержащие edited_image
                ]
                
                found_paths = []
                for pattern in path_patterns:
                    matches = re.findall(pattern, text, re.IGNORECASE)
                    found_paths.extend(matches)
                
                # Проверяем каждый найденный путь
                for potential_path in found_paths:
                    if os.path.exists(potential_path):
                        edited_filename = potential_path
                        break
                
                # Если ничего не найдено регексами, проверяем исходный текст как есть
                if not edited_filename and text:
                    if os.path.exists(text):
                        edited_filename = text
                    else:
                        # Попробуем найти файл по частичному пути или имени
                        import glob
                        filename_only = os.path.basename(text)
                        possible_paths = glob.glob(f"**/{filename_only}", recursive=True)
                        
                        if possible_paths:
                            edited_filename = possible_paths[0]
                        else:
                            # Финальная попытка - ищем любые файлы edited_image в plots
                            plots_dir = "./plots"
                            if os.path.exists(plots_dir):
                                edited_files = glob.glob(f"{plots_dir}/edited_image*.png")
                                if edited_files:
                                    # Берем самый новый файл
                                    edited_filename = max(edited_files, key=os.path.getmtime)
            
            # Если не нашли файл через парсинг, попробуем найти новый отредактированный файл
            if not edited_filename:
                import glob
                import time
                plots_dir = "./plots"
                if os.path.exists(plots_dir):
                    # Находим все файлы edited_image
                    edited_files = glob.glob(f"{plots_dir}/edited_image*.png")
                    if edited_files:
                        # Берем самый новый файл (созданный последним)
                        newest_file = max(edited_files, key=os.path.getmtime)
                        file_age = time.time() - os.path.getmtime(newest_file)
                        
                        # Если файл создан недавно (в течение последних 5 минут)
                        if file_age < 300:  # 5 минут
                            edited_filename = newest_file
                    
            if isinstance(edited_filename, str) and os.path.exists(edited_filename):
                st.success("✅ Изображение успешно отредактировано")
                
                col1, col2 = st.columns(2)
                
                with col1:
                    st.markdown("#### 📷 Оригинал")
                    if input_type == "url":
                        st.image(temp_image_path, caption=original_caption)
                    else:
                        st.image(temp_image_path, caption=original_caption)
                    try:
                        orig_size = os.path.getsize(temp_image_path) / 1024
                        st.caption(f"📁 {os.path.basename(temp_image_path)} ({orig_size:.1f} KB)")
                    except Exception:
                        pass
                
                with col2:
                    st.markdown("#### 🎨 Результат")
                    st.image(edited_filename, caption="Отредактированное изображение")
                    try:
                        edited_size = os.path.getsize(edited_filename) / 1024
                        st.caption(f"📁 {os.path.basename(edited_filename)} ({edited_size:.1f} KB)")
                    except Exception:
                        pass
                
                with st.expander("ℹ️ Детали редактирования", expanded=False):
                    st.markdown(f"**🎯 Промпт:** {prompt}")
                    st.markdown(f"**⏰ Время:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    st.markdown(f"**🆔 Session ID:** {session_id}")
                    st.markdown(f"**📂 Результат:** {os.path.basename(edited_filename)}")
                
                col1, col2 = st.columns(2)
                with col1:
                    with open(edited_filename, "rb") as file:
                        st.download_button(
                            label="💾 Скачать результат",
                            data=file.read(),
                            file_name=os.path.basename(edited_filename),
                            mime="image/png",
                            key=f"download_edited_{session_id}"
                        )
                with col2:
                    st.info("🔁 Для нового редактирования измените промпт и повторите")
            else:
                st.error("❌ Агент не вернул путь к отредактированному файлу")
                if isinstance(final_output, str):
                    with st.expander("Ответ агента", expanded=False):
                        st.code(final_output)
        
        # Удаляем временный файл
        try:
            if temp_image_path and os.path.exists(temp_image_path):
                os.unlink(temp_image_path)
        except Exception:
            pass
    
    except Exception as e:
        st.error(f"❌ Общая ошибка редактирования: {e}")
        import traceback
        with st.expander("🔍 Детали ошибки", expanded=False):
            st.code(traceback.format_exc())

def show_image_analysis():
    """Анализ изображений"""
    
    st.markdown("### 📊 Анализ изображений")
    
    # Выбор способа загрузки изображения
    input_method = st.radio(
        "Выберите способ загрузки изображения:",
        ["📁 Загрузить файл", "🌐 URL изображения"],
        key="image_input_method"
    )
    
    image_file = None
    image_url = None
    
    if input_method == "📁 Загрузить файл":
        # Загрузка изображения для анализа
        uploaded_file = st.file_uploader(
            "📁 Загрузить изображение для анализа",
            type=['png', 'jpg', 'jpeg'],
            help="Загрузите изображение для автоматического анализа",
            key="analysis_upload"
        )
        image_file = uploaded_file
        
    else:  # URL изображения
        image_url = st.text_input(
            "🌐 URL изображения",
            placeholder="https://example.com/image.jpg",
            help="Введите URL изображения для анализа",
            key="analysis_url"
        )
        
        if image_url:
            try:
                # Показываем превью изображения по URL
                st.image(image_url, caption="Изображение для анализа", use_container_width=True)
            except Exception as e:
                st.error(f"Не удалось загрузить изображение по URL: {e}")
                image_url = None
    
    if uploaded_file is not None or image_url:
        # Отображаем изображение (только для загруженного файла, для URL уже показали выше)
        if uploaded_file is not None:
            st.image(uploaded_file, caption="Изображение для анализа", use_container_width=True)
        
        # Типы анализа
        col1, col2 = st.columns(2)
        
        with col1:
            # Импортируем конфиг и получаем доступные типы
            from custom_tools.image_tools import get_available_image_analysis_types, ANALYSIS_TYPES_CONFIG
            
            available_types = get_available_image_analysis_types()
            
            analysis_types = st.multiselect(
                "🔍 Типы анализа",
                available_types,
                default=["Распознавание объектов", "Анализ композиции"],
                format_func=lambda x: f"{ANALYSIS_TYPES_CONFIG.get(x, {}).get('icon', '📋')} {x}"
            )
        
        with col2:
            confidence_threshold = st.slider(
                "🎯 Порог уверенности",
                min_value=0.1,
                max_value=1.0,
                value=0.5,
                step=0.1,
                help="Минимальная уверенность для отображения результатов"
            )
            
            detailed_analysis = st.checkbox(
                "📋 Детальный анализ",
                value=True,
                help="Включить подробную информацию в результаты"
            )
        
        # Кнопка анализа
        if st.button("🔍 Анализировать изображение", type="primary"):
            if uploaded_file is not None:
                import os, uuid
                session_id = f"run-{uuid.uuid4().hex[:16]}"
                os.environ["RUN_ID"] = session_id  # Используем session_id как run_id
                try:
                    from unified_logging import get_run_logger
                    _rlog = get_run_logger(session_id, __name__)
                    _rlog.info("Старт анализа изображения")
                except Exception:
                    pass
                # Используем централизованный ToolManager для телеметрии
                from tool_manager import get_tool_manager
                tool_manager = get_tool_manager()
                
                with tool_manager.tool_context(
                    tool_name="image_analysis",
                    task_description=f"Analyze image with {len(analysis_types)} analysis types",
                    session_id=session_id,  # Передаем session_id
                    analysis_types=", ".join(analysis_types),
                    input_type="file",
                    detailed=detailed_analysis
                ) as ctx:
                    analyze_image_with_agent(uploaded_file, analysis_types, confidence_threshold, detailed_analysis, input_type="file")
                    ctx.add_metadata("analysis_types_count", len(analysis_types))
                    
            elif image_url:
                # Используем централизованный ToolManager для телеметрии
                from tool_manager import get_tool_manager
                tool_manager = get_tool_manager()
                
                with tool_manager.tool_context(
                    tool_name="image_analysis",
                    task_description=f"Analyze image with {len(analysis_types)} analysis types",
                    analysis_types=", ".join(analysis_types),
                    input_type="url",
                    detailed=detailed_analysis
                ) as ctx:
                    analyze_image_with_agent(image_url, analysis_types, confidence_threshold, detailed_analysis, input_type="url")
                    ctx.add_metadata("analysis_types_count", len(analysis_types))
    
    else:
        st.info("📁 Загрузите изображение или введите URL для анализа")

def analyze_image_with_agent(image_input, analysis_types, confidence_threshold, detailed, input_type="file"):
    """Анализ изображения через реальную vision модель"""
    
    st.markdown("### 📊 Результаты анализа")
    
    try:
        with st.spinner("Анализ изображения через ИИ..."):
            # Импортируем наш инструмент анализа изображений
            from custom_tools.image_tools import analyze_image_tool
            import base64
            import json
            
            # Подготавливаем данные в зависимости от типа входных данных
            if input_type == "file":
                # Читаем изображение и преобразуем в base64
                image_bytes = image_input.getvalue()
                image_base64 = base64.b64encode(image_bytes).decode('utf-8')
                
                # Выполняем анализ с base64
                analysis_result = analyze_image_tool(
                    image_input=image_base64,
                    analysis_types=analysis_types,
                    input_type="base64"
                )
            elif input_type == "url":
                # Выполняем анализ с URL
                analysis_result = analyze_image_tool(
                    image_input=image_input,
                    analysis_types=analysis_types,
                    input_type="url"
                )
            else:
                st.error("❌ Неизвестный тип входных данных")
                return
        
        if analysis_result.startswith("Ошибка:"):
            st.error(f"❌ {analysis_result}")
            return
        
        # Проверяем, что результат анализа не пустой
        if not analysis_result or not analysis_result.strip():
            st.error("❌ Результат анализа пустой")
            return
            
        st.success("✅ Анализ завершен")
        
        # Парсим результат анализа
        try:
            analysis_data = json.loads(analysis_result)
            
            # Показываем общее описание всегда
            if "general_description" in analysis_data:
                with st.expander("📋 Общее описание", expanded=True):
                    st.markdown(analysis_data["general_description"])
            
            # Показываем результаты динамически на основе выбранных типов анализа
            from custom_tools.image_tools import get_analysis_type_config
            
            for analysis_type in analysis_types:
                config = get_analysis_type_config(analysis_type)
                if config and "json_field" in config:
                    field_name = config["json_field"]
                    icon = config.get("icon", "📋")
                    
                    if field_name in analysis_data and analysis_data[field_name]:
                        with st.expander(f"{icon} {analysis_type}", expanded=True):
                            result = analysis_data[field_name]
                            
                            # Если это список объектов - форматируем красиво
                            if isinstance(result, list):
                                for item in result:
                                    st.markdown(f"• **{item}**")
                            else:
                                st.markdown(result)
                    
            # Показываем полный JSON ответ если включен детальный анализ
            if detailed:
                with st.expander("🔍 Полный JSON ответ", expanded=False):
                    st.json(analysis_data)
                    
        except json.JSONDecodeError:
            # Если не JSON, показываем как есть
            with st.expander("📋 Результат анализа", expanded=True):
                st.markdown(analysis_result)
        
        # Примечание для пользователя
        if analysis_types:
            st.info("ℹ️ Показаны результаты только для выбранных типов анализа. Если какой-то тип не отображается, возможно, он не был обнаружен на изображении.")
        
        # Генерируем умную сводку
        st.markdown("### 🧠 Умная сводка")
        
        with st.spinner("Генерация умной сводки..."):
            # Импортируем функцию для генерации сводки
            from custom_tools.image_tools import generate_smart_summary
            
            smart_summary_result = generate_smart_summary(analysis_result, analysis_types)
        
        if smart_summary_result and not smart_summary_result.startswith("Ошибка:"):
            try:
                smart_summary = json.loads(smart_summary_result)
                
                # Отображаем статистику и оценку качества
                summary_col1, summary_col2 = st.columns(2)
                
                with summary_col1:
                    st.markdown("**📊 Статистика анализа:**")
                    if "statistics" in smart_summary:
                        stats = smart_summary["statistics"]
                        st.info(f"Выполнено анализов: {stats.get('completed_analyses', len(analysis_types))}")
                        if "total_objects_found" in stats and stats["total_objects_found"] != "не определено":
                            st.info(f"Найдено объектов: {stats['total_objects_found']}")
                        if "main_colors_identified" in stats and stats["main_colors_identified"] != "не определено":
                            st.info(f"Основных цветов: {stats['main_colors_identified']}")
                        if "analysis_completeness" in stats:
                            st.info(f"Полнота анализа: {stats['analysis_completeness']}")
                        if "quality_indicator" in stats:
                            quality = stats["quality_indicator"]
                            if quality == "высокое":
                                st.success(f"Качество изображения: {quality}")
                            elif quality == "требует улучшения":
                                st.warning(f"Качество изображения: {quality}")
                            else:
                                st.info(f"Качество изображения: {quality}")
                    
                    st.info(f"Порог уверенности: {confidence_threshold}")
                
                with summary_col2:
                    st.markdown("**⭐ Оценка качества:**")
                    if "quality_assessment" in smart_summary:
                        quality = smart_summary["quality_assessment"]
                        if "overall_score" in quality:
                            score = quality["overall_score"]
                            st.metric("Общая оценка", f"{score}/10")
                        
                        if "main_strengths" in quality and quality["main_strengths"]:
                            st.markdown("**Сильные стороны:**")
                            for strength in quality["main_strengths"]:
                                st.success(f"✅ {strength}")
                        
                        if "areas_for_improvement" in quality and quality["areas_for_improvement"]:
                            st.markdown("**Области для улучшения:**")
                            for improvement in quality["areas_for_improvement"]:
                                st.warning(f"💡 {improvement}")
                
                # Показываем ключевые находки и рекомендации
                if "key_insights" in smart_summary and smart_summary["key_insights"]:
                    st.markdown("**🔍 Ключевые находки:**")
                    for insight in smart_summary["key_insights"]:
                        st.info(f"💎 {insight}")
                
                if "practical_recommendations" in smart_summary and smart_summary["practical_recommendations"]:
                    st.markdown("**🎯 Практические рекомендации:**")
                    for rec in smart_summary["practical_recommendations"]:
                        st.info(f"📝 {rec}")
                
            except json.JSONDecodeError:
                # Fallback на простую статистику
                st.markdown("**📊 Базовая статистика:**")
                st.info(f"Выполнено анализов: {len(analysis_types)}")
                st.info(f"Порог уверенности: {confidence_threshold}")
                st.warning("⚠️ Не удалось сгенерировать умную сводку")
        else:
            # Fallback на простую статистику при ошибке
            st.markdown("**📊 Базовая статистика:**")
            st.info(f"Выполнено анализов: {len(analysis_types)}")
            st.info(f"Порог уверенности: {confidence_threshold}")
            if smart_summary_result and smart_summary_result.startswith("Ошибка:"):
                st.warning(f"⚠️ {smart_summary_result}")
        
        # Экспорт результатов
        if st.button("📥 Экспорт результатов анализа"):
            # Подготавливаем полный экспорт
            export_data = {
                "metadata": {
                    "timestamp": datetime.now().isoformat(),
                    "analysis_types": analysis_types,
                    "confidence_threshold": confidence_threshold,
                    "input_type": input_type,
                    "version": "2.0"
                },
                "original_analysis": {},
                "smart_summary": {},
                "export_summary": {
                    "total_fields": 0,
                    "has_smart_summary": False
                }
            }
            
            # Добавляем оригинальные результаты анализа
            try:
                if analysis_result and not analysis_result.startswith("Ошибка:"):
                    original_data = json.loads(analysis_result)
                    export_data["original_analysis"] = original_data
                    export_data["export_summary"]["total_fields"] = len(original_data.keys())
            except json.JSONDecodeError:
                export_data["original_analysis"] = {"raw_result": analysis_result}
            
            # Добавляем умную сводку
            if smart_summary_result and not smart_summary_result.startswith("Ошибка:"):
                try:
                    smart_data = json.loads(smart_summary_result)
                    export_data["smart_summary"] = smart_data
                    export_data["export_summary"]["has_smart_summary"] = True
                except json.JSONDecodeError:
                    export_data["smart_summary"] = {"raw_summary": smart_summary_result}
            
            st.success("📥 Полные результаты анализа подготовлены к экспорту")
            
            # Показываем краткую информацию об экспорте
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Типов анализа", len(analysis_types))
            with col2:
                st.metric("Полей данных", export_data["export_summary"]["total_fields"])
            with col3:
                smart_status = "✅" if export_data["export_summary"]["has_smart_summary"] else "❌"
                st.metric("Умная сводка", smart_status)
            
            st.download_button(
                label="💾 Скачать полные результаты (JSON)",
                data=json.dumps(export_data, indent=2, ensure_ascii=False),
                file_name=f"image_analysis_full_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                mime="application/json",
                help="Скачать файл с оригинальными результатами анализа, умной сводкой и метаданными"
            )
    
    except Exception as e:
        st.error(f"❌ Ошибка анализа изображения: {e}")

def show_utility_tools():
    """Утилиты и вспомогательные инструменты"""
    
    st.markdown("## 🔧 Утилиты")
    
    # Вкладки утилит
    util_tab1, util_tab2, util_tab3, util_tab4 = st.tabs(["📊 Данные", "🔤 Текст", "🕐 Время", "🧮 Конвертеры"])
    
    with util_tab1:
        show_data_utilities()
    
    with util_tab2:
        show_text_utilities()
    
    with util_tab3:
        show_time_utilities()
    
    with util_tab4:
        show_converters()

def show_data_utilities():
    """Утилиты для работы с данными"""
    
    st.markdown("### 📊 Утилиты для данных")
    
    # JSON форматтер
    st.markdown("#### 📋 JSON Форматтер")
    
    json_input = st.text_area(
        "JSON для форматирования",
        height=150,
        placeholder='{"key": "value", "array": [1,2,3]}',
        help="Вставьте JSON для форматирования и валидации"
    )
    
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("✨ Форматировать JSON"):
            if json_input.strip():
                try:
                    parsed = json.loads(json_input)
                    formatted = json.dumps(parsed, indent=2, ensure_ascii=False)
                    st.code(formatted, language="json")
                    st.success("✅ JSON валиден и отформатирован")
                except json.JSONDecodeError as e:
                    st.error(f"❌ Ошибка JSON: {e}")
            else:
                st.warning("⚠️ Введите JSON")
    
    with col2:
        if st.button("🗜️ Минимизировать JSON"):
            if json_input.strip():
                try:
                    parsed = json.loads(json_input)
                    minified = json.dumps(parsed, separators=(',', ':'), ensure_ascii=False)
                    st.code(minified, language="json")
                    st.success("✅ JSON минимизирован")
                except json.JSONDecodeError as e:
                    st.error(f"❌ Ошибка JSON: {e}")
    
    # CSV анализатор
    st.markdown("#### 📈 CSV Анализатор")
    
    uploaded_csv = st.file_uploader(
        "Загрузить CSV файл",
        type=['csv'],
        help="Загрузите CSV файл для анализа"
    )
    
    if uploaded_csv is not None:
        try:
            import pandas as pd
            df = pd.read_csv(uploaded_csv)
            
            st.markdown("**📊 Информация о данных:**")
            
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric("📏 Строк", len(df))
            
            with col2:
                st.metric("📋 Колонок", len(df.columns))
            
            with col3:
                st.metric("💾 Размер", f"{uploaded_csv.size} байт")
            
            # Превью данных
            st.markdown("**👀 Превью данных:**")
            st.dataframe(df.head(), use_container_width=True)
            
            # Статистика
            if st.button("📊 Показать статистику"):
                st.markdown("**📈 Описательная статистика:**")
                st.dataframe(df.describe(), use_container_width=True)
        
        except Exception as e:
            st.error(f"❌ Ошибка обработки CSV: {e}")

def show_text_utilities():
    """Утилиты для работы с текстом"""
    
    st.markdown("### 🔤 Текстовые утилиты")
    
    # Анализ текста
    st.markdown("#### 📝 Анализ текста")
    
    text_input = st.text_area(
        "Текст для анализа",
        height=150,
        placeholder="Введите текст для анализа...",
        help="Введите любой текст для получения статистики"
    )
    
    if text_input.strip():
        # Базовая статистика
        words = text_input.split()
        sentences = text_input.split('.')
        paragraphs = text_input.split('\n\n')
        
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric("📄 Символов", len(text_input))
        
        with col2:
            st.metric("🔤 Слов", len(words))
        
        with col3:
            st.metric("📝 Предложений", len([s for s in sentences if s.strip()]))
        
        with col4:
            st.metric("📋 Параграфов", len([p for p in paragraphs if p.strip()]))
        
        # Дополнительный анализ
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("🔍 Анализ частоты слов"):
                from collections import Counter
                
                # Простая очистка текста
                clean_words = [word.lower().strip('.,!?;:"()[]') for word in words if len(word) > 2]
                word_freq = Counter(clean_words)
                
                st.markdown("**📊 Топ-10 слов:**")
                for word, count in word_freq.most_common(10):
                    st.markdown(f"- **{word}**: {count}")
        
        with col2:
            if st.button("📊 Статистика символов"):
                char_stats = {
                    "Буквы": sum(1 for c in text_input if c.isalpha()),
                    "Цифры": sum(1 for c in text_input if c.isdigit()),
                    "Пробелы": sum(1 for c in text_input if c.isspace()),
                    "Знаки препинания": sum(1 for c in text_input if c in '.,!?;:"()[]')
                }
                
                for stat_name, stat_value in char_stats.items():
                    st.markdown(f"- **{stat_name}**: {stat_value}")
    
    # Генератор хешей
    st.markdown("#### 🔐 Генератор хешей")
    
    hash_input = st.text_input(
        "Текст для хеширования",
        placeholder="Введите текст...",
        help="Текст будет хеширован различными алгоритмами"
    )
    
    if hash_input:
        import hashlib
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("**MD5:**")
            md5_hash = hashlib.md5(hash_input.encode()).hexdigest()
            st.code(md5_hash)
            
            st.markdown("**SHA1:**")
            sha1_hash = hashlib.sha1(hash_input.encode()).hexdigest()
            st.code(sha1_hash)
        
        with col2:
            st.markdown("**SHA256:**")
            sha256_hash = hashlib.sha256(hash_input.encode()).hexdigest()
            st.code(sha256_hash)
            
            st.markdown("**SHA512:**")
            sha512_hash = hashlib.sha512(hash_input.encode()).hexdigest()
            st.code(sha512_hash[:64] + "...")  # Укорачиваем для отображения

def show_time_utilities():
    """Утилиты для работы со временем"""
    
    st.markdown("### 🕐 Временные утилиты")
    
    # Текущее время
    st.markdown("#### ⏰ Текущее время")
    
    now = datetime.now()
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric("🕐 Локальное время", now.strftime("%H:%M:%S"))
        st.metric("📅 Дата", now.strftime("%Y-%m-%d"))
    
    with col2:
        import time
        timestamp = int(time.time())
        st.metric("⏱️ Unix timestamp", timestamp)
        st.metric("📊 ISO 8601", now.isoformat())
    
    with col3:
        day_of_year = now.timetuple().tm_yday
        week_number = now.isocalendar()[1]
        st.metric("📈 День года", day_of_year)
        st.metric("📅 Неделя года", week_number)
    
    # Конвертер временных зон
    st.markdown("#### 🌍 Конвертер временных зон")
    
    col1, col2 = st.columns(2)
    
    with col1:
        time_input = st.time_input("Время для конвертации", value=now.time())
        date_input = st.date_input("Дата", value=now.date())
    
    with col2:
        timezones = ["UTC", "Europe/Moscow", "Europe/London", "America/New_York", "Asia/Tokyo"]
        selected_tz = st.selectbox("Целевая временная зона", timezones)
        
        if st.button("🔄 Конвертировать"):
            st.info(f"Конвертация в {selected_tz}: {time_input} (заглушка)")
    
    # Калькулятор времени
    st.markdown("#### 🧮 Калькулятор времени")
    
    col1, col2 = st.columns(2)
    
    with col1:
        start_date = st.date_input("Начальная дата", key="start_date")
        start_time = st.time_input("Начальное время", key="start_time")
    
    with col2:
        end_date = st.date_input("Конечная дата", key="end_date")
        end_time = st.time_input("Конечное время", key="end_time")
    
    if st.button("⏱️ Вычислить разность"):
        start_datetime = datetime.combine(start_date, start_time)
        end_datetime = datetime.combine(end_date, end_time)
        
        diff = end_datetime - start_datetime
        
        st.markdown("**🕐 Разность времени:**")
        st.info(f"Дней: {diff.days}")
        st.info(f"Секунд: {diff.seconds}")
        st.info(f"Часов: {diff.total_seconds() / 3600:.2f}")

def show_converters():
    """Конвертеры различных форматов"""
    
    st.markdown("### 🧮 Конвертеры")
    
    # Base64 конвертер
    st.markdown("#### 🔤 Base64 конвертер")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("**➡️ Кодирование в Base64:**")
        encode_input = st.text_area("Текст для кодирования", key="base64_encode")
        
        if st.button("🔒 Кодировать"):
            if encode_input:
                encoded = base64.b64encode(encode_input.encode('utf-8')).decode('utf-8')
                st.code(encoded)
            else:
                st.warning("⚠️ Введите текст")
    
    with col2:
        st.markdown("**⬅️ Декодирование из Base64:**")
        decode_input = st.text_area("Base64 для декодирования", key="base64_decode")
        
        if st.button("🔓 Декодировать"):
            if decode_input:
                try:
                    decoded = base64.b64decode(decode_input).decode('utf-8')
                    st.code(decoded)
                except Exception as e:
                    st.error(f"❌ Ошибка декодирования: {e}")
            else:
                st.warning("⚠️ Введите Base64 строку")
    
    # URL кодировщик
    st.markdown("#### 🌐 URL кодировщик")
    
    col1, col2 = st.columns(2)
    
    with col1:
        url_input = st.text_input("URL для кодирования", placeholder="https://example.com/path with spaces")
        
        if st.button("🔗 Кодировать URL"):
            if url_input:
                import urllib.parse
                encoded_url = urllib.parse.quote(url_input, safe=':/?#[]@!$&\'()*+,;=')
                st.code(encoded_url)
    
    with col2:
        encoded_url_input = st.text_input("Кодированный URL", placeholder="https%3A//example.com/path%20with%20spaces")
        
        if st.button("🔓 Декодировать URL"):
            if encoded_url_input:
                import urllib.parse
                decoded_url = urllib.parse.unquote(encoded_url_input)
                st.code(decoded_url)
    
    # Цветовой конвертер
    st.markdown("#### 🎨 Цветовой конвертер")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        color_picker = st.color_picker("Выберите цвет", "#FF5733")
        
        # Конвертируем HEX в RGB
        hex_color = color_picker.lstrip('#')
        rgb = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
        
        st.markdown("**🎯 Значения цвета:**")
        st.code(f"HEX: {color_picker}")
        st.code(f"RGB: rgb({rgb[0]}, {rgb[1]}, {rgb[2]})")
        
        # HSL приблизительно
        r, g, b = [x/255.0 for x in rgb]
        max_val = max(r, g, b)
        min_val = min(r, g, b)
        h = s = l = (max_val + min_val) / 2
        
        st.code(f"HSL: приблизительно hsl({int(h*360)}, {int(s*100)}%, {int(l*100)}%)")
    
    with col2:
        st.markdown("**🔢 RGB в HEX:**")
        
        r_input = st.number_input("R", min_value=0, max_value=255, value=255, key="r_val")
        g_input = st.number_input("G", min_value=0, max_value=255, value=87, key="g_val")
        b_input = st.number_input("B", min_value=0, max_value=255, value=51, key="b_val")
        
        if st.button("🎨 RGB → HEX"):
            hex_result = f"#{r_input:02x}{g_input:02x}{b_input:02x}".upper()
            st.code(hex_result)
            st.color_picker("Результат", hex_result, disabled=True, key="rgb_to_hex_result")
    
    with col3:
        st.markdown("**📐 Единицы измерения:**")
        
        pixel_input = st.number_input("Пиксели", min_value=0, value=16)
        
        if st.button("📏 Конвертировать"):
            st.markdown("**Результаты:**")
            st.code(f"em: {pixel_input/16:.2f}em")
            st.code(f"rem: {pixel_input/16:.2f}rem")
            st.code(f"pt: {pixel_input*0.75:.1f}pt")
            st.code(f"% (от 16px): {(pixel_input/16)*100:.1f}%")

def show_available_tools():
    """Отображение всех доступных инструментов с фильтрацией по категориям"""
    
    st.markdown("## 📋 Доступные инструменты системы")
    
    try:
        # Получаем список инструментов из директории tool_definitions
        tools_dir = Path(project_root) / "tool_definitions"
        
        if not tools_dir.exists():
            st.error("❌ Директория tool_definitions не найдена")
            return
        
        tool_files = list(tools_dir.glob("*.yaml"))
        
        if not tool_files:
            st.warning("⚠️ Не найдено файлов инструментов")
            return
        
        # Загружаем конфигурации инструментов и извлекаем категории
        import yaml
        tools_data = {}
        all_categories = set()
        
        for tool_file in tool_files:
            try:
                with open(tool_file, 'r', encoding='utf-8') as f:
                    tool_config = yaml.safe_load(f)
                    
                category = tool_config.get('category', 'Общие')
                all_categories.add(category)
                
                tools_data[tool_file] = {
                    'config': tool_config,
                    'category': category,
                    'name': tool_config.get('name', tool_file.stem),
                    'description': tool_config.get('description', 'Нет описания')
                }
            except Exception as e:
                # Если не удается загрузить, используем fallback
                tools_data[tool_file] = {
                    'config': {},
                    'category': 'Ошибка',
                    'name': tool_file.stem,
                    'description': f'Ошибка загрузки: {e}'
                }
                all_categories.add('Ошибка')
        
        # Основная статистика
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric("🛠️ Всего инструментов", len(tool_files))
        
        with col2:
            st.metric("🏷️ Категорий", len(all_categories))
        
        with col3:
            total_size = sum(f.stat().st_size for f in tool_files) / 1024
            st.metric("💾 Общий размер", f"{total_size:.1f} KB")
        
        # Фильтры
        st.markdown("### 🔍 Фильтры")
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            search_term = st.text_input("🔍 Поиск инструментов", placeholder="Введите название...")
        
        with col2:
            # Фильтр по категориям
            sorted_categories = sorted(all_categories)
            selected_category = st.selectbox(
                "🏷️ Фильтр по категории", 
                options=["Все категории"] + sorted_categories,
                index=0
            )
        
        with col3:
            show_content = st.checkbox("📋 Показать содержимое", help="Отображать содержимое YAML файлов")
        
        # Применяем фильтры
        filtered_tools = []
        
        for tool_file, tool_data in tools_data.items():
            # Фильтр по названию
            if search_term and search_term.lower() not in tool_data['name'].lower():
                continue
                
            # Фильтр по категории
            if selected_category != "Все категории" and tool_data['category'] != selected_category:
                continue
                
            filtered_tools.append((tool_file, tool_data))
        
        st.markdown(f"### 🛠️ Инструменты ({len(filtered_tools)} найдено)")
        
        # Определяем иконки для категорий
        CATEGORY_ICONS = {
            "SQL": "🗄️",
            "Генерация": "🎨", 
            "Файлы": "📁",
            "Веб": "🌐",
            "Безопасность": "🔒",
            "Анализ": "📊",
            "NLP": "🧠",
            "Утилиты": "🔧",
            "Агенты": "🤖",
            "Общие": "⚙️",
            "Ошибка": "❌"
        }
        
        for tool_file, tool_data in sorted(filtered_tools, key=lambda x: x[1]['name']):
            tool_name = tool_data['name']
            category = tool_data['category']
            tool_config = tool_data['config']
            icon = CATEGORY_ICONS.get(category, "🔧")
            
            with st.expander(f"{icon} {tool_name} ({category})", expanded=False):
                
                col1, col2 = st.columns([2, 1])
                
                with col1:
                    st.markdown(f"**📝 Описание:** {tool_data['description']}")
                    st.markdown(f"**🏷️ Категория:** {category}")
                    st.markdown(f"**📁 Файл:** `{tool_file.name}`")
                    
                    # Параметры
                    if 'parameters' in tool_config:
                        st.markdown("**⚙️ Параметры:**")
                        parameters = tool_config['parameters']
                        if isinstance(parameters, list):
                            # Если parameters это список (как в YAML)
                            for param_info in parameters:
                                if isinstance(param_info, dict):
                                    param_name = param_info.get('name', 'unknown')
                                    param_type = param_info.get('type', 'unknown')
                                    param_desc = param_info.get('description', 'Нет описания')
                                    param_required = "✅ обязательный" if param_info.get('required', False) else "⚪ опциональный"
                                    st.markdown(f"- `{param_name}` ({param_type}) - {param_required}: {param_desc}")
                        elif isinstance(parameters, dict):
                            # Если parameters это словарь (старый формат)
                            for param_name, param_info in parameters.items():
                                param_type = param_info.get('type', 'unknown')
                                param_desc = param_info.get('description', 'Нет описания')
                                st.markdown(f"- `{param_name}` ({param_type}): {param_desc}")
                
                with col2:
                    file_size = tool_file.stat().st_size
                    st.info(f"**Размер:** {file_size} байт")
                    
                    mod_time = datetime.fromtimestamp(tool_file.stat().st_mtime)
                    st.info(f"**Изменен:** {mod_time.strftime('%Y-%m-%d %H:%M')}")
                    
                    if st.button(f"📋 Копировать путь", key=f"copy_path_{tool_name.replace(' ', '_')}"):
                        st.code(str(tool_file))
                
                # Показываем содержимое если включено
                if show_content:
                    try:
                        with open(tool_file, 'r', encoding='utf-8') as f:
                            content = f.read()
                        
                        st.markdown("**📋 Содержимое YAML:**")
                        st.code(content, language='yaml')
                    
                    except Exception as e:
                        st.error(f"❌ Ошибка чтения файла: {e}")
        
        # Статистика по категориям
        st.markdown("### 📊 Статистика по категориям")
        
        # Считаем реальную статистику из загруженных данных
        category_stats = {}
        for tool_data in tools_data.values():
            category = tool_data['category']
            category_stats[category] = category_stats.get(category, 0) + 1
        
        # Отображаем статистику
        for category, count in sorted(category_stats.items()):
            percentage = (count / len(tool_files)) * 100
            icon = CATEGORY_ICONS.get(category, "🔧")
            st.progress(percentage / 100)
            st.markdown(f"**{icon} {category}**: {count} инструментов ({percentage:.1f}%)")
    
    except Exception as e:
        st.error(f"❌ Ошибка загрузки инструментов: {e}")
        st.exception(e)

def show_agent_constructor_tab():
    """UI для создания нового агента на базе профиля agent_constructor с выбором тулов."""
    st.markdown("## 🤖 Создать нового агента")
    st.markdown("Опишите агента, укажите список инструментов (custom + MCP) и получите YAML‑профиль.")

    # Ввод описания
    description = st.text_area("📝 Описание агента", height=160, placeholder="Кто это, какие цели, какие входы/выходы и ограничения...")

    # Список доступных тулов
    from pathlib import Path as _Path
    import yaml as _yaml
    from mcp_tools import mcp_tools as _mcp_tools

    custom_dir = _Path(project_root) / "tool_definitions"
    custom_names = []
    if custom_dir.exists():
        for f in custom_dir.glob("*.yaml"):
            try:
                with open(f, 'r', encoding='utf-8') as _f:
                    cfg = _yaml.safe_load(_f) or {}
                    n = cfg.get('name')
                    if isinstance(n, str) and n:
                        custom_names.append(n)
            except Exception:
                pass
    custom_names = sorted(set(custom_names))

    mcp_names = []
    try:
        for t in _mcp_tools:
            n = getattr(t, 'name', None)
            if isinstance(n, str) and n:
                mcp_names.append(n)
    except Exception:
        pass
    mcp_names = sorted(set(mcp_names))

    st.markdown("### 🛠️ Выбор инструментов")
    col1, col2 = st.columns(2)
    with col1:
        sel_custom = st.multiselect("Custom инструменты", options=custom_names)
    with col2:
        sel_mcp = st.multiselect("MCP инструменты", options=mcp_names)

    tools_requested = sel_custom + sel_mcp

    # Имя файла (опционально)
    out_name = st.text_input("Имя агента (опц.)", placeholder="если оставить пустым — сгенерируется автоматически")

    # Кнопка создания
    if st.button("🚀 Создать агента", type="primary"):
        if not description.strip():
            st.error("❌ Заполните описание агента")
            return
        if not tools_requested:
            st.error("❌ Выберите хотя бы один инструмент")
            return

        # Запускаем ИМЕННО агента по профилю agent_constructor
        import uuid, time
        from agent_streamlit_api import AgentManager

        session_id = f"run-{uuid.uuid4().hex[:16]}"

        ctx = {}
        if out_name.strip():
            ctx['agent_name'] = out_name.strip()

        # Формируем задачу для агента (он сам вызовет нужные тули)
        task = (
            "Создай YAML-профиль нового агента по описанию и явному списку инструментов.\n"
            "Используй ТОЛЬКО переданные инструменты: проверяй их доступность (custom и MCP), не подбирай альтернативы.\n"
            "Сгенерируй план зависимостей и конфигураций, затем профиль. Верни путь к профилю и краткое резюме.\n\n"
            f"description: \n'''\n{description.strip()}\n'''\n\n"
            f"tools_requested: {json.dumps(tools_requested, ensure_ascii=False)}\n"
            f"context: {json.dumps(ctx, ensure_ascii=False)}\n"
        )

        with st.spinner("🤖 Агент-конструктор работает..."):
            try:
                manager = AgentManager()
                run_id = manager.run_agent(
                    agent_id_or_profile="agent_constructor",
                    task=task,
                    session_id=session_id
                )

                # Ожидаем завершения
                max_wait_seconds = 120
                start_time = time.time()
                status_placeholder = st.empty()
                progress_placeholder = st.empty()
                while time.time() - start_time < max_wait_seconds:
                    status = manager.get_agent_status(run_id)
                    elapsed = int(time.time() - start_time)
                    progress_placeholder.progress(min(elapsed / max_wait_seconds, 0.95))
                    if status and getattr(status, 'status', '') in ("completed", "failed"):
                        break
                    status_placeholder.info(f"⏳ Агент создаёт профиль... ({elapsed}с)")
                    time.sleep(1)

                status_placeholder.empty()
                progress_placeholder.empty()

                result_obj = manager.get_agent_result(run_id)
                final_output = getattr(result_obj, 'final_output', None) if result_obj else None
            except Exception as e:
                st.error(f"❌ Ошибка при запуске агента: {e}")
                return

        if final_output:
            st.success("✅ Агент завершил работу")
            st.markdown("**Итог:**")
            st.text(final_output)
        else:
            st.error("❌ Агент не вернул результирующий ответ")

if __name__ == "__main__":
    main()
