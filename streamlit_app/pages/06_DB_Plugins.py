"""
Страница управления DB плагинами
===============================
"""

import streamlit as st
import sys
from pathlib import Path
import json
from datetime import datetime

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

def main():
    st.set_page_config(
        page_title="DB Plugins - MultiAgent System",
        page_icon="🔌",
        layout="wide"
    )
    
    st.title("🔌 Управление плагинами БД")
    st.markdown("---")
    
    # Главные вкладки
    tab1, tab2, tab3 = st.tabs(["🛠️ Доступные плагины", "🧪 Тестирование", "📊 Диагностика"])
    
    with tab1:
        show_available_plugins()
    
    with tab2:
        show_connection_testing()
    
    with tab3:
        show_plugin_diagnostics()

def show_available_plugins():
    """Отображение доступных плагинов БД"""
    
    st.markdown("## 🛠️ Доступные плагины баз данных")
    
    try:
        from db_plugins.streamlit_api import get_db_plugin_manager
        
        db_manager = get_db_plugin_manager()
        plugins = db_manager.list_plugins()
        
        if not plugins:
            st.error("❌ Не найдено плагинов БД")
            return
        
        # Сводная статистика
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric("🔌 Всего плагинов", len(plugins))
        
        with col2:
            schemes = list(set(p.scheme for p in plugins))
            st.metric("🏷️ Типов БД", len(schemes))
        
        with col3:
            total_features = sum(len(p.supported_features) for p in plugins)
            st.metric("⚡ Возможностей", total_features)
        
        # Детальная информация о плагинах
        for plugin in plugins:
            with st.expander(f"🔌 {plugin.name} ({plugin.scheme})", expanded=False):
                
                col1, col2 = st.columns([2, 1])
                
                with col1:
                    st.markdown(f"**📋 Описание:** {plugin.description}")
                    st.markdown(f"**🏷️ Схема:** `{plugin.scheme}`")
                    st.markdown(f"**🗃️ Диалект:** {plugin.dialect_label}")
                    
                    if plugin.supported_features:
                        st.markdown("**⚡ Поддерживаемые возможности:**")
                        for feature in plugin.supported_features:
                            st.markdown(f"- ✅ {feature}")
                    
                    if plugin.dsn_examples:
                        st.markdown("**📋 Примеры DSN:**")
                        for example in plugin.dsn_examples:
                            st.code(example, language='text')
                
                with col2:
                    st.markdown("**📊 Характеристики:**")
                    
                    # Проверка доступности
                    try:
                        from db_plugins import get_plugin
                        test_plugin = get_plugin(plugin.scheme + "://test")
                        st.success("✅ Плагин загружается")
                    except Exception as e:
                        st.error(f"❌ Ошибка загрузки: {str(e)[:50]}...")
                    
                    # Информация о лимитах
                    limits_info = db_manager.get_sql_generation_limits(plugin.scheme)
                    
                    st.info(f"**Синтаксис лимитов:** {limits_info.get('limit_syntax', 'LIMIT')}")
                    st.info(f"**Макс. строк:** {limits_info.get('max_rows_recommended', 1000)}")
                    st.info(f"**Кавычки:** {limits_info.get('quote_char', '\"')}")
                    
                    # Быстрый тест
                    if st.button(f"🧪 Быстрый тест", key=f"quick_test_{plugin.scheme}"):
                        quick_test_plugin(plugin)
        
        # Информация о системе плагинов
        with st.expander("ℹ️ Информация о системе плагинов", expanded=False):
            st.markdown("""
            ### 🔧 Архитектура плагинов
            
            Система плагинов БД использует **унифицированный интерфейс** для работы с различными типами баз данных:
            
            **📋 Поддерживаемые операции:**
            - Подключение и тестирование соединения
            - Интроспекция схемы (таблицы, колонки, связи)
            - Выполнение SELECT запросов с лимитами через API
            - Валидация SQL и проверка безопасности
            - Получение планов выполнения (EXPLAIN)
            
            **🔒 Правила безопасности:**
            - Лимиты строк применяются через API плагина (не через SQL)
            - Обязательное указание схемы в DSN
            - Валидация всех запросов перед выполнением
            - Поддержка только SELECT операций для Text-to-SQL
            
            **⚙️ Диалект-специфичные возможности:**
            - Автоматическое определение синтаксиса лимитов (LIMIT vs TOP)
            - Корректная обработка кавычек и идентификаторов
            - Оптимизированные схемы интроспекции для каждой БД
            """)
    
    except Exception as e:
        st.error(f"❌ Ошибка загрузки плагинов: {e}")
        st.exception(e)

def quick_test_plugin(plugin):
    """Быстрый тест плагина"""
    
    st.markdown(f"### 🧪 Тест плагина {plugin.name}")
    
    test_results = []
    
    # Тест 1: Загрузка плагина
    try:
        from db_plugins import get_plugin
        test_plugin = get_plugin(plugin.scheme + "://test")
        test_results.append(("✅", "Загрузка плагина", "OK"))
    except Exception as e:
        test_results.append(("❌", "Загрузка плагина", f"Ошибка: {e}"))
    
    # Тест 2: Проверка методов
    required_methods = ["connect", "execute_select", "introspect_schema", "close"]
    
    try:
        for method in required_methods:
            if hasattr(test_plugin, method):
                test_results.append(("✅", f"Метод {method}", "Присутствует"))
            else:
                test_results.append(("❌", f"Метод {method}", "Отсутствует"))
    except:
        test_results.append(("❌", "Проверка методов", "Ошибка доступа к плагину"))
    
    # Тест 3: Валидация DSN
    try:
        from db_plugins.streamlit_api import get_db_plugin_manager
        db_manager = get_db_plugin_manager()
        
        if plugin.dsn_examples:
            validation = db_manager.validate_dsn(plugin.dsn_examples[0])
            if validation.is_valid:
                test_results.append(("✅", "Валидация DSN", "Пример DSN валиден"))
            else:
                test_results.append(("⚠️", "Валидация DSN", "Пример DSN невалиден"))
        else:
            test_results.append(("ℹ️", "Валидация DSN", "Нет примеров DSN"))
    except Exception as e:
        test_results.append(("❌", "Валидация DSN", f"Ошибка: {e}"))
    
    # Отображение результатов
    for icon, test_name, result in test_results:
        if icon == "✅":
            st.success(f"{icon} **{test_name}**: {result}")
        elif icon == "❌":
            st.error(f"{icon} **{test_name}**: {result}")
        elif icon == "⚠️":
            st.warning(f"{icon} **{test_name}**: {result}")
        else:
            st.info(f"{icon} **{test_name}**: {result}")

def show_connection_testing():
    """Интерфейс тестирования соединений"""
    
    st.markdown("## 🧪 Тестирование соединений")
    
    try:
        from db_plugins.streamlit_api import get_db_plugin_manager
        
        db_manager = get_db_plugin_manager()
        plugins = db_manager.list_plugins()
        
        # Форма тестирования
        with st.form("connection_test_form"):
            st.markdown("### 🔗 Параметры тестирования")
            
            col1, col2 = st.columns(2)
            
            with col1:
                selected_plugin = st.selectbox(
                    "🔌 Выберите плагин",
                    options=[f"{p.scheme} - {p.name}" for p in plugins],
                    help="Тип базы данных для тестирования"
                )
                
                scheme = selected_plugin.split(" - ")[0] if selected_plugin else ""
                plugin_info = next((p for p in plugins if p.scheme == scheme), None)
                
                if plugin_info and plugin_info.dsn_examples:
                    st.markdown("**📋 Примеры DSN:**")
                    for example in plugin_info.dsn_examples:
                        st.code(example, language='text')
            
            with col2:
                dsn = st.text_input(
                    "🔗 DSN для тестирования",
                    placeholder="scheme://user:password@host:port/database.schema",
                    help="Полная строка подключения к базе данных"
                )
                
                timeout = st.number_input(
                    "⏱️ Таймаут (секунды)",
                    min_value=1,
                    max_value=60,
                    value=10,
                    help="Максимальное время ожидания соединения"
                )
            
            # Дополнительные опции
            with st.expander("⚙️ Дополнительные опции тестирования"):
                col1, col2 = st.columns(2)
                
                with col1:
                    test_basic_query = st.checkbox(
                        "🔍 Тестовый SELECT",
                        value=True,
                        help="Выполнить простой SELECT запрос"
                    )
                    
                    test_schema_introspection = st.checkbox(
                        "📊 Интроспекция схемы",
                        value=True,
                        help="Проверить загрузку схемы БД"
                    )
                
                with col2:
                    test_security_validation = st.checkbox(
                        "🔒 Проверка безопасности",
                        value=True,
                        help="Тестировать валидацию SQL"
                    )
                    
                    verbose_output = st.checkbox(
                        "📝 Подробный вывод",
                        value=False,
                        help="Показать детальную информацию"
                    )
            
            # Кнопка тестирования
            test_clicked = st.form_submit_button("🧪 Запустить тест", type="primary")
            
            if test_clicked and dsn:
                import os, uuid
                run_id = f"run-{uuid.uuid4().hex[:16]}"
                os.environ["RUN_ID"] = run_id
                try:
                    from unified_logging import get_run_logger
                    _rlog = get_run_logger(run_id, __name__)
                    _rlog.info("Старт комплексного теста DB плагина")
                except Exception:
                    pass
                run_comprehensive_test(
                    db_manager, dsn, timeout, test_basic_query, 
                    test_schema_introspection, test_security_validation, verbose_output
                )
            elif test_clicked and not dsn:
                st.error("❌ Введите DSN для тестирования")
        
        # Сохраненные конфигурации для тестирования
        show_saved_test_configurations()
    
    except Exception as e:
        st.error(f"❌ Ошибка инициализации тестирования: {e}")

def run_comprehensive_test(db_manager, dsn, timeout, test_basic_query, 
                          test_schema_introspection, test_security_validation, verbose_output):
    """Запуск комплексного тестирования соединения"""
    
    st.markdown("### 📊 Результаты тестирования")
    
    test_results = {}
    start_time = datetime.now()
    
    # Тест 1: Валидация DSN
    with st.spinner("Валидация DSN..."):
        try:
            validation = db_manager.validate_dsn(dsn, check_schema_requirement=True)
            test_results["dsn_validation"] = {
                "success": validation.is_valid,
                "details": validation,
                "duration_ms": 0
            }
        except Exception as e:
            test_results["dsn_validation"] = {
                "success": False,
                "error": str(e),
                "duration_ms": 0
            }
    
    # Тест 2: Тестирование соединения
    with st.spinner("Тестирование соединения..."):
        try:
            conn_start = datetime.now()
            connection_result = db_manager.test_connection(dsn, timeout_seconds=timeout)
            conn_duration = (datetime.now() - conn_start).total_seconds() * 1000
            
            test_results["connection"] = {
                "success": connection_result.success,
                "details": connection_result,
                "duration_ms": conn_duration
            }
        except Exception as e:
            test_results["connection"] = {
                "success": False,
                "error": str(e),
                "duration_ms": 0
            }
    
    # Тест 3: Базовый SELECT (если включен)
    if test_basic_query and test_results["connection"]["success"]:
        with st.spinner("Выполнение тестового запроса..."):
            try:
                from db_plugins import get_plugin
                
                query_start = datetime.now()
                plugin = get_plugin(dsn)
                conn = plugin.connect(dsn)
                
                try:
                    # Простейший запрос
                    result = plugin.execute_select(conn, "SELECT 1 as test_column", row_limit=1)
                    query_duration = (datetime.now() - query_start).total_seconds() * 1000
                    
                    # Проверяем результат выполнения запроса
                    if result.get("success", False):
                        test_results["basic_query"] = {
                            "success": True,
                            "details": {
                                "rows": result.get("rows_affected", 0),
                                "columns": len(result.get("columns", [])),
                                "data": result.get("data", [])
                            },
                            "duration_ms": query_duration
                        }
                    else:
                        test_results["basic_query"] = {
                            "success": False,
                            "error": result.get("error_message", "Неизвестная ошибка выполнения запроса"),
                            "duration_ms": query_duration
                        }
                finally:
                    plugin.close(conn)
                    
            except Exception as e:
                test_results["basic_query"] = {
                    "success": False,
                    "error": str(e),
                    "duration_ms": 0
                }
    
    # Тест 4: Интроспекция схемы (если включена)
    if test_schema_introspection and test_results["connection"]["success"]:
        with st.spinner("Тестирование интроспекции схемы..."):
            try:
                from db_plugins import get_plugin
                
                schema_start = datetime.now()
                plugin = get_plugin(dsn)
                conn = plugin.connect(dsn)
                
                try:
                    schema = plugin.introspect_schema(conn)
                    schema_duration = (datetime.now() - schema_start).total_seconds() * 1000
                    
                    test_results["schema_introspection"] = {
                        "success": True,
                        "details": {
                            "tables_count": len(schema),
                            "total_columns": sum(len(table.get("columns", {})) for table in schema.values())
                        },
                        "duration_ms": schema_duration
                    }
                finally:
                    plugin.close(conn)
                    
            except Exception as e:
                test_results["schema_introspection"] = {
                    "success": False,
                    "error": str(e),
                    "duration_ms": 0
                }
    
    # Тест 5: Валидация безопасности (если включена)
    if test_security_validation and test_results["connection"]["success"]:
        with st.spinner("Тестирование валидации безопасности..."):
            try:
                from text_to_sql_streamlit_api import get_text_to_sql_manager
                
                security_start = datetime.now()
                sql_manager = get_text_to_sql_manager()
                
                # Тестируем безопасный запрос
                safe_result = sql_manager.validate_sql("SELECT 1", dsn)
                
                # Тестируем небезопасный запрос
                unsafe_result = sql_manager.validate_sql("DROP TABLE users", dsn)
                
                security_duration = (datetime.now() - security_start).total_seconds() * 1000
                
                test_results["security_validation"] = {
                    "success": True,
                    "details": {
                        "safe_query_valid": safe_result.is_valid,
                        "unsafe_query_blocked": not unsafe_result.is_safe
                    },
                    "duration_ms": security_duration
                }
                
            except Exception as e:
                test_results["security_validation"] = {
                    "success": False,
                    "error": str(e),
                    "duration_ms": 0
                }
    
    # Отображение результатов
    total_duration = (datetime.now() - start_time).total_seconds() * 1000
    
    st.markdown(f"**⏱️ Общее время тестирования:** {total_duration:.1f}ms")
    st.markdown("---")
    
    # Результаты по категориям
    for test_name, result in test_results.items():
        test_display_name = {
            "dsn_validation": "🔍 Валидация DSN",
            "connection": "🔗 Соединение",
            "basic_query": "📊 Базовый запрос",
            "schema_introspection": "🗄️ Интроспекция схемы",
            "security_validation": "🔒 Валидация безопасности"
        }.get(test_name, test_name)
        
        if result["success"]:
            st.success(f"✅ {test_display_name} - Успешно ({result['duration_ms']:.1f}ms)")
            
            if verbose_output and "details" in result:
                with st.expander(f"📋 Детали: {test_display_name}"):
                    if hasattr(result["details"], "__dict__"):
                        st.json(result["details"].__dict__)
                    else:
                        st.json(result["details"])
        else:
            st.error(f"❌ {test_display_name} - Ошибка")
            if "error" in result:
                st.error(f"**Ошибка:** {result['error']}")

def show_saved_test_configurations():
    """Управление сохраненными конфигурациями тестирования"""
    
    st.markdown("### 💾 Сохраненные конфигурации")
    
    # Инициализация состояния
    if "saved_test_configs" not in st.session_state:
        st.session_state.saved_test_configs = {}
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("**➕ Добавить конфигурацию:**")
        
        with st.form("save_test_config"):
            config_name = st.text_input("📝 Название конфигурации")
            config_dsn = st.text_input("🔗 DSN")
            config_description = st.text_input("📋 Описание (опционально)")
            
            if st.form_submit_button("💾 Сохранить"):
                if config_name and config_dsn:
                    st.session_state.saved_test_configs[config_name] = {
                        "dsn": config_dsn,
                        "description": config_description,
                        "created_at": datetime.now()
                    }
                    st.success(f"✅ Конфигурация '{config_name}' сохранена")
                    st.rerun()
                else:
                    st.error("❌ Заполните название и DSN")
    
    with col2:
        st.markdown("**📋 Сохраненные конфигурации:**")
        
        if st.session_state.saved_test_configs:
            for config_name, config_data in st.session_state.saved_test_configs.items():
                with st.expander(f"⚙️ {config_name}"):
                    st.markdown(f"**DSN:** `{config_data['dsn']}`")
                    st.markdown(f"**Описание:** {config_data.get('description', 'Нет')}")
                    st.markdown(f"**Создано:** {config_data['created_at'].strftime('%Y-%m-%d %H:%M')}")
                    
                    action_col1, action_col2 = st.columns(2)
                    
                    with action_col1:
                        if st.button(f"🧪 Тест", key=f"test_config_{config_name}"):
                            # Заполняем форму тестирования
                            st.info(f"DSN для тестирования: {config_data['dsn']}")
                    
                    with action_col2:
                        if st.button(f"🗑️ Удалить", key=f"delete_config_{config_name}"):
                            del st.session_state.saved_test_configs[config_name]
                            st.success(f"✅ Конфигурация '{config_name}' удалена")
                            st.rerun()
        else:
            st.info("📭 Нет сохраненных конфигураций")

def show_plugin_diagnostics():
    """Диагностика и отладка плагинов"""
    
    st.markdown("## 📊 Диагностика плагинов")
    
    # Системная информация
    st.markdown("### 🔧 Системная информация")
    
    try:
        import platform
        import sys
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.info(f"**Платформа:** {platform.system()}")
            st.info(f"**Версия Python:** {sys.version.split()[0]}")
        
        with col2:
            st.info(f"**Архитектура:** {platform.architecture()[0]}")
            st.info(f"**Процессор:** {platform.processor()}")
        
        with col3:
            st.info(f"**Hostname:** {platform.node()}")
            st.info(f"**ОС:** {platform.platform()}")
    
    except Exception as e:
        st.error(f"❌ Ошибка получения системной информации: {e}")
    
    # Статус плагинов
    st.markdown("### 🔌 Статус плагинов")
    
    try:
        from db_plugins.streamlit_api import get_db_plugin_manager
        import importlib
        
        db_manager = get_db_plugin_manager()
        plugins = db_manager.list_plugins()
        
        plugin_status = []
        
        # Маппинг схем на реальные имена модулей
        scheme_to_module = {
            "postgresql": "postgres",  # postgresql использует модуль postgres
            "psql": "postgres",        # psql использует модуль postgres  
            "pg": "postgres"           # pg использует модуль postgres
        }
        
        for plugin in plugins:
            # Определяем реальное имя модуля для отображения
            real_module_name = scheme_to_module.get(plugin.scheme, plugin.scheme)
            
            status = {
                "Плагин": plugin.name,
                "Схема": plugin.scheme,
                "Модуль": f"db_plugins.{real_module_name}",
                "Загружен": "❌",
                "Ошибка": ""
            }
            
            try:
                # Определяем реальное имя модуля (с учетом алиасов)
                real_module_name = scheme_to_module.get(plugin.scheme, plugin.scheme)
                module_name = f"db_plugins.{real_module_name}"
                
                # Пытаемся импортировать модуль плагина
                module = importlib.import_module(module_name)
                status["Загружен"] = "✅"
                
                # Проверяем наличие класса плагина, наследующегося от BaseDBPlugin
                from db_plugins.base import BaseDBPlugin
                plugin_class_found = False
                
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (isinstance(attr, type) and 
                        issubclass(attr, BaseDBPlugin) and 
                        attr != BaseDBPlugin):
                        plugin_class_found = True
                        break
                
                if plugin_class_found:
                    status["Класс Plugin"] = "✅"
                else:
                    status["Класс Plugin"] = "❌"
                    
            except ImportError as e:
                status["Ошибка"] = f"ImportError: {e}"
            except Exception as e:
                status["Ошибка"] = f"Ошибка: {e}"
            
            plugin_status.append(status)
        
        if plugin_status:
            st.dataframe(plugin_status, use_container_width=True)
    
    except Exception as e:
        st.error(f"❌ Ошибка проверки статуса плагинов: {e}")
    
    # Проверка зависимостей
    st.markdown("### 📦 Проверка зависимостей")
    
    dependencies = [
        ("sqlglot", "SQL парсинг и валидация"),
        ("psycopg2", "PostgreSQL подключения"),
        ("PyMySQL", "MySQL подключения"),
        ("sqlite3", "SQLite подключения (встроенный)"),
        ("duckdb", "DuckDB подключения"),
        ("pyodbc", "ODBC подключения"),
        ("pandas", "Обработка данных")
    ]
    
    dep_status = []
    
    for dep_name, description in dependencies:
        status = {
            "Пакет": dep_name,
            "Описание": description,
            "Статус": "❌",
            "Версия": "",
            "Путь": ""
        }
        
        try:
            if dep_name == "sqlite3":
                import sqlite3
                module = sqlite3
            else:
                module = __import__(dep_name)
            
            status["Статус"] = "✅"
            
            if hasattr(module, "__version__"):
                status["Версия"] = module.__version__
            
            if hasattr(module, "__file__"):
                status["Путь"] = str(Path(module.__file__).parent)[:50] + "..."
                
        except ImportError:
            status["Статус"] = "❌ Не установлен"
        except Exception as e:
            status["Статус"] = f"❌ Ошибка: {e}"
        
        dep_status.append(status)
    
    st.dataframe(dep_status, use_container_width=True)
    
    # Тест производительности
    st.markdown("### ⚡ Тест производительности")
    
    if st.button("🚀 Запустить бенчмарк плагинов"):
        run_plugin_benchmark()

def run_plugin_benchmark():
    """Бенчмарк производительности плагинов"""
    
    st.markdown("#### 📊 Результаты бенчмарка")
    
    try:
        from db_plugins.streamlit_api import get_db_plugin_manager
        import time
        
        db_manager = get_db_plugin_manager()
        plugins = db_manager.list_plugins()
        
        benchmark_results = []
        
        for plugin in plugins:
            result = {
                "Плагин": plugin.name,
                "Схема": plugin.scheme,
                "Время загрузки": "N/A",
                "Валидация DSN": "N/A",
                "Статус": "❌"
            }
            
            try:
                # Тест загрузки плагина
                start_time = time.time()
                from db_plugins import get_plugin
                test_plugin = get_plugin(plugin.scheme + "://test")
                load_time = (time.time() - start_time) * 1000
                
                result["Время загрузки"] = f"{load_time:.2f}ms"
                
                # Тест валидации DSN
                if plugin.dsn_examples:
                    start_time = time.time()
                    validation = db_manager.validate_dsn(plugin.dsn_examples[0])
                    validation_time = (time.time() - start_time) * 1000
                    
                    result["Валидация DSN"] = f"{validation_time:.2f}ms"
                
                result["Статус"] = "✅"
                
            except Exception as e:
                result["Статус"] = f"❌ {str(e)[:30]}..."
            
            benchmark_results.append(result)
        
        if benchmark_results:
            st.dataframe(benchmark_results, use_container_width=True)
            
            # Сводная статистика
            successful_tests = [r for r in benchmark_results if r["Статус"] == "✅"]
            
            if successful_tests:
                st.success(f"✅ Успешно протестировано {len(successful_tests)}/{len(benchmark_results)} плагинов")
                
                # Средние времена
                load_times = []
                validation_times = []
                
                for result in successful_tests:
                    if "ms" in result["Время загрузки"]:
                        load_times.append(float(result["Время загрузки"].replace("ms", "")))
                    
                    if "ms" in result["Валидация DSN"]:
                        validation_times.append(float(result["Валидация DSN"].replace("ms", "")))
                
                if load_times:
                    avg_load = sum(load_times) / len(load_times)
                    st.info(f"📊 Среднее время загрузки: {avg_load:.2f}ms")
                
                if validation_times:
                    avg_validation = sum(validation_times) / len(validation_times)
                    st.info(f"📊 Среднее время валидации: {avg_validation:.2f}ms")
    
    except Exception as e:
        st.error(f"❌ Ошибка бенчмарка: {e}")

if __name__ == "__main__":
    main()
