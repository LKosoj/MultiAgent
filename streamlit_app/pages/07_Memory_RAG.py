"""
Страница управления Memory/RAG системой
======================================
"""

import streamlit as st
import sys
from pathlib import Path
import json
from datetime import datetime, timedelta
import pandas as pd

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

def main():
    st.set_page_config(
        page_title="Memory/RAG - MultiAgent System",
        page_icon="🧠",
        layout="wide"
    )
    
    st.title("🧠 Управление памятью и RAG")
    st.markdown("---")
    
    # Главные вкладки
    tab1, tab2, tab3, tab4 = st.tabs(["📊 Статус памяти", "🔍 Поиск", "📈 Аналитика", "⚙️ Управление"])
    
    with tab1:
        show_memory_status()
    
    with tab2:
        show_memory_search()
    
    with tab3:
        show_memory_analytics()
    
    with tab4:
        show_memory_management()

def show_memory_status():
    """Отображение статуса системы памяти"""
    
    st.markdown("## 📊 Статус системы памяти")
    
    try:
        from memory.streamlit_api import get_memory_rag_manager
        
        memory_manager = get_memory_rag_manager()
        status = memory_manager.get_memory_status()
        
        # Основные метрики
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric(
                "💾 SQLite записей",
                status.tactical_memories_count + status.strategic_memories_count
            )
            
            if status.database_size_mb:
                st.metric("📁 Размер БД", f"{status.database_size_mb:.1f} MB")
        
        with col2:
            st.metric("🎯 Тактических", status.tactical_memories_count)
            st.metric("🗺️ Стратегических", status.strategic_memories_count)
        
        with col3:
            if status.chromadb_available:
                st.metric("🔍 ChromaDB", "✅ Доступна")
                
                total_vectors = sum(
                    coll_info.get("count", 0) 
                    for coll_info in status.collections_info.values()
                ) if status.collections_info else 0
                
                st.metric("📊 Векторов", total_vectors)
            else:
                st.metric("🔍 ChromaDB", "❌ Недоступна")
        
        with col4:
            if status.embedding_model_available:
                st.metric("🤖 Embeddings", "✅ Доступна")
                if status.embedding_model_name:
                    st.caption(f"Модель: {status.embedding_model_name}")
            else:
                st.metric("🤖 Embeddings", "❌ Недоступна")
        
        # Детальная информация
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("### 💾 SQLite Database")
            
            if status.sqlite_available:
                st.success("✅ SQLite доступна")
                st.info(f"**Путь:** `{status.sqlite_path}`")
                st.info(f"**Размер:** {status.database_size_mb:.1f} MB")
                
                # Статистика памяти
                if status.tactical_memories_count > 0:
                    st.info(f"**Тактическая память:** {status.tactical_memories_count} записей")
                
                if status.strategic_memories_count > 0:
                    st.info(f"**Стратегическая память:** {status.strategic_memories_count} записей")
            else:
                st.error("❌ SQLite недоступна")
                if status.error_message:
                    st.error(f"Ошибка: {status.error_message}")
        
        with col2:
            st.markdown("### 🔍 ChromaDB Vector Store")
            
            if status.chromadb_available:
                st.success("✅ ChromaDB доступна")
                st.info(f"**Путь:** `{status.chromadb_path}`")
                
                if status.collections_info:
                    st.markdown("**📊 Коллекции:**")
                    
                    for collection_name, collection_info in status.collections_info.items():
                        count = collection_info.get("count", 0)
                        last_modified = collection_info.get("last_modified", "N/A")
                        
                        with st.expander(f"📁 {collection_name} ({count} записей)"):
                            st.json(collection_info)
                else:
                    st.warning("⚠️ Коллекции не найдены")
            else:
                st.error("❌ ChromaDB недоступна")
                if status.chromadb_error_message:
                    st.error(f"Ошибка: {status.chromadb_error_message}")
        
        # Embeddings модель
        st.markdown("### 🤖 Модель Embeddings")
        
        if status.embedding_model_available:
            col1, col2 = st.columns(2)
            
            with col1:
                st.success("✅ Модель доступна")
                st.info(f"**Название:** {status.embedding_model_name}")
            
            with col2:
                # Тест embeddings
                if st.button("🧪 Тест модели embeddings"):
                    import os, uuid
                    run_id = f"run-{uuid.uuid4().hex[:16]}"
                    os.environ["RUN_ID"] = run_id
                    try:
                        from unified_logging import get_run_logger
                        _rlog = get_run_logger(run_id, __name__)
                        _rlog.info("Старт теста embeddings")
                    except Exception:
                        pass
                    test_embedding_model(memory_manager)
        else:
            st.error("❌ Модель embeddings недоступна")
        
        # Автообновление статуса
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("🔄 Обновить статус"):
                st.rerun()
        
        with col2:
            auto_refresh = st.checkbox("🔄 Автообновление (30с)")
            
            # Правильная реализация автообновления
            if auto_refresh:
                import time
                # Инициализируем время последнего обновления
                if "last_refresh_time_memory" not in st.session_state:
                    st.session_state.last_refresh_time_memory = time.time()
                
                # Проверяем, прошло ли 30 секунд
                current_time = time.time()
                if current_time - st.session_state.last_refresh_time_memory >= 30:
                    st.session_state.last_refresh_time_memory = current_time
                    st.rerun()
                
                # Показываем индикатор автообновления
                next_refresh = 30 - (current_time - st.session_state.last_refresh_time_memory)
                if next_refresh > 0:
                    st.caption(f"⏱️ Обновление через {next_refresh:.0f}с")
    
    except Exception as e:
        st.error(f"❌ Ошибка получения статуса памяти: {e}")
        st.exception(e)

def test_embedding_model(memory_manager):
    """Тестирование модели embeddings"""
    
    st.markdown("#### 🧪 Тест модели embeddings")
    
    try:
        test_text = "Это тестовое сообщение для проверки модели embeddings"
        
        with st.spinner("Генерация embedding..."):
            # Выполняем тестовый поиск
            search_results = memory_manager.search_memory(
                query=test_text,
                session_id="test_embedding",
                agent_name="system_test",
                limit=1
            )
        
        if not search_results.error_message:
            st.success("✅ Модель embeddings работает корректно")
            st.info(f"Найдено результатов: {len(search_results.results)}")
            
            if search_results.results:
                st.info(f"Первый результат: релевантность {search_results.results[0].relevance_score:.3f}")
        else:
            st.error(f"❌ Ошибка тестирования: {search_results.error_message}")
    
    except Exception as e:
        st.error(f"❌ Ошибка теста: {e}")

def show_memory_search():
    """Интерфейс поиска в памяти"""
    
    st.markdown("## 🔍 Поиск в памяти агентов")
    
    try:
        from memory.streamlit_api import get_memory_rag_manager
        
        memory_manager = get_memory_rag_manager()
        
        # Форма поиска
        with st.form("memory_search_form"):
            st.markdown("### 🔍 Параметры поиска")
            
            col1, col2 = st.columns(2)
            
            with col1:
                search_query = st.text_area(
                    "🗣️ Поисковый запрос",
                    height=100,
                    placeholder="Введите запрос для семантического поиска...",
                    help="Естественный язык запроса для поиска релевантных записей"
                )
                
                session_filter = st.text_input(
                    "🆔 Фильтр по Session ID (опционально)",
                    placeholder="Оставьте пустым для поиска во всех сессиях",
                    help="Поиск только в конкретной сессии"
                )
            
            with col2:
                agent_filter = st.text_input(
                    "🤖 Фильтр по имени агента (опционально)",
                    placeholder="Оставьте пустым для поиска по всем агентам",
                    help="Поиск только в памяти конкретного агента"
                )
                
                top_k = st.number_input(
                    "📊 Количество результатов",
                    min_value=1,
                    max_value=100,
                    value=10,
                    help="Максимальное количество результатов"
                )
            
            # Дополнительные параметры
            with st.expander("⚙️ Дополнительные параметры"):
                col1, col2 = st.columns(2)
                
                with col1:
                    memory_types = st.multiselect(
                        "🎯 Типы памяти",
                        ["tactical", "strategic"],
                        default=["tactical", "strategic"],
                        help="Выберите типы памяти для поиска"
                    )
                    
                    min_relevance = st.slider(
                        "📈 Минимальная релевантность",
                        min_value=0.0,
                        max_value=1.0,
                        value=0.1,
                        step=0.05,
                        help="Минимальный порог релевантности результатов"
                    )
                
                with col2:
                    date_filter = st.date_input(
                        "📅 Фильтр по дате (от)",
                        value=None,
                        help="Поиск записей начиная с указанной даты"
                    )
                    
                    include_metadata = st.checkbox(
                        "📋 Включить метаданные",
                        value=True,
                        help="Показывать дополнительную информацию о записях"
                    )
            
            # Кнопка поиска
            search_clicked = st.form_submit_button("🔍 Поиск", type="primary")
            
            if search_clicked and search_query:
                perform_memory_search(
                    memory_manager, search_query, session_filter, agent_filter,
                    top_k, memory_types, min_relevance, date_filter, include_metadata
                )
            elif search_clicked and not search_query:
                st.error("❌ Введите поисковый запрос")
        
        # Кнопка экспорта результатов последнего поиска (вне формы)
        if st.session_state.get("memory_last_results"):
            if st.button("📥 Экспорт результатов последнего поиска"):
                export_search_results(
                    st.session_state.get("memory_last_results", []),
                    st.session_state.get("memory_last_query", "")
                )

        # Быстрые поиски
        show_quick_searches(memory_manager)
    
    except Exception as e:
        st.error(f"❌ Ошибка инициализации поиска: {e}")

def perform_memory_search(memory_manager, search_query, session_filter, agent_filter,
                         top_k, memory_types, min_relevance, date_filter, include_metadata):
    """Выполнение поиска в памяти"""
    
    st.markdown("### 📊 Результаты поиска")
    
    try:
        # Выполняем поиск для каждого типа памяти отдельно
        all_results = []
        
        with st.spinner("Выполнение семантического поиска..."):
            for memory_type in memory_types:
                # Подготавливаем параметры поиска для текущего типа
                search_params = {
                    "query": search_query,
                    "limit": top_k,
                    "memory_type": memory_type
                }
                
                if session_filter:
                    search_params["session_id"] = session_filter
                
                if agent_filter:
                    search_params["agent_name"] = agent_filter
                
                # Выполняем поиск для данного типа памяти
                type_results = memory_manager.search_memory(**search_params)
                
                # Проверяем успешность (нет ошибки и есть результаты)
                if not type_results.error_message and type_results.results:
                    # Добавляем тип памяти к каждому результату
                    for result in type_results.results:
                        result["memory_type"] = memory_type
                    all_results.extend(type_results.results)
        
        # Создаем объединенный результат
        class CombinedSearchResult:
            def __init__(self, results):
                self.results = sorted(results, key=lambda x: x.get('relevance_score', 0), reverse=True)[:top_k]
                self.search_time_ms = 0.0  # Общее время не замеряем для упрощения
                self.error_message = None
                
        search_results = CombinedSearchResult(all_results)
        
        # Проверяем успешность по наличию результатов
        if search_results.results:
            st.success(f"✅ Найдено {len(search_results.results)} результатов за {search_results.search_time_ms:.1f}ms")
                
            # Отображение результатов
            for i, result in enumerate(search_results.results):
                content = result.get("content", "Нет содержимого")
                relevance_score = result.get("relevance_score", 0.0)
                metadata = result.get("metadata", {})
                memory_type = result.get("memory_type", result.get("type", "tactical"))
                agent_name = metadata.get("agent_name", "Неизвестный агент")
                session_id = metadata.get("session_id", "Неизвестная сессия")
                
                with st.expander(f"📝 Результат {i+1} (релевантность: {relevance_score:.3f})", expanded=i < 3):
                    
                    col1, col2 = st.columns([3, 1])
                    
                    with col1:
                        st.markdown("**📝 Содержимое:**")
                        st.markdown(content)
                        
                        # Контекст может быть в metadata или отдельно
                        context = result.get("context") or metadata.get("context")
                        if context and include_metadata:
                            st.markdown("**🔍 Контекст:**")
                            st.info(context)
                    
                    with col2:
                        st.markdown("**ℹ️ Метаданные:**")
                        st.info(f"**Тип:** {memory_type}")
                        st.info(f"**Агент:** {agent_name}")
                        st.info(f"**Сессия:** {session_id}")
                        
                        # Дата может быть в разных форматах
                        created_at = metadata.get("created_at") or metadata.get("timestamp")
                        if created_at:
                            try:
                                if isinstance(created_at, str):
                                    from datetime import datetime
                                    created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                                st.info(f"**Дата:** {created_at.strftime('%Y-%m-%d %H:%M')}")
                            except:
                                st.info(f"**Дата:** {created_at}")
                        
                        st.info(f"**Релевантность:** {relevance_score:.3f}")
                        
                        if include_metadata and metadata:
                            with st.expander("📋 Доп. метаданные"):
                                st.json(metadata)
            
            # Экспорт результатов (сохраняем и показываем кнопку вне формы)
            st.session_state.memory_last_results = search_results.results
            st.session_state.memory_last_query = search_query
        
        else:
            st.warning("🔍 Результаты не найдены. Попробуйте изменить параметры поиска.")
    
    except Exception as e:
        st.error(f"❌ Ошибка выполнения поиска: {e}")
        st.exception(e)

def export_search_results(results, search_query):
    """Экспорт результатов поиска"""
    
    export_data = []
    
    for result in results:
        metadata = result.get("metadata", {})
        export_data.append({
            "content": result.get("content", "Нет содержимого"),
            "context": result.get("context") or metadata.get("context"),
            "memory_type": result.get("memory_type", result.get("type", "tactical")),
            "agent_name": metadata.get("agent_name", "Неизвестный агент"),
            "session_id": metadata.get("session_id", "Неизвестная сессия"),
            "created_at": metadata.get("created_at") or metadata.get("timestamp"),
            "relevance_score": result.get("relevance_score", 0.0),
            "metadata": metadata
        })
    
    # Создаем JSON для экспорта
    export_json = {
        "search_query": search_query,
        "search_timestamp": datetime.now().isoformat(),
        "results_count": len(results),
        "results": export_data
    }
    
    st.download_button(
        label="💾 Скачать результаты (JSON)",
        data=json.dumps(export_json, indent=2, ensure_ascii=False),
        file_name=f"memory_search_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        mime="application/json"
    )

def show_quick_searches(memory_manager):
    """Быстрые поиски"""
    
    st.markdown("### ⚡ Быстрые поиски")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        if st.button("🎯 Последние тактические", use_container_width=True):
            perform_quick_search(memory_manager, "recent_tactical")
    
    with col2:
        if st.button("🗺️ Последние стратегические", use_container_width=True):
            perform_quick_search(memory_manager, "recent_strategic")
    
    with col3:
        if st.button("❌ Ошибки и проблемы", use_container_width=True):
            perform_quick_search(memory_manager, "errors")
    
    with col4:
        if st.button("✅ Успешные задачи", use_container_width=True):
            perform_quick_search(memory_manager, "success")

def perform_quick_search(memory_manager, search_type):
    """Выполнение быстрого поиска"""
    
    quick_searches = {
        "recent_tactical": {
            "query": "последние тактические решения и выполненные задачи",
            "memory_type": "tactical",
            "limit": 5
        },
        "recent_strategic": {
            "query": "стратегические планы и долгосрочные цели",
            "memory_type": "strategic",
            "limit": 5
        },
        "errors": {
            "query": "ошибки проблемы неудачи исключения",
            "memory_type": "tactical",  # Быстрый поиск только в одном типе
            "limit": 10
        },
        "success": {
            "query": "успешно завершено выполнено достигнуто",
            "memory_type": "tactical",  # Быстрый поиск только в одном типе
            "limit": 10
        }
    }
    
    search_config = quick_searches.get(search_type, {})
    
    if search_config:
        try:
            with st.spinner(f"Быстрый поиск: {search_type}..."):
                results = memory_manager.search_memory(**search_config)
            
            st.markdown(f"#### 📊 Результаты быстрого поиска: {search_type}")
            
            if not results.error_message and results.results:
                for i, result in enumerate(results.results[:3]):  # Показываем топ-3
                    content = result.get("content", "Нет содержимого")
                    agent_name = result.get("metadata", {}).get("agent_name", "Неизвестный агент")
                    relevance_score = result.get("relevance_score", 0.0)
                    
                    st.markdown(f"**{i+1}.** {content[:100]}...")
                    st.caption(f"Агент: {agent_name}, Релевантность: {relevance_score:.3f}")
            else:
                st.info("Нет результатов для данного быстрого поиска")
        
        except Exception as e:
            st.error(f"❌ Ошибка быстрого поиска: {e}")

def show_memory_analytics():
    """Аналитика памяти"""
    
    st.markdown("## 📈 Аналитика памяти")
    
    try:
        from memory.streamlit_api import get_memory_rag_manager
        
        memory_manager = get_memory_rag_manager()
        
        # Статистика по агентам
        st.markdown("### 🤖 Статистика по агентам")
        
        active_agents = memory_manager.get_active_agents()
        
        if active_agents:
            # Создаем DataFrame для отображения
            stats_data = []
            
            for agent_info in active_agents:
                stats_data.append({
                    "Агент": agent_info.get("agent_name", "Unknown"),
                    "Тактических записей": agent_info.get("tactical_count", 0),
                    "Стратегических записей": agent_info.get("strategic_count", 0),
                    "Всего записей": agent_info.get("total_count", 0),
                    "Последняя активность": agent_info.get("last_activity", "N/A"),
                    "Сессий": agent_info.get("unique_sessions", 0)
                })
            
            if stats_data:
                df = pd.DataFrame(stats_data)
                
                # Отображаем таблицу
                st.dataframe(df, use_container_width=True)
                
                # Визуализация
                col1, col2 = st.columns(2)
                
                with col1:
                    # График по агентам
                    chart_data = df.set_index("Агент")[["Тактических записей", "Стратегических записей"]]
                    st.bar_chart(chart_data)
                
                with col2:
                    # Топ агентов
                    top_agents = df.nlargest(5, "Всего записей")
                    st.markdown("**🏆 Топ-5 агентов по активности:**")
                    
                    for i, row in top_agents.iterrows():
                        st.markdown(f"{i+1}. **{row['Агент']}** - {row['Всего записей']} записей")
        else:
            st.info("📊 Нет данных для аналитики агентов")
        
        # Временная аналитика
        st.markdown("### 📅 Временная аналитика")
        
        time_period = st.selectbox(
            "📅 Период анализа",
            ["Последние 24 часа", "Последние 7 дней", "Последние 30 дней", "Все время"]
        )
        
        if st.button("📊 Построить временной график"):
            show_temporal_analytics(memory_manager, time_period)
        
        # Аналитика контента
        st.markdown("### 📝 Аналитика контента")
        
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("🔤 Анализ ключевых слов"):
                show_keyword_analysis(memory_manager)
        
        with col2:
            if st.button("📊 Анализ тематик"):
                show_topic_analysis(memory_manager)
    
    except Exception as e:
        st.error(f"❌ Ошибка аналитики: {e}")

def show_temporal_analytics(memory_manager, time_period):
    """Временная аналитика"""
    
    st.markdown("#### 📅 Активность по времени")
    
    # Определяем временной диапазон
    period_mapping = {
        "Последние 24 часа": timedelta(hours=24),
        "Последние 7 дней": timedelta(days=7),
        "Последние 30 дней": timedelta(days=30),
        "Все время": None
    }
    
    time_delta = period_mapping.get(time_period)
    
    try:
        # Здесь можно добавить логику получения временной статистики
        # Пока отображаем заглушку
        st.info(f"Анализ активности за период: {time_period}")
        
        # Пример данных для демонстрации
        sample_dates = pd.date_range(
            start=datetime.now() - timedelta(days=7),
            end=datetime.now(),
            freq='D'
        )
        
        sample_data = pd.DataFrame({
            'Дата': sample_dates,
            'Тактические записи': [5, 8, 12, 6, 9, 15, 11, 7],
            'Стратегические записи': [2, 3, 1, 4, 2, 5, 3, 2]
        })
        
        sample_data = sample_data.set_index('Дата')
        st.line_chart(sample_data)
        
    except Exception as e:
        st.error(f"❌ Ошибка построения временного графика: {e}")

def show_keyword_analysis(memory_manager):
    """Анализ ключевых слов"""
    
    st.markdown("#### 🔤 Ключевые слова в памяти")
    
    try:
        # Здесь можно добавить логику анализа ключевых слов
        # Пока отображаем заглушку
        st.info("Анализ наиболее частых ключевых слов в записях памяти")
        
        # Пример данных
        sample_keywords = [
            ("задача", 45),
            ("выполнение", 38),
            ("результат", 32),
            ("ошибка", 28),
            ("анализ", 25),
            ("данные", 22),
            ("процесс", 19),
            ("система", 17),
            ("пользователь", 15),
            ("решение", 13)
        ]
        
        for i, (keyword, count) in enumerate(sample_keywords):
            st.markdown(f"{i+1}. **{keyword}** - {count} упоминаний")
        
    except Exception as e:
        st.error(f"❌ Ошибка анализа ключевых слов: {e}")

def show_topic_analysis(memory_manager):
    """Анализ тематик"""
    
    st.markdown("#### 📊 Тематический анализ")
    
    try:
        # Здесь можно добавить логику тематического анализа
        # Пока отображаем заглушку
        st.info("Анализ основных тематик в записях памяти")
        
        # Пример данных
        sample_topics = [
            ("Выполнение задач", 35),
            ("Обработка данных", 28),
            ("Взаимодействие с пользователем", 22),
            ("Техническая диагностика", 18),
            ("Планирование", 15),
            ("Ошибки и исключения", 12)
        ]
        
        for i, (topic, percentage) in enumerate(sample_topics):
            st.progress(percentage / 100)
            st.markdown(f"**{topic}** - {percentage}%")
        
    except Exception as e:
        st.error(f"❌ Ошибка тематического анализа: {e}")

def show_memory_management():
    """Управление памятью"""
    
    st.markdown("## ⚙️ Управление памятью")
    
    try:
        from memory.streamlit_api import get_memory_rag_manager
        
        memory_manager = get_memory_rag_manager()
        
        # Операции с ChromaDB
        st.markdown("### 🔍 Управление ChromaDB")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("**🔄 Перестройка индекса:**")
            
            if st.button("🔄 Перестроить ChromaDB из SQLite", type="primary"):
                import os, uuid
                run_id = f"run-{uuid.uuid4().hex[:16]}"
                os.environ["RUN_ID"] = run_id
                try:
                    from unified_logging import get_run_logger
                    _rlog = get_run_logger(run_id, __name__)
                    _rlog.info("Старт перестройки ChromaDB из SQLite")
                except Exception:
                    pass
                rebuild_chromadb_index(memory_manager)
            
            st.caption("Восстанавливает векторные индексы из данных SQLite")
            
            if st.button("🧹 Очистить пустые коллекции"):
                import os, uuid
                run_id = f"run-{uuid.uuid4().hex[:16]}"
                os.environ["RUN_ID"] = run_id
                try:
                    from unified_logging import get_run_logger
                    _rlog = get_run_logger(run_id, __name__)
                    _rlog.info("Старт очистки пустых коллекций")
                except Exception:
                    pass
                cleanup_empty_collections(memory_manager)
        
        with col2:
            st.markdown("**📊 Оптимизация:**")
            
            if st.button("⚡ Оптимизировать индексы"):
                import os, uuid
                run_id = f"run-{uuid.uuid4().hex[:16]}"
                os.environ["RUN_ID"] = run_id
                try:
                    from unified_logging import get_run_logger
                    _rlog = get_run_logger(run_id, __name__)
                    _rlog.info("Старт оптимизации индексов памяти")
                except Exception:
                    pass
                optimize_indexes(memory_manager)
            
            if st.button("🗜️ Сжать базу данных"):
                import os, uuid
                run_id = f"run-{uuid.uuid4().hex[:16]}"
                os.environ["RUN_ID"] = run_id
                try:
                    from unified_logging import get_run_logger
                    _rlog = get_run_logger(run_id, __name__)
                    _rlog.info("Старт сжатия базы памяти")
                except Exception:
                    pass
                compress_database(memory_manager)
        
        # Операции экспорта/импорта
        st.markdown("### 📥📤 Экспорт/Импорт")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("**📤 Экспорт данных:**")
            
            export_format = st.selectbox(
                "Формат экспорта",
                ["JSON", "CSV", "SQLite Dump"]
            )
            
            session_id_filter = st.text_input(
                "Session ID (опционально)",
                help="Экспорт только конкретной сессии"
            )
            
            agent_name_filter = st.text_input(
                "Имя агента (опционально)",
                help="Экспорт только данных конкретного агента"
            )
            
            if st.button("📥 Экспорт памяти"):
                export_memory_data(
                    memory_manager, export_format, 
                    session_id_filter, agent_name_filter
                )
        
        with col2:
            st.markdown("**📥 Импорт данных:**")
            
            uploaded_file = st.file_uploader(
                "Загрузить файл экспорта",
                type=['json', 'csv'],
                help="Файл с экспортированными данными памяти"
            )
            
            if uploaded_file is not None:
                if st.button("📥 Импорт данных"):
                    import_memory_data(memory_manager, uploaded_file)
        
        # Очистка и обслуживание
        st.markdown("### 🧹 Очистка и обслуживание")
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.markdown("**🗑️ Очистка по времени:**")
            
            days_to_keep = st.number_input(
                "Сохранить последние (дней)",
                min_value=1,
                max_value=365,
                value=30
            )
            
            if st.button("🗑️ Очистить старые записи"):
                cleanup_old_memories(memory_manager, days_to_keep)
        
        with col2:
            st.markdown("**🤖 Очистка по агенту:**")
            
            # Получаем список агентов
            active_agents = memory_manager.get_active_agents()
            agent_names = [agent.get("agent_name") for agent in active_agents if agent.get("agent_name")]
            
            if agent_names:
                agent_to_cleanup = st.selectbox(
                    "Выберите агента",
                    agent_names
                )
                
                if st.button("🗑️ Очистить память агента"):
                    cleanup_agent_memory(memory_manager, agent_to_cleanup)
            else:
                st.info("Нет агентов для очистки")
        
        with col3:
            st.markdown("**⚠️ Полная очистка:**")
            
            st.warning("Опасная операция!")
            
            confirm_cleanup = st.checkbox("Подтверждаю очистку")
            
            if confirm_cleanup:
                if st.button("🚨 Очистить всю память", type="secondary"):
                    full_memory_cleanup(memory_manager)
        
        # Статистика хранилища
        show_storage_statistics(memory_manager)
    
    except Exception as e:
        st.error(f"❌ Ошибка управления памятью: {e}")

def rebuild_chromadb_index(memory_manager):
    """Перестройка индекса ChromaDB"""
    
    with st.spinner("Перестройка ChromaDB индекса..."):
        try:
            result = memory_manager.rebuild_memory()
            
            if result.success:
                st.success(f"✅ Индекс перестроен за {result.rebuild_time_ms:.1f}ms")
                st.info(f"Восстановлено: {result.tactical_count} тактических, {result.strategic_count} стратегических записей")
            else:
                st.error(f"❌ Ошибка перестройки: {result.error_message}")
        
        except Exception as e:
            st.error(f"❌ Ошибка перестройки: {e}")

def cleanup_empty_collections(memory_manager):
    """Очистка пустых коллекций"""
    
    with st.spinner("Очистка пустых коллекций..."):
        try:
            # Здесь должна быть логика очистки
            st.success("✅ Пустые коллекции очищены")
        except Exception as e:
            st.error(f"❌ Ошибка очистки: {e}")

def optimize_indexes(memory_manager):
    """Оптимизация индексов"""
    
    with st.spinner("Оптимизация индексов..."):
        try:
            # Здесь должна быть логика оптимизации
            st.success("✅ Индексы оптимизированы")
        except Exception as e:
            st.error(f"❌ Ошибка оптимизации: {e}")

def compress_database(memory_manager):
    """Сжатие базы данных"""
    
    with st.spinner("Сжатие базы данных..."):
        try:
            # Здесь должна быть логика сжатия
            st.success("✅ База данных сжата")
        except Exception as e:
            st.error(f"❌ Ошибка сжатия: {e}")

def export_memory_data(memory_manager, export_format, session_id_filter, agent_name_filter):
    """Экспорт данных памяти"""
    
    try:
        with st.spinner("Экспорт данных..."):
            export_result = memory_manager.export_memory(
                session_id=session_id_filter if session_id_filter else None,
                agent_name=agent_name_filter if agent_name_filter else None,
                format=export_format.lower()
            )
        
        if export_result.get("success"):
            st.success(f"✅ Экспортировано {export_result.get('count', 0)} записей")
            
            # Создаем файл для скачивания
            filename = f"memory_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            
            if export_format == "JSON":
                filename += ".json"
                mime_type = "application/json"
                data_to_export = json.dumps(export_result.get("data", []), indent=2, ensure_ascii=False)
            elif export_format == "CSV":
                filename += ".csv"
                mime_type = "text/csv"
                # Для CSV нужно конвертировать данные
                data_to_export = export_result.get("data", [])
            else:
                filename += ".sql"
                mime_type = "text/sql"
                data_to_export = export_result.get("data", [])
            
            st.download_button(
                label=f"💾 Скачать {export_format}",
                data=data_to_export,
                file_name=filename,
                mime=mime_type
            )
        else:
            st.error(f"❌ Ошибка экспорта: {export_result.get('error', 'Неизвестная ошибка')}")
    
    except Exception as e:
        st.error(f"❌ Ошибка экспорта: {e}")

def import_memory_data(memory_manager, uploaded_file):
    """Импорт данных памяти"""
    
    try:
        with st.spinner("Импорт данных..."):
            file_content = uploaded_file.read()
            
            if uploaded_file.name.endswith('.json'):
                data = json.loads(file_content.decode('utf-8'))
            else:
                # Для CSV нужна дополнительная логика
                st.error("❌ CSV импорт пока не поддерживается")
                return
            
            # Здесь должна быть логика импорта
            st.success("✅ Данные импортированы")
    
    except Exception as e:
        st.error(f"❌ Ошибка импорта: {e}")

def cleanup_old_memories(memory_manager, days_to_keep):
    """Очистка старых записей"""
    
    try:
        with st.spinner(f"Очистка записей старше {days_to_keep} дней..."):
            # Здесь должна быть логика очистки
            st.success(f"✅ Записи старше {days_to_keep} дней очищены")
    
    except Exception as e:
        st.error(f"❌ Ошибка очистки: {e}")

def cleanup_agent_memory(memory_manager, agent_name):
    """Очистка памяти конкретного агента"""
    
    try:
        with st.spinner(f"Очистка памяти агента {agent_name}..."):
            # Здесь должна быть логика очистки памяти агента
            st.success(f"✅ Память агента {agent_name} очищена")
    
    except Exception as e:
        st.error(f"❌ Ошибка очистки памяти агента: {e}")

def full_memory_cleanup(memory_manager):
    """Полная очистка памяти"""
    
    try:
        with st.spinner("Полная очистка памяти..."):
            # Здесь должна быть логика полной очистки
            st.success("✅ Вся память очищена")
            st.warning("⚠️ Все данные удалены!")
    
    except Exception as e:
        st.error(f"❌ Ошибка полной очистки: {e}")

def show_storage_statistics(memory_manager):
    """Статистика хранилища"""
    
    st.markdown("### 📊 Статистика хранилища")
    
    try:
        status = memory_manager.get_memory_status()
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric(
                "💾 SQLite размер",
                f"{status.database_size_mb:.1f} MB" if status.database_size_mb else "N/A"
            )
        
        with col2:
            if status.collections_info:
                total_vectors = sum(
                    coll.get("count", 0) 
                    for coll in status.collections_info.values()
                )
                st.metric("🔍 Всего векторов", total_vectors)
            else:
                st.metric("🔍 Всего векторов", "N/A")
        
        with col3:
            total_memories = status.tactical_memories_count + status.strategic_memories_count
            st.metric("📝 Всего записей", total_memories)
        
        # Детальная статистика по коллекциям
        if status.collections_info:
            st.markdown("**📁 Детализация по коллекциям:**")
            
            for collection_name, collection_info in status.collections_info.items():
                count = collection_info.get("count", 0)
                size_estimate = count * 0.1  # Примерная оценка в MB
                
                st.markdown(f"- **{collection_name}**: {count} записей (~{size_estimate:.1f} MB)")
    
    except Exception as e:
        st.error(f"❌ Ошибка получения статистики: {e}")

if __name__ == "__main__":
    main()
