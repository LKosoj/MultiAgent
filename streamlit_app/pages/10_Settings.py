"""
Страница настроек системы
========================
"""

import streamlit as st
import sys
from pathlib import Path
import json
from datetime import datetime
import os

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

def main():
    st.set_page_config(
        page_title="Settings - MultiAgent System",
        page_icon="⚙️",
        layout="wide"
    )
    
    st.title("⚙️ Настройки системы")
    st.markdown("---")
    
    # Главные вкладки
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "🤖 LLM", "📊 Телеметрия", "🔒 Безопасность", 
        "💾 Память", "🔧 Система"
    ])
    
    with tab1:
        show_llm_settings()
    
    with tab2:
        show_telemetry_settings()
    
    with tab3:
        show_security_settings()
    
    with tab4:
        show_memory_settings()
    
    with tab5:
        show_system_settings()

def show_llm_settings():
    """Настройки LLM провайдеров"""
    
    st.markdown("## 🤖 Настройки LLM")
    
    try:
        from configuration_api import get_configuration_manager
        
        config_manager = get_configuration_manager()
        config = config_manager.get_config()
        
        # Основные настройки LLM
        st.markdown("### 🔧 Основная конфигурация")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Провайдер
            llm_providers = config_manager.get_llm_providers()
            
            current_provider = config.llm.provider
            selected_provider = st.selectbox(
                "🏢 LLM Провайдер",
                options=list(llm_providers.keys()),
                index=list(llm_providers.keys()).index(current_provider) if current_provider in llm_providers else 0,
                help="Выберите провайдера языковой модели"
            )
            
            # Модель
            provider_info = llm_providers.get(selected_provider, {})
            available_models = provider_info.get("models", ["gpt-4", "gpt-3.5-turbo"])
            model_details = provider_info.get("model_details", {})
            
            # Создаем варианты для отображения с описаниями
            if model_details:
                # Если есть детали модели (логические имена), показываем с описаниями
                model_options = []
                model_labels = []
                
                for model_name in available_models:
                    if model_name in model_details:
                        detail = model_details[model_name]
                        label = f"{model_name} - {detail.get('description', 'Нет описания')}"
                        model_options.append(model_name)
                        model_labels.append(label)
                    else:
                        model_options.append(model_name)
                        model_labels.append(model_name)
                
                current_model = config.llm.model
                current_index = model_options.index(current_model) if current_model in model_options else 0
                
                selected_model_label = st.selectbox(
                    "🧠 Модель",
                    options=model_labels,
                    index=current_index,
                    help="Выберите логическую модель из системы"
                )
                
                # Извлекаем логическое имя из выбранной метки
                selected_model = model_options[model_labels.index(selected_model_label)]
                
                # Показываем дополнительную информацию о выбранной модели
                if selected_model in model_details:
                    detail = model_details[selected_model]
                    
                    with st.expander(f"ℹ️ Информация о модели {selected_model}", expanded=False):
                        col_info1, col_info2 = st.columns(2)
                        
                        with col_info1:
                            st.markdown(f"**📝 Описание:** {detail.get('description', 'Нет описания')}")
                            st.markdown(f"**🎯 Назначение:** {detail.get('use_case', 'Универсальное')}")
                            st.markdown(f"**⚡ Характеристики:** {detail.get('characteristics', 'Стандартные')}")
                        
                        with col_info2:
                            st.markdown(f"**🔗 Реальная модель:** `{detail.get('real_model_id', 'Неизвестно')}`")
                            st.markdown(f"**🌡️ Temperature:** {detail.get('temperature', 0.7)}")
                            st.markdown(f"**📏 Max tokens:** {detail.get('max_tokens', 'Не указано')}")
            else:
                # Стандартный режим для внешних провайдеров
                current_model = config.llm.model
                selected_model = st.selectbox(
                    "🧠 Модель",
                    options=available_models,
                    index=available_models.index(current_model) if current_model in available_models else 0,
                    help="Выберите конкретную модель"
                )
        
        with col2:
            # Проверяем, использует ли провайдер системные подключения
            provider_info = llm_providers.get(selected_provider, {})
            uses_system_connections = provider_info.get("uses_system_connections", False)
            
            if uses_system_connections:
                # Для системных моделей показываем информацию о подключении
                st.info("🔧 **Системные подключения**")
                st.markdown("Модели используют подключения из `agent_command.py`:")
                st.code(f"• API Base: os.getenv('OPENAI_API_BASE_DB')\n• API Key: os.getenv('OPENAI_API_KEY_DB')")
                
                connection_source = provider_info.get("connection_source", "agent_command.py")
                st.markdown(f"📍 **Источник**: {connection_source}")
                
                # Скрываем настройки API ключа
                new_api_key = ""
                base_url = ""
                
                st.markdown("---")
                st.markdown("⚠️ **Для изменения подключений** отредактируйте переменные окружения:")
                st.markdown("- `OPENAI_API_BASE_DB`")
                st.markdown("- `OPENAI_API_KEY_DB`")
                
            else:
                # Для внешних провайдеров показываем настройки
                api_key_placeholder = "sk-..." if selected_provider == "openai" else "API ключ"
                
                current_api_key = config.llm.api_key
                api_key_display = current_api_key[:10] + "..." if current_api_key and len(current_api_key) > 10 else ""
                
                new_api_key = st.text_input(
                    "🔑 API Ключ",
                    value=api_key_display,
                    type="password",
                    placeholder=api_key_placeholder,
                    help="API ключ для доступа к модели"
                )
                
                # Базовый URL (для кастомных провайдеров)
                base_url = st.text_input(
                    "🌐 Базовый URL (опционально)",
                    value=config.llm.base_url or "",
                    placeholder="https://api.openai.com/v1",
                    help="Кастомный URL для API (для локальных моделей)"
            )
        
        # Параметры модели
        st.markdown("### ⚙️ Параметры модели")
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            temperature = st.slider(
                "🌡️ Temperature",
                min_value=0.0,
                max_value=2.0,
                value=config.llm.temperature,
                step=0.1,
                help="Контролирует случайность ответов (0 = детерминированно, 2 = очень случайно)"
            )
            
            max_tokens = st.number_input(
                "📏 Максимум токенов",
                min_value=1,
                max_value=32000,
                value=config.llm.max_tokens,
                help="Максимальное количество токенов в ответе"
            )
        
        with col2:
            top_p = st.slider(
                "🎯 Top P",
                min_value=0.0,
                max_value=1.0,
                value=config.llm.top_p,
                step=0.05,
                help="Ядерная выборка (альтернатива temperature)"
            )
            
            frequency_penalty = st.slider(
                "🔄 Frequency Penalty",
                min_value=0.0,
                max_value=2.0,
                value=config.llm.frequency_penalty,
                step=0.1,
                help="Штраф за повторяющиеся слова"
            )
        
        with col3:
            presence_penalty = st.slider(
                "✨ Presence Penalty",
                min_value=0.0,
                max_value=2.0,
                value=config.llm.presence_penalty,
                step=0.1,
                help="Штраф за повторение тем"
            )
            
            timeout_seconds = st.number_input(
                "⏱️ Таймаут (секунды)",
                min_value=1,
                max_value=300,
                value=config.llm.timeout_seconds,
                help="Максимальное время ожидания ответа"
            )
        
        # Тест соединения
        st.markdown("### 🧪 Тестирование")
        
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("🧪 Тест соединения", type="primary"):
                test_llm_connection(config_manager, selected_provider, selected_model, new_api_key, base_url)
        
        with col2:
            if st.button("💾 Сохранить настройки"):
                save_llm_settings(
                    config_manager, config, selected_provider, selected_model,
                    new_api_key, base_url, temperature, max_tokens, top_p,
                    frequency_penalty, presence_penalty, timeout_seconds
                )
        
        # Информация о провайдерах
        show_provider_info(llm_providers)
    
    except Exception as e:
        st.error(f"❌ Ошибка загрузки настроек LLM: {e}")

def test_llm_connection(config_manager, provider, model, api_key, base_url):
    """Тестирование соединения с LLM"""
    
    with st.spinner("Тестирование соединения с LLM..."):
        try:
            # Обновляем API ключ если он изменился
            temp_config = {
                "provider": provider,
                "model": model,
                "api_key": api_key if api_key and not api_key.endswith("...") else None,
                "base_url": base_url if base_url else None
            }
            
            result = config_manager.test_llm_connection(provider, model, temp_config)
            
            if result["success"]:
                st.success(f"✅ Соединение успешно! Время ответа: {result['response_time_ms']}ms")
                
                if "test_response" in result:
                    with st.expander("📝 Тестовый ответ"):
                        st.markdown(result["test_response"])
            else:
                st.error(f"❌ Ошибка соединения: {result['error_message']}")
                
                if "suggestions" in result:
                    st.markdown("**💡 Предложения:**")
                    for suggestion in result["suggestions"]:
                        st.info(f"• {suggestion}")
        
        except Exception as e:
            st.error(f"❌ Ошибка тестирования: {e}")

def save_llm_settings(config_manager, config, provider, model, api_key, base_url,
                     temperature, max_tokens, top_p, frequency_penalty, presence_penalty, timeout_seconds):
    """Сохранение настроек LLM"""
    
    try:
        # Обновляем конфигурацию
        new_config = config
        new_config.llm.provider = provider
        new_config.llm.model = model
        
        # Обновляем API ключ только если он изменился
        if api_key and not api_key.endswith("..."):
            new_config.llm.api_key = api_key
        
        new_config.llm.base_url = base_url if base_url else None
        new_config.llm.temperature = temperature
        new_config.llm.max_tokens = max_tokens
        new_config.llm.top_p = top_p
        new_config.llm.frequency_penalty = frequency_penalty
        new_config.llm.presence_penalty = presence_penalty
        new_config.llm.timeout_seconds = timeout_seconds
        
        # Используем правильный метод для обновления LLM конфигурации
        from configuration_api import LLMConfig
        
        llm_config = LLMConfig()
        llm_config.provider = new_config.llm.provider
        llm_config.model = new_config.llm.model
        llm_config.api_key = new_config.llm.api_key
        llm_config.base_url = new_config.llm.base_url
        llm_config.temperature = new_config.llm.temperature
        llm_config.max_tokens = new_config.llm.max_tokens
        llm_config.top_p = new_config.llm.top_p
        llm_config.frequency_penalty = new_config.llm.frequency_penalty
        llm_config.presence_penalty = new_config.llm.presence_penalty
        llm_config.timeout_seconds = new_config.llm.timeout_seconds
        
        config_manager.update_llm_config(llm_config)
        st.success("✅ Настройки LLM сохранены")
        
        # Перезагружаем страницу для отображения обновленных настроек
        st.rerun()
    
    except Exception as e:
        st.error(f"❌ Ошибка сохранения настроек: {e}")

def show_provider_info(providers):
    """Информация о провайдерах"""
    
    st.markdown("### 📋 Информация о провайдерах")
    
    for provider_name, provider_info in providers.items():
        with st.expander(f"🏢 {provider_name}", expanded=False):
            
            col1, col2 = st.columns(2)
            
            with col1:
                st.markdown(f"**📝 Описание:** {provider_info.get('description', 'Нет описания')}")
                st.markdown(f"**🔗 Сайт:** {provider_info.get('website', 'Не указан')}")
                
                if provider_info.get("requires_api_key", True):
                    st.info("🔑 Требует API ключ")
                else:
                    st.success("🆓 Не требует API ключ")
            
            with col2:
                st.markdown("**🧠 Доступные модели:**")
                for model in provider_info.get("models", []):
                    st.markdown(f"- {model}")
                
                # Особенности провайдера
                features = provider_info.get("features", [])
                if features:
                    st.markdown("**⭐ Особенности:**")
                    for feature in features:
                        st.markdown(f"- {feature}")

def show_telemetry_settings():
    """Настройки телеметрии"""
    
    st.markdown("## 📊 Настройки телеметрии")
    
    try:
        from configuration_api import get_configuration_manager
        from telemetry import get_telemetry_manager
        
        config_manager = get_configuration_manager()
        config = config_manager.get_config()
        telemetry_manager = get_telemetry_manager()
        
        # Основные настройки
        st.markdown("### ⚙️ Основные настройки")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Включение/выключение
            current_enabled = telemetry_manager.is_enabled()
            
            telemetry_enabled = st.checkbox(
                "📊 Включить телеметрию",
                value=current_enabled,
                help="Включить сбор телеметрии OpenTelemetry"
            )
            
            if telemetry_enabled != current_enabled:
                if telemetry_enabled:
                    telemetry_manager.enable()
                    st.success("✅ Телеметрия включена")
                else:
                    telemetry_manager.disable()
                    st.success("✅ Телеметрия отключена")
                
                st.rerun()
            
            # Уровень детализации
            detail_levels = ["minimal", "standard", "verbose"]
            detail_level = st.selectbox(
                "🔍 Уровень детализации",
                detail_levels,
                index=detail_levels.index(config.telemetry.detail_level) if config.telemetry.detail_level in detail_levels else 1,
                help="Количество собираемой информации"
            )
        
        with col2:
            # Настройки хранения
            trace_retention_days = st.number_input(
                "📅 Хранить трассы (дней)",
                min_value=1,
                max_value=365,
                value=config.telemetry.trace_retention_days,
                help="Количество дней хранения файлов трасс"
            )
            
            max_trace_file_size_mb = st.number_input(
                "📁 Макс. размер файла (MB)",
                min_value=1.0,
                max_value=100.0,
                value=config.telemetry.max_trace_file_size_mb,
                help="Максимальный размер одного файла трассы"
            )
        
        # Дополнительные настройки сбора
        st.markdown("### 📋 Настройки сбора данных")
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            collect_detailed_spans = st.checkbox(
                "🔍 Детальные спаны",
                value=config.telemetry.collect_detailed_spans,
                help="Собирать подробную информацию о каждом шаге"
            )
            
            collect_system_metrics = st.checkbox(
                "💻 Системные метрики",
                value=config.telemetry.collect_system_metrics,
                help="Собирать метрики использования системы"
            )
        
        with col2:
            collect_memory_metrics = st.checkbox(
                "🧠 Метрики памяти",
                value=config.telemetry.collect_memory_metrics,
                help="Собирать информацию об использовании памяти"
            )
            
            collect_performance_metrics = st.checkbox(
                "⚡ Метрики производительности",
                value=config.telemetry.collect_performance_metrics,
                help="Собирать метрики времени выполнения"
            )
        
        with col3:
            collect_error_details = st.checkbox(
                "❌ Детали ошибок",
                value=config.telemetry.collect_error_details,
                help="Собирать подробную информацию об ошибках"
            )
            
            collect_user_interactions = st.checkbox(
                "👤 Взаимодействия пользователя",
                value=config.telemetry.collect_user_interactions,
                help="Собирать информацию о действиях пользователя"
            )
        
        # Настройки экспорта
        st.markdown("### 📤 Настройки экспорта")
        
        col1, col2 = st.columns(2)
        
        with col1:
            export_format = st.selectbox(
                "📋 Формат экспорта",
                ["jsonl", "json", "otlp"],
                index=["jsonl", "json", "otlp"].index(config.telemetry.export_format),
                help="Формат файлов трасс"
            )
            
            batch_size = st.number_input(
                "📦 Размер пакета",
                min_value=1,
                max_value=1000,
                value=config.telemetry.batch_size,
                help="Количество спанов в одном пакете"
            )
        
        with col2:
            flush_interval_seconds = st.number_input(
                "⏱️ Интервал сброса (секунды)",
                min_value=1,
                max_value=300,
                value=config.telemetry.flush_interval_seconds,
                help="Как часто сбрасывать данные на диск"
            )
            
            compression_enabled = st.checkbox(
                "🗜️ Сжатие файлов",
                value=config.telemetry.compression_enabled,
                help="Сжимать файлы трасс для экономии места"
            )
        
        # Кнопки действий
        col1, col2, col3 = st.columns(3)
        
        with col1:
            if st.button("💾 Сохранить настройки телеметрии"):
                save_telemetry_settings(
                    config_manager, config, detail_level, trace_retention_days,
                    max_trace_file_size_mb, collect_detailed_spans, collect_system_metrics,
                    collect_memory_metrics, collect_performance_metrics, collect_error_details,
                    collect_user_interactions, export_format, batch_size,
                    flush_interval_seconds, compression_enabled
                )
        
        with col2:
            if st.button("🧹 Очистить старые трассы"):
                cleanup_old_traces(telemetry_manager, trace_retention_days)
        
        with col3:
            if st.button("📊 Статистика хранилища"):
                show_telemetry_storage_stats()
    
    except Exception as e:
        st.error(f"❌ Ошибка настроек телеметрии: {e}")

def save_telemetry_settings(config_manager, config, detail_level, retention_days, max_file_size,
                           detailed_spans, system_metrics, memory_metrics, performance_metrics,
                           error_details, user_interactions, export_format, batch_size,
                           flush_interval, compression):
    """Сохранение настроек телеметрии"""
    
    try:
        new_config = config
        new_config.telemetry.detail_level = detail_level
        new_config.telemetry.trace_retention_days = retention_days
        new_config.telemetry.max_trace_file_size_mb = max_file_size
        new_config.telemetry.collect_detailed_spans = detailed_spans
        new_config.telemetry.collect_system_metrics = system_metrics
        new_config.telemetry.collect_memory_metrics = memory_metrics
        new_config.telemetry.collect_performance_metrics = performance_metrics
        new_config.telemetry.collect_error_details = error_details
        new_config.telemetry.collect_user_interactions = user_interactions
        new_config.telemetry.export_format = export_format
        new_config.telemetry.batch_size = batch_size
        new_config.telemetry.flush_interval_seconds = flush_interval
        new_config.telemetry.compression_enabled = compression
        
        config_manager.update_config(new_config)
        st.success("✅ Настройки телеметрии сохранены")
    
    except Exception as e:
        st.error(f"❌ Ошибка сохранения: {e}")

def cleanup_old_traces(telemetry_manager, retention_days):
    """Очистка старых трасс"""
    
    with st.spinner("Очистка старых трасс..."):
        try:
            removed_count = telemetry_manager.cleanup_old_traces(max_age_days=retention_days)
            st.success(f"✅ Удалено {removed_count} старых файлов трасс")
        except Exception as e:
            st.error(f"❌ Ошибка очистки: {e}")

def show_telemetry_storage_stats():
    """Статистика хранилища телеметрии"""
    
    try:
        logs_dir = Path(project_root) / "logs" / "traces"
        
        if logs_dir.exists():
            trace_files = list(logs_dir.glob("*.jsonl"))
            total_size = sum(f.stat().st_size for f in trace_files) / (1024 * 1024)
            
            st.markdown("### 📊 Статистика хранилища")
            
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric("📁 Файлов", len(trace_files))
            
            with col2:
                st.metric("💾 Размер", f"{total_size:.1f} MB")
            
            with col3:
                avg_size = total_size / len(trace_files) if trace_files else 0
                st.metric("📊 Средний размер", f"{avg_size:.2f} MB")
        else:
            st.info("📁 Директория трасс не существует")
    
    except Exception as e:
        st.error(f"❌ Ошибка получения статистики: {e}")

def show_security_settings():
    """Настройки безопасности"""
    
    st.markdown("## 🔒 Настройки безопасности")
    
    try:
        from configuration_api import get_configuration_manager
        
        config_manager = get_configuration_manager()
        config = config_manager.get_config()
        
        # Основные настройки безопасности
        st.markdown("### 🛡️ Основные настройки")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Выполнение SQL
            sql_execution_enabled = st.checkbox(
                "🗄️ Разрешить выполнение SQL",
                value=config.security.sql_execution_enabled,
                help="Разрешить агентам выполнять SQL запросы"
            )
            
            if sql_execution_enabled:
                st.warning("⚠️ Включение выполнения SQL может быть небезопасно")
            
            # Уровень безопасности
            safety_level = st.selectbox(
                "🔒 Уровень безопасности",
                ["strict", "moderate", "permissive"],
                index=["strict", "moderate", "permissive"].index(config.security.safety_level),
                help="Общий уровень проверок безопасности"
            )
            
            # Максимальные лимиты
            max_sql_rows = st.number_input(
                "📊 Максимум строк SQL",
                min_value=1,
                max_value=100000,
                value=config.security.max_sql_rows,
                help="Максимальное количество строк в SQL результате"
            )
        
        with col2:
            # Таймауты
            query_timeout_seconds = st.number_input(
                "⏱️ Таймаут запросов (секунды)",
                min_value=1,
                max_value=300,
                value=config.security.query_timeout_seconds,
                help="Максимальное время выполнения SQL запроса"
            )
            
            # Разрешенные операции
            st.markdown("**✅ Разрешенные SQL операции:**")
            
            allowed_operations = config.security.allowed_sql_operations
            
            allow_select = st.checkbox(
                "SELECT",
                value="SELECT" in allowed_operations,
                help="Разрешить SELECT запросы"
            )
            
            allow_insert = st.checkbox(
                "INSERT",
                value="INSERT" in allowed_operations,
                help="Разрешить INSERT запросы (опасно!)"
            )
            
            allow_update = st.checkbox(
                "UPDATE",
                value="UPDATE" in allowed_operations,
                help="Разрешить UPDATE запросы (опасно!)"
            )
            
            allow_delete = st.checkbox(
                "DELETE",
                value="DELETE" in allowed_operations,
                help="Разрешить DELETE запросы (очень опасно!)"
            )
            
            if allow_insert or allow_update or allow_delete:
                st.error("🚨 Разрешение изменяющих операций крайне опасно!")
        
        # Дополнительные ограничения
        st.markdown("### 🚫 Дополнительные ограничения")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Блокируемые ключевые слова
            blocked_keywords = st.text_area(
                "🚫 Блокируемые ключевые слова",
                value="\n".join(config.security.blocked_sql_keywords),
                height=100,
                help="По одному ключевому слову на строку"
            )
            
            # Разрешенные схемы
            allowed_schemas = st.text_area(
                "✅ Разрешенные схемы БД",
                value="\n".join(config.security.allowed_schemas) if config.security.allowed_schemas else "",
                height=100,
                help="Список разрешенных схем (пусто = все разрешены)"
            )
        
        with col2:
            # Настройки PII
            enable_pii_detection = st.checkbox(
                "🔍 Обнаружение PII",
                value=config.security.enable_pii_detection,
                help="Автоматическое обнаружение персональных данных"
            )
            
            pii_action = st.selectbox(
                "🎭 Действие при обнаружении PII",
                ["block", "mask", "warn"],
                index=["block", "mask", "warn"].index(config.security.pii_action),
                help="Что делать при обнаружении персональных данных"
            )
            
            # Логирование безопасности
            log_security_events = st.checkbox(
                "📝 Логировать события безопасности",
                value=config.security.log_security_events,
                help="Записывать все события безопасности в лог"
            )
            
            audit_all_queries = st.checkbox(
                "🔍 Аудит всех запросов",
                value=config.security.audit_all_queries,
                help="Сохранять все SQL запросы для аудита"
            )
        
        # Whitelist/Blacklist
        st.markdown("### 📋 Списки доступа")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("**✅ Whitelist таблиц:**")
            table_whitelist = st.text_area(
                "Разрешенные таблицы",
                value="\n".join(config.security.table_whitelist) if config.security.table_whitelist else "",
                height=100,
                help="Список разрешенных таблиц (пусто = все разрешены)",
                key="table_whitelist"
            )
        
        with col2:
            st.markdown("**🚫 Blacklist таблиц:**")
            table_blacklist = st.text_area(
                "Запрещенные таблицы",
                value="\n".join(config.security.table_blacklist) if config.security.table_blacklist else "",
                height=100,
                help="Список запрещенных таблиц",
                key="table_blacklist"
            )
        
        # Сохранение настроек
        if st.button("💾 Сохранить настройки безопасности", type="primary"):
            save_security_settings(
                config_manager, config, sql_execution_enabled, safety_level,
                max_sql_rows, query_timeout_seconds, allow_select, allow_insert,
                allow_update, allow_delete, blocked_keywords, allowed_schemas,
                enable_pii_detection, pii_action, log_security_events,
                audit_all_queries, table_whitelist, table_blacklist
            )
        
        # Предупреждения безопасности
        show_security_warnings(config.security)
    
    except Exception as e:
        st.error(f"❌ Ошибка настроек безопасности: {e}")

def save_security_settings(config_manager, config, sql_enabled, safety_level, max_rows,
                          timeout, select, insert, update, delete, blocked_kw, allowed_schemas,
                          pii_detection, pii_action, log_events, audit_queries,
                          whitelist, blacklist):
    """Сохранение настроек безопасности"""
    
    try:
        new_config = config
        new_config.security.sql_execution_enabled = sql_enabled
        new_config.security.safety_level = safety_level
        new_config.security.max_sql_rows = max_rows
        new_config.security.query_timeout_seconds = timeout
        
        # Разрешенные операции
        operations = []
        if select:
            operations.append("SELECT")
        if insert:
            operations.append("INSERT")
        if update:
            operations.append("UPDATE")
        if delete:
            operations.append("DELETE")
        
        new_config.security.allowed_sql_operations = operations
        
        # Списки
        new_config.security.blocked_sql_keywords = [kw.strip() for kw in blocked_kw.split('\n') if kw.strip()]
        new_config.security.allowed_schemas = [s.strip() for s in allowed_schemas.split('\n') if s.strip()] if allowed_schemas else None
        new_config.security.table_whitelist = [t.strip() for t in whitelist.split('\n') if t.strip()] if whitelist else None
        new_config.security.table_blacklist = [t.strip() for t in blacklist.split('\n') if t.strip()] if blacklist else None
        
        # PII и аудит
        new_config.security.enable_pii_detection = pii_detection
        new_config.security.pii_action = pii_action
        new_config.security.log_security_events = log_events
        new_config.security.audit_all_queries = audit_queries
        
        config_manager.update_config(new_config)
        st.success("✅ Настройки безопасности сохранены")
        
        st.rerun()
    
    except Exception as e:
        st.error(f"❌ Ошибка сохранения: {e}")

def show_security_warnings(security_config):
    """Отображение предупреждений безопасности"""
    
    st.markdown("### ⚠️ Анализ безопасности")
    
    warnings = []
    
    # Проверяем различные настройки
    if security_config.sql_execution_enabled:
        warnings.append("🚨 Выполнение SQL включено")
    
    if "INSERT" in security_config.allowed_sql_operations:
        warnings.append("🚨 Разрешены INSERT операции")
    
    if "UPDATE" in security_config.allowed_sql_operations:
        warnings.append("🚨 Разрешены UPDATE операции")
    
    if "DELETE" in security_config.allowed_sql_operations:
        warnings.append("🚨 Разрешены DELETE операции")
    
    if security_config.safety_level == "permissive":
        warnings.append("⚠️ Уровень безопасности: Permissive")
    
    if security_config.max_sql_rows > 10000:
        warnings.append("⚠️ Высокий лимит строк SQL")
    
    if not security_config.enable_pii_detection:
        warnings.append("⚠️ Обнаружение PII отключено")
    
    if not security_config.log_security_events:
        warnings.append("ℹ️ Логирование событий безопасности отключено")
    
    # Отображаем предупреждения
    if warnings:
        for warning in warnings:
            if warning.startswith("🚨"):
                st.error(warning)
            elif warning.startswith("⚠️"):
                st.warning(warning)
            else:
                st.info(warning)
    else:
        st.success("✅ Конфигурация безопасности выглядит хорошо")
    
    # Рекомендации
    st.markdown("**💡 Рекомендации для безопасности:**")
    recommendations = [
        "Используйте только SELECT операции для Text-to-SQL",
        "Установите строгий уровень безопасности (strict)",
        "Включите обнаружение PII",
        "Ведите лог всех событий безопасности",
        "Регулярно проверяйте аудит-логи",
        "Ограничьте доступ к чувствительным таблицам"
    ]
    
    for rec in recommendations:
        st.markdown(f"• {rec}")

def show_memory_settings():
    """Настройки памяти"""
    
    st.markdown("## 💾 Настройки памяти")
    
    try:
        from configuration_api import get_configuration_manager
        from memory.streamlit_api import get_memory_rag_manager
        
        config_manager = get_configuration_manager()
        config = config_manager.get_config()
        memory_manager = get_memory_rag_manager()
        
        # Основные настройки памяти
        st.markdown("### 🧠 Основные настройки")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Включение памяти
            memory_enabled = st.checkbox(
                "🧠 Включить память агентов",
                value=config.memory.enabled,
                help="Включить систему памяти для агентов"
            )
            
            # Тип памяти
            memory_type = st.selectbox(
                "📋 Тип памяти",
                ["chromadb", "sqlite"],
                index=["chromadb", "sqlite"].index(config.memory.memory_type) if config.memory.memory_type in ["chromadb", "sqlite"] else 0,
                help="Тип системы памяти (chromadb - с векторным поиском, sqlite - только SQL)"
            )
            
            # Максимальные лимиты
            max_tactical_memories = st.number_input(
                "🎯 Макс. тактических воспоминаний",
                min_value=10,
                max_value=100000,
                value=config.memory.max_tactical_memories,
                help="Максимальное количество тактических воспоминаний на агента"
            )
        
        with col2:
            max_strategic_memories = st.number_input(
                "🗺️ Макс. стратегических воспоминаний",
                min_value=1,
                max_value=10000,
                value=config.memory.max_strategic_memories,
                help="Максимальное количество стратегических воспоминаний на агента"
            )
            
            # Время жизни
            tactical_ttl_hours = st.number_input(
                "⏱️ TTL тактических (часы)",
                min_value=1,
                max_value=8760,  # 1 год
                value=config.memory.tactical_memory_ttl_hours,
                help="Время жизни тактических воспоминаний"
            )
            
            strategic_ttl_days = st.number_input(
                "📅 TTL стратегических (дни)",
                min_value=1,
                max_value=365,
                value=config.memory.strategic_memory_ttl_days,
                help="Время жизни стратегических воспоминаний"
            )
        
        # Настройки embeddings
        st.markdown("### 🤖 Настройки Embeddings")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Модель embeddings
            embedding_models = [
                "intfloat/multilingual-e5-base",
                "sentence-transformers/all-MiniLM-L6-v2",
                "sentence-transformers/all-mpnet-base-v2",
                "text-embedding-ada-002",
                "custom"
            ]
            
            current_model = config.memory.embedding_model
            embedding_model = st.selectbox(
                "🧠 Модель embeddings",
                embedding_models,
                index=embedding_models.index(current_model) if current_model in embedding_models else 0,
                help="Модель для генерации векторных представлений"
            )
            
            if embedding_model == "custom":
                custom_model = st.text_input(
                    "🔧 Кастомная модель",
                    value=config.memory.custom_embedding_model or "",
                    help="Путь или название кастомной модели"
                )
            
            # Размерность векторов
            embedding_dimensions = st.number_input(
                "📏 Размерность векторов",
                min_value=64,
                max_value=4096,
                value=config.memory.embedding_dimensions,
                help="Размерность векторных представлений"
            )
        
        with col2:
            # Настройки поиска
            default_search_k = st.number_input(
                "🔍 K для поиска по умолчанию",
                min_value=1,
                max_value=100,
                value=config.memory.default_search_k,
                help="Количество результатов поиска по умолчанию"
            )
            
            similarity_threshold = st.slider(
                "🎯 Порог схожести",
                min_value=0.0,
                max_value=1.0,
                value=config.memory.similarity_threshold,
                step=0.05,
                help="Минимальная схожесть для включения в результаты"
            )
            
            # Настройки индексации
            reindex_interval_hours = st.number_input(
                "🔄 Интервал переиндексации (часы)",
                min_value=1,
                max_value=168,  # 1 неделя
                value=config.memory.reindex_interval_hours,
                help="Как часто переиндексировать векторную базу"
            )
        
        # Настройки ChromaDB
        st.markdown("### 🔍 Настройки ChromaDB")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Путь к ChromaDB
            chromadb_path = st.text_input(
                "📁 Путь к ChromaDB",
                value=config.memory.chromadb_path or "./memory/chromadb",
                help="Путь к директории ChromaDB"
            )
            
            # Настройки коллекций
            collection_prefix = st.text_input(
                "🏷️ Префикс коллекций",
                value=config.memory.collection_prefix or "multiagent",
                help="Префикс для названий коллекций"
            )
        
        with col2:
            # Размер батча
            batch_size = st.number_input(
                "📦 Размер батча",
                min_value=1,
                max_value=1000,
                value=config.memory.batch_size,
                help="Размер батча для операций с векторной БД"
            )
            
            # Сжатие
            enable_compression = st.checkbox(
                "🗜️ Включить сжатие",
                value=config.memory.enable_compression,
                help="Сжимать векторные данные"
            )
        
        # Кнопки управления
        col1, col2, col3 = st.columns(3)
        
        with col1:
            if st.button("💾 Сохранить настройки памяти"):
                save_memory_settings(
                    config_manager, config, memory_enabled, memory_type,
                    max_tactical_memories, max_strategic_memories, tactical_ttl_hours,
                    strategic_ttl_days, embedding_model, custom_model if embedding_model == "custom" else None,
                    embedding_dimensions, default_search_k, similarity_threshold,
                    reindex_interval_hours, chromadb_path, collection_prefix,
                    batch_size, enable_compression
                )
        
        with col2:
            if st.button("🔄 Перестроить индексы"):
                rebuild_memory_indexes(memory_manager)
        
        with col3:
            if st.button("📊 Статистика памяти"):
                show_memory_statistics(memory_manager)
    
    except Exception as e:
        st.error(f"❌ Ошибка настроек памяти: {e}")

def save_memory_settings(config_manager, config, enabled, mem_type, max_tactical, max_strategic,
                        tactical_ttl, strategic_ttl, embedding_model, custom_model,
                        dimensions, search_k, threshold, reindex_interval, chromadb_path,
                        collection_prefix, batch_size, compression):
    """Сохранение настроек памяти"""
    
    try:
        new_config = config
        new_config.memory.enabled = enabled
        new_config.memory.memory_type = mem_type
        new_config.memory.max_tactical_memories = max_tactical
        new_config.memory.max_strategic_memories = max_strategic
        new_config.memory.tactical_memory_ttl_hours = tactical_ttl
        new_config.memory.strategic_memory_ttl_days = strategic_ttl
        new_config.memory.embedding_model = embedding_model
        new_config.memory.custom_embedding_model = custom_model
        new_config.memory.embedding_dimensions = dimensions
        new_config.memory.default_search_k = search_k
        new_config.memory.similarity_threshold = threshold
        new_config.memory.reindex_interval_hours = reindex_interval
        new_config.memory.chromadb_path = chromadb_path
        new_config.memory.collection_prefix = collection_prefix
        new_config.memory.batch_size = batch_size
        new_config.memory.enable_compression = compression
        
        config_manager.update_config(new_config)
        st.success("✅ Настройки памяти сохранены")
    
    except Exception as e:
        st.error(f"❌ Ошибка сохранения: {e}")

def rebuild_memory_indexes(memory_manager):
    """Перестройка индексов памяти"""
    
    with st.spinner("Перестройка индексов памяти..."):
        try:
            result = memory_manager.rebuild_memory()
            
            if result.success:
                st.success(f"✅ Индексы перестроены за {result.rebuild_time_ms:.1f}ms")
                st.info(f"Восстановлено: {result.tactical_count} тактических, {result.strategic_count} стратегических")
            else:
                st.error(f"❌ Ошибка перестройки: {result.error_message}")
        
        except Exception as e:
            st.error(f"❌ Ошибка перестройки: {e}")

def show_memory_statistics(memory_manager):
    """Статистика памяти"""
    
    try:
        status = memory_manager.get_memory_status()
        
        st.markdown("### 📊 Статистика памяти")
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric("🎯 Тактических", status.tactical_memories_count)
            st.metric("🗺️ Стратегических", status.strategic_memories_count)
        
        with col2:
            if status.database_size_mb:
                st.metric("💾 Размер БД", f"{status.database_size_mb:.1f} MB")
            
            total_vectors = sum(
                coll.get("count", 0) 
                for coll in status.collections_info.values()
            ) if status.collections_info else 0
            st.metric("🔍 Векторов", total_vectors)
        
        with col3:
            if status.chromadb_available:
                st.success("✅ ChromaDB работает")
            else:
                st.error("❌ ChromaDB недоступна")
            
            if status.embedding_model_available:
                st.success("✅ Embeddings работают")
            else:
                st.error("❌ Embeddings недоступны")
    
    except Exception as e:
        st.error(f"❌ Ошибка получения статистики: {e}")

def show_system_settings():
    """Системные настройки"""
    
    st.markdown("## 🔧 Системные настройки")
    
    try:
        from configuration_api import get_configuration_manager
        
        config_manager = get_configuration_manager()
        config = config_manager.get_config()
        
        # Общие настройки
        st.markdown("### ⚙️ Общие настройки")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Уровень логирования
            log_level = st.selectbox(
                "📝 Уровень логирования",
                ["DEBUG", "INFO", "WARNING", "ERROR"],
                index=["DEBUG", "INFO", "WARNING", "ERROR"].index(config.logging.level),
                help="Уровень детализации логов"
            )
            
            # Формат логов
            log_format = st.selectbox(
                "📋 Формат логов",
                ["detailed", "simple", "json"],
                index=["detailed", "simple", "json"].index(config.logging.format),
                help="Формат записи логов"
            )
            
            # Ротация логов
            log_rotation_mb = st.number_input(
                "🔄 Ротация логов (MB)",
                min_value=1,
                max_value=1000,
                value=config.logging.rotation_size_mb,
                help="Размер лог-файла для ротации"
            )
        
        with col2:
            # Рабочая директория
            work_directory = st.text_input(
                "📁 Рабочая директория",
                value=config.system.work_directory or str(project_root),
                help="Базовая рабочая директория системы"
            )
            
            # Временная директория
            temp_directory = st.text_input(
                "🗂️ Временная директория",
                value=config.system.temp_directory or "/tmp",
                help="Директория для временных файлов"
            )
            
            # Язык системы
            system_language = st.selectbox(
                "🌐 Язык системы",
                ["ru", "en", "auto"],
                index=["ru", "en", "auto"].index(config.system.language),
                help="Язык интерфейса и сообщений"
            )
        
        # Лимиты ресурсов
        st.markdown("### 📊 Лимиты ресурсов")
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            max_concurrent_workflows = st.number_input(
                "🔄 Макс. workflows",
                min_value=1,
                max_value=100,
                value=config.resource_limits.max_concurrent_workflows,
                help="Максимальное количество одновременных workflows"
            )
            
            max_concurrent_agents = st.number_input(
                "🤖 Макс. агентов",
                min_value=1,
                max_value=100,
                value=config.resource_limits.max_concurrent_agents,
                help="Максимальное количество одновременных агентов"
            )
        
        with col2:
            memory_limit_mb = st.number_input(
                "🧠 Лимит памяти (MB)",
                min_value=128,
                max_value=32768,
                value=config.resource_limits.memory_limit_mb,
                help="Лимит использования оперативной памяти"
            )
            
            disk_space_limit_gb = st.number_input(
                "💾 Лимит диска (GB)",
                min_value=1,
                max_value=1000,
                value=config.resource_limits.disk_space_limit_gb,
                help="Лимит использования дискового пространства"
            )
        
        with col3:
            execution_timeout_minutes = st.number_input(
                "⏱️ Таймаут выполнения (мин)",
                min_value=1,
                max_value=1440,  # 24 часа
                value=config.resource_limits.execution_timeout_minutes,
                help="Максимальное время выполнения задач"
            )
            
            cleanup_interval_hours = st.number_input(
                "🧹 Интервал очистки (часы)",
                min_value=1,
                max_value=168,  # 1 неделя
                value=config.system.cleanup_interval_hours,
                help="Как часто запускать очистку временных файлов"
            )
        
        # Производительность
        st.markdown("### ⚡ Настройки производительности")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Количество потоков
            worker_threads = st.number_input(
                "🔀 Рабочих потоков",
                min_value=1,
                max_value=32,
                value=config.performance.worker_threads,
                help="Количество рабочих потоков для обработки задач"
            )
            
            # Размер очереди
            task_queue_size = st.number_input(
                "📋 Размер очереди задач",
                min_value=10,
                max_value=10000,
                value=config.performance.task_queue_size,
                help="Максимальный размер очереди задач"
            )
        
        with col2:
            # Кэширование
            enable_caching = st.checkbox(
                "💾 Включить кэширование",
                value=config.performance.enable_caching,
                help="Кэшировать результаты для ускорения"
            )
            
            cache_size_mb = st.number_input(
                "📦 Размер кэша (MB)",
                min_value=10,
                max_value=2048,
                value=config.performance.cache_size_mb,
                help="Максимальный размер кэша",
                disabled=not enable_caching
            )
        
        # Сетевые настройки
        st.markdown("### 🌐 Сетевые настройки")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Таймауты
            http_timeout_seconds = st.number_input(
                "🌐 HTTP таймаут (секунды)",
                min_value=1,
                max_value=300,
                value=config.network.http_timeout_seconds,
                help="Таймаут для HTTP запросов"
            )
            
            # Retry
            max_retries = st.number_input(
                "🔄 Максимум повторов",
                min_value=0,
                max_value=10,
                value=config.network.max_retries,
                help="Количество повторных попыток при ошибках"
            )
        
        with col2:
            # User Agent
            user_agent = st.text_input(
                "🔍 User Agent",
                value=config.network.user_agent or "MultiAgent-System/1.0",
                help="User Agent для HTTP запросов"
            )
            
            # Прокси
            proxy_url = st.text_input(
                "🔀 Proxy URL (опционально)",
                value=config.network.proxy_url or "",
                placeholder="http://proxy:8080",
                help="URL прокси-сервера"
            )
        
        # Кнопки действий
        col1, col2, col3 = st.columns(3)
        
        with col1:
            if st.button("💾 Сохранить системные настройки", type="primary"):
                save_system_settings(
                    config_manager, config, log_level, log_format, log_rotation_mb,
                    work_directory, temp_directory, system_language, max_concurrent_workflows,
                    max_concurrent_agents, memory_limit_mb, disk_space_limit_gb,
                    execution_timeout_minutes, cleanup_interval_hours, worker_threads,
                    task_queue_size, enable_caching, cache_size_mb, http_timeout_seconds,
                    max_retries, user_agent, proxy_url
                )
        
        with col2:
            if st.button("🔄 Сбросить к умолчаниям"):
                reset_to_defaults(config_manager)
        
        with col3:
            if st.button("📋 Экспорт конфигурации"):
                export_configuration(config)
        
        # Системная информация
        show_system_info()
    
    except Exception as e:
        st.error(f"❌ Ошибка системных настроек: {e}")

def save_system_settings(config_manager, config, log_level, log_format, log_rotation,
                        work_dir, temp_dir, language, max_workflows, max_agents,
                        memory_limit, disk_limit, exec_timeout, cleanup_interval,
                        worker_threads, queue_size, caching, cache_size,
                        http_timeout, retries, user_agent, proxy):
    """Сохранение системных настроек"""
    
    try:
        new_config = config
        
        # Логирование
        new_config.logging.level = log_level
        new_config.logging.format = log_format
        new_config.logging.rotation_size_mb = log_rotation
        
        # Система
        new_config.system.work_directory = work_dir
        new_config.system.temp_directory = temp_dir
        new_config.system.language = language
        new_config.system.cleanup_interval_hours = cleanup_interval
        
        # Лимиты ресурсов
        new_config.resource_limits.max_concurrent_workflows = max_workflows
        new_config.resource_limits.max_concurrent_agents = max_agents
        new_config.resource_limits.memory_limit_mb = memory_limit
        new_config.resource_limits.disk_space_limit_gb = disk_limit
        new_config.resource_limits.execution_timeout_minutes = exec_timeout
        
        # Производительность
        new_config.performance.worker_threads = worker_threads
        new_config.performance.task_queue_size = queue_size
        new_config.performance.enable_caching = caching
        new_config.performance.cache_size_mb = cache_size
        
        # Сеть
        new_config.network.http_timeout_seconds = http_timeout
        new_config.network.max_retries = retries
        new_config.network.user_agent = user_agent
        new_config.network.proxy_url = proxy if proxy else None
        
        config_manager.update_config(new_config)
        st.success("✅ Системные настройки сохранены")
    
    except Exception as e:
        st.error(f"❌ Ошибка сохранения: {e}")

def reset_to_defaults(config_manager):
    """Сброс к настройкам по умолчанию"""
    
    if st.button("⚠️ Подтвердить сброс к умолчаниям"):
        try:
            config_manager.reset_to_defaults()
            st.success("✅ Настройки сброшены к умолчаниям")
            st.rerun()
        except Exception as e:
            st.error(f"❌ Ошибка сброса: {e}")

def export_configuration(config):
    """Экспорт конфигурации"""
    
    try:
        # Преобразуем конфигурацию в JSON
        config_dict = config.__dict__ if hasattr(config, '__dict__') else {}
        
        # Маскируем чувствительные данные
        if 'llm' in config_dict and 'api_key' in config_dict['llm']:
            config_dict['llm']['api_key'] = "***MASKED***"
        
        config_json = json.dumps(config_dict, indent=2, default=str, ensure_ascii=False)
        
        st.download_button(
            label="💾 Скачать конфигурацию",
            data=config_json,
            file_name=f"multiagent_config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            mime="application/json"
        )
        
        st.success("✅ Конфигурация готова для скачивания")
    
    except Exception as e:
        st.error(f"❌ Ошибка экспорта: {e}")

def show_system_info():
    """Системная информация"""
    
    st.markdown("### 💻 Системная информация")
    
    try:
        import platform
        import sys
        import psutil
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.markdown("**🖥️ Платформа:**")
            st.info(f"ОС: {platform.system()}")
            st.info(f"Версия: {platform.release()}")
            st.info(f"Архитектура: {platform.architecture()[0]}")
            st.info(f"Процессор: {platform.processor()}")
        
        with col2:
            st.markdown("**🐍 Python:**")
            st.info(f"Версия: {sys.version.split()[0]}")
            st.info(f"Executable: {sys.executable}")
            
            # Виртуальное окружение
            venv_active = os.environ.get('VIRTUAL_ENV') is not None
            if venv_active:
                st.success("✅ Virtual env активно")
                st.info(f"Путь: {os.environ.get('VIRTUAL_ENV')}")
            else:
                st.warning("⚠️ Virtual env неактивно")
        
        with col3:
            st.markdown("**💻 Ресурсы:**")
            
            # CPU
            cpu_percent = psutil.cpu_percent(interval=1)
            st.metric("CPU", f"{cpu_percent:.1f}%")
            
            # Память
            memory = psutil.virtual_memory()
            memory_percent = memory.percent
            memory_available = memory.available / (1024**3)  # GB
            st.metric("RAM", f"{memory_percent:.1f}%")
            st.info(f"Доступно: {memory_available:.1f} GB")
            
            # Диск
            disk = psutil.disk_usage('/')
            disk_percent = (disk.used / disk.total) * 100
            disk_free = disk.free / (1024**3)  # GB
            st.metric("Диск", f"{disk_percent:.1f}%")
            st.info(f"Свободно: {disk_free:.1f} GB")
        
        # Переменные окружения
        with st.expander("🔧 Важные переменные окружения"):
            important_vars = [
                "PATH", "PYTHONPATH", "VIRTUAL_ENV", "HOME", "USER",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY"
            ]
            
            for var in important_vars:
                value = os.environ.get(var, "Не установлена")
                
                # Маскируем API ключи
                if "API_KEY" in var and value != "Не установлена":
                    value = value[:8] + "..." if len(value) > 8 else "***"
                
                st.code(f"{var} = {value}")
    
    except Exception as e:
        st.error(f"❌ Ошибка получения системной информации: {e}")

if __name__ == "__main__":
    main()
