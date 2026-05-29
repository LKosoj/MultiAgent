"""
Страница просмотра логов и трасс телеметрии
==========================================

Расширенная система фильтрации и анализа логов:

🔍 Основные возможности фильтрации:
- Фильтрация по уровню логирования (ERROR, WARNING, INFO, DEBUG)
- Поиск по тексту с поддержкой регулярных выражений
- Фильтрация по времени (временной диапазон)
- Фильтрация по агентам и модулям
- Фильтрация по типу событий
- Инвертированный поиск (исключение найденного)
- Контекстные строки вокруг найденных результатов

🔖 Быстрые фильтры:
- "Только ошибки" - показать только ERROR логи
- "Агенты" - найти все упоминания агентов
- "SQL запросы" - найти SQL-операции с регулярными выражениями
- "Сбросить фильтры" - очистить все фильтры

📊 Аналитика:
- Статистика по уровням логирования
- Временные паттерны активности
- Экспорт отфильтрованных результатов

🔍 Мульти-поиск:
- Поиск по всем файлам логов одновременно
- Прогресс выполнения поиска
- Экспорт результатов мульти-поиска

📊 Расширенная фильтрация трасс OpenTelemetry:
- Фильтрация по количеству спанов (мин/макс до 10,000 спанов)
- Фильтрация по длительности выполнения (в секундах, до 7 дней)
- Поиск по именам спанов с регулярными выражениями
- Поиск по атрибутам спанов (ключ:значение)
- Фильтрация по типу операции
- Поиск по тексту ошибок в спанах
- Фильтрация только корневых или включение вложенных спанов
- Сортировка по длительности выполнения

🔖 Быстрые фильтры для трасс (2 ряда):
- "Только с ошибками" - трассы содержащие ошибки
- "Долгие операции (>30с)" - операции длительностью более 30 секунд
- "Очень долгие (>10мин)" - операции длительностью более 10 минут
- "Быстрые операции (<5с)" - быстрые операции до 5 секунд
- "Агентские операции" - операции связанные с агентами
- "SQL операции" - операции генерации SQL
- "Сложные трассы (>10 спанов)" - трассы с множественными операциями
- Детальная статистика и анализ спанов
"""

import streamlit as st
import streamlit.components.v1 as components
import sys
from pathlib import Path
import json
import base64
import gzip
from io import BytesIO
import os
from datetime import datetime, timedelta
import pandas as pd

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# Импортируем функцию отображения артефактов workflow
try:
    # Импортируем напрямую из модуля
    import importlib.util
    spec = importlib.util.spec_from_file_location("workflows_page", Path(__file__).parent / "02_Workflows.py")
    workflows_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflows_module)
    show_workflow_artifacts = workflows_module.show_workflow_artifacts
except ImportError:
    # Создаем простую fallback функцию
    def show_workflow_artifacts(final_output, run_id):
        if isinstance(final_output, dict):
            st.json(final_output)
        else:
            st.write(final_output)
from html_utils import html_visualizer
from telemetry.helpers import is_trace_completed, get_trace_status

_is_trace_completed = is_trace_completed

def main():
    st.set_page_config(
        page_title="Logs/Traces - MultiAgent System",
        page_icon="🔍",
        layout="wide"
    )
    
    st.title("🔍 Логи и трассы телеметрии")
    st.markdown("---")
    
    # Главные вкладки
    tab1, tab2, tab3, tab4 = st.tabs([
        "📊 Трассы OpenTelemetry", 
        "📝 Логи системы", 
        "📈 Аналитика", 
        "⚙️ Настройки"
    ])
    
    with tab1:
        show_telemetry_traces()
    
    with tab2:
        show_system_logs()
    
    with tab3:
        show_analytics()
    
    with tab4:
        show_telemetry_settings()

def show_telemetry_traces():
    """Отображение трасс OpenTelemetry с интегрированными логами"""
    
    st.markdown("## 📊 Трассы OpenTelemetry")
    
    # Информационная панель о новой функциональности
    with st.expander("ℹ️ Новая функциональность: Интегрированные логи", expanded=False):
        st.markdown("""
        🔗 **Теперь логи интегрированы прямо в дерево спанов!**
        
        **Как использовать:**
        1. Выберите трассу из списка ниже
        2. В дереве спанов кликните на любой спан
        3. В правой панели активируйте чекбокс **"📝 Показать логи"**
        4. Все логи, связанные с выбранным спаном, отобразятся под деталями спана
        
        **Возможности:**
        - 🎯 Автоматическая корреляция по `span_id`
        - 🔍 Фильтрация логов по уровню и тексту
        - ⏱️ Хронологическая сортировка логов
        - 🎨 Цветовое кодирование по уровню важности
        
        Это заменяет отдельную вкладку корреляции для более интуитивного использования!
        """)
    
    st.markdown("---")
    
    try:
        from telemetry import get_telemetry_manager
        
        telemetry_manager = get_telemetry_manager()
        
        # Статус телеметрии
        col1, col2, col3 = st.columns(3)
        
        with col1:
            if telemetry_manager.is_enabled():
                st.success("✅ Телеметрия включена")
            else:
                st.error("❌ Телеметрия отключена")
                
                if st.button("▶️ Включить телеметрию"):
                    telemetry_manager.enable()
                    st.rerun()
                
                return
        
        with col2:
            trace_files = telemetry_manager.get_trace_files()
            # Исключаем служебную трассу unknown
            trace_files = [tf for tf in trace_files if tf.get("run_id") != "unknown"]
            st.metric("📁 Файлов трасс", len(trace_files))
        
        with col3:
            if trace_files:
                total_events = sum(f.get("events_count", 0) for f in trace_files)
                st.metric("🔍 Всего событий", total_events)
            else:
                st.metric("🔍 Всего событий", "0")
        
        # Управление обновлением
        refresh_col1, refresh_col2, refresh_col3 = st.columns([1,1,6])
        with refresh_col1:
            if st.button("🔄 Обновить", key="traces_refresh"):
                st.rerun()
        with refresh_col2:
            auto_refresh = st.checkbox("Авто", key="traces_auto_refresh", help="Автообновление каждые 5 секунд")
            if auto_refresh:
                import time as _t
                # Инициализация таймера
                if "__traces_last_refresh" not in st.session_state:
                    st.session_state.__traces_last_refresh = _t.time()
                now = _t.time()
                if now - st.session_state.__traces_last_refresh >= 5:
                    st.session_state.__traces_last_refresh = now
                    st.rerun()
                else:
                    remain = 5 - (now - st.session_state.__traces_last_refresh)
                    st.caption(f"⏱️ Обновление через {remain:.1f}с")

        # Расширенные фильтры для трасс
        st.markdown("### 🔍 Расширенные фильтры трасс")
        
        # Первый ряд базовых фильтров
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            date_filter = st.date_input(
                "📅 Дата (от)",
                value=datetime.now().date() - timedelta(days=1),
                help="Показать трассы начиная с указанной даты"
            )
        
        with col2:
            # Подхватываем run_id из query params один раз, чтобы префиллить фильтр и авто-раскрыть нужную трассу
            try:
                params = getattr(st, "query_params", None)
                if params is not None:
                    param_rid = params.get("run_id")
                else:
                    param_rid = st.query_params.get("run_id")
                if isinstance(param_rid, list):
                    param_rid = param_rid[0] if param_rid else ""
            except Exception:
                param_rid = None

            if param_rid:
                # Сохраняем активный run_id и просим авто-раскрыть детали соответствующей трассы
                if st.session_state.get("active_trace_run_id") != param_rid:
                    st.session_state["active_trace_run_id"] = param_rid
                st.session_state.setdefault("run_id_prefill", param_rid)
                st.session_state["auto_open_trace_details"] = True

            # Значение по умолчанию для ввода: либо текущее из сессии, либо то, что пришло в query
            default_rid = st.session_state.get("run_id_filter") or st.session_state.get("run_id_prefill", "")

            run_id_filter = st.text_input(
                "🆔 Run ID",
                value=default_rid,
                key="run_id_filter",
                placeholder="Частичный или полный ID",
                help="Фильтр по идентификатору запуска"
            )
        
        with col3:
            agent_filter = st.text_input(
                "🤖 Агент/Workflow",
                placeholder="Имя агента или пайплайна",
                help="Фильтр по имени агента или workflow"
            )
        
        with col4:
            status_filter = st.selectbox(
                "📊 Статус",
                ["Все", "Успешные", "С ошибками", "Активные"],
                help="Фильтр по статусу выполнения"
            )
        
        # Второй ряд продвинутых фильтров
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            # Применяем быстрый фильтр если есть
            default_min_spans = 0
            if 'quick_trace_filter' in st.session_state:
                default_min_spans = st.session_state.quick_trace_filter.get("min_spans", 0)
                
            # Фильтр по количеству спанов
            min_spans = st.number_input(
                "📊 Мин. спанов",
                min_value=0,
                max_value=1000,
                value=default_min_spans,
                help="Минимальное количество спанов в трассе"
            )
        
        with col2:
            # Применяем быстрый фильтр если есть
            default_max_spans = 10000  # Увеличиваем лимит до 10,000 спанов
            if 'quick_trace_filter' in st.session_state:
                default_max_spans = st.session_state.quick_trace_filter.get("max_spans", 10000)
                
            max_spans = st.number_input(
                "📊 Макс. спанов",
                min_value=0,
                max_value=10000,  # Увеличиваем лимит до 10,000
                value=default_max_spans,
                help="Максимальное количество спанов в трассе (до 10,000)"
            )
        
        with col3:
            # Применяем быстрый фильтр если есть
            default_min_duration = 0.0
            if 'quick_trace_filter' in st.session_state:
                default_min_duration = st.session_state.quick_trace_filter.get("min_duration_sec", 0.0)
                
            # Фильтр по длительности в секундах
            min_duration_sec = st.number_input(
                "⏱️ Мин. длительность (сек)",
                min_value=0.0,
                max_value=86400.0,  # 24 часа
                value=default_min_duration,
                step=0.1,
                help="Минимальная длительность выполнения трассы в секундах"
            )
        
        with col4:
            # Применяем быстрый фильтр если есть
            default_max_duration = 604800.0  # 7 дней по умолчанию (24*7*3600)
            if 'quick_trace_filter' in st.session_state:
                default_max_duration = st.session_state.quick_trace_filter.get("max_duration_sec", 604800.0)
                
            max_duration_sec = st.number_input(
                "⏱️ Макс. длительность (сек)",
                min_value=0.0,
                max_value=604800.0,  # 7 дней (неделя)
                value=default_max_duration,
                step=0.1,
                help="Максимальная длительность выполнения трассы в секундах (до 7 дней)"
            )
        
        # Третий ряд фильтров по содержимому
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            # Применяем быстрый фильтр если есть
            default_span_name = ""
            if 'quick_trace_filter' in st.session_state:
                default_span_name = st.session_state.quick_trace_filter.get("span_name_filter", "")
                
            span_name_filter = st.text_input(
                "🏷️ Имя спана",
                value=default_span_name,
                placeholder="Поиск по именам спанов...",
                help="Поиск трасс, содержащих спаны с указанным именем"
            )
        
        with col2:
            attribute_filter = st.text_input(
                "🔖 Атрибут спана",
                placeholder="ключ:значение или ключ",
                help="Поиск по атрибутам спанов (например: agent_name:analyst)"
            )
        
        with col3:
            operation_filter = st.selectbox(
                "⚙️ Тип операции",
                ["Все", "agent_run", "tool_call", "workflow", "sql_generation", "web_search", "file_operation"],
                help="Фильтр по типу операции в спанах"
            )
        
        with col4:
            error_text_filter = st.text_input(
                "❌ Текст ошибки",
                placeholder="Поиск в тексте ошибок...",
                help="Поиск трасс, содержащих указанный текст в ошибках"
            )
        
        # Четвертый ряд дополнительных опций
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            use_regex_traces = st.checkbox(
                "🔍 Регулярные выражения",
                help="Использовать регулярные выражения для текстового поиска"
            )
        
        with col2:
            show_only_root_spans = st.checkbox(
                "🌳 Только корневые спаны",
                help="Фильтровать только по корневым (основным) спанам"
            )
        
        with col3:
            include_nested_spans = st.checkbox(
                "📚 Включить вложенные",
                value=True,
                help="Включать поиск во вложенных спанах"
            )
        
        with col4:
            # Применяем быстрый фильтр если есть
            default_sort = False
            if 'quick_trace_filter' in st.session_state:
                default_sort = st.session_state.quick_trace_filter.get("sort_by_duration", False)
                
            sort_by_duration = st.checkbox(
                "⏱️ Сортировать по времени",
                value=default_sort,
                help="Сортировать результаты по длительности выполнения"
            )
        
        # Конвертируем секунды в миллисекунды для внутренней обработки
        min_duration_ms = min_duration_sec * 1000
        max_duration_ms = max_duration_sec * 1000
        
        # Получаем и фильтруем трассы
        filtered_traces = get_filtered_traces_advanced(
            telemetry_manager, trace_files, 
            date_filter=date_filter,
            run_id_filter=run_id_filter,
            agent_filter=agent_filter,
            status_filter=status_filter,
            min_spans=min_spans,
            max_spans=max_spans,
            min_duration_ms=min_duration_ms,
            max_duration_ms=max_duration_ms,
            span_name_filter=span_name_filter,
            attribute_filter=attribute_filter,
            operation_filter=operation_filter,
            error_text_filter=error_text_filter,
            use_regex=use_regex_traces,
            show_only_root_spans=show_only_root_spans,
            include_nested_spans=include_nested_spans,
            sort_by_duration=sort_by_duration
        )
        
        # Если run_id указан, поднимем соответствующую трассу вверх списка
        try:
            if run_id_filter:
                filtered_traces.sort(
                    key=lambda tf: 0 if tf.get("run_id", "").startswith(run_id_filter) else 1
                )
        except Exception:
            pass

        # Быстрые фильтры для трасс
        st.markdown("### 🔖 Быстрые фильтры трасс")
        
        # Первый ряд быстрых фильтров
        quick_trace_row1_col1, quick_trace_row1_col2, quick_trace_row1_col3, quick_trace_row1_col4 = st.columns(4)
        
        with quick_trace_row1_col1:
            if st.button("❌ Только с ошибками", key="trace_errors_only"):
                st.session_state.quick_trace_filter = {
                    "status_filter": "С ошибками",
                    "min_spans": 0,
                    "max_spans": 10000,  # Обновляем до нового лимита
                    "error_text_filter": "",
                    "operation_filter": "Все"
                }
                st.rerun()
        
        with quick_trace_row1_col2:
            if st.button("🕐 Долгие операции (>30с)", key="trace_long_operations"):
                st.session_state.quick_trace_filter = {
                    "status_filter": "Все",
                    "min_duration_sec": 30.0,  # > 30 секунд
                    "max_duration_sec": 604800.0,  # до 7 дней
                    "sort_by_duration": True
                }
                st.rerun()
        
        with quick_trace_row1_col3:
            if st.button("🤖 Агентские операции", key="trace_agent_ops"):
                st.session_state.quick_trace_filter = {
                    "operation_filter": "agent_run",
                    "min_spans": 3,  # Минимум 3 спана для агентских операций
                    "max_spans": 10000,  # Обновляем до нового лимита
                    "include_nested_spans": True
                }
                st.rerun()
        
        with quick_trace_row1_col4:
            if st.button("🔄 Сбросить фильтры", key="trace_reset_filters"):
                if 'quick_trace_filter' in st.session_state:
                    del st.session_state.quick_trace_filter
                st.rerun()
        
        # Второй ряд быстрых фильтров
        quick_trace_row2_col1, quick_trace_row2_col2, quick_trace_row2_col3, quick_trace_row2_col4 = st.columns(4)
        
        with quick_trace_row2_col1:
            if st.button("⚡ Быстрые операции (<5с)", key="trace_fast_operations"):
                st.session_state.quick_trace_filter = {
                    "status_filter": "Все",
                    "min_duration_sec": 0.0,
                    "max_duration_sec": 5.0,  # до 5 секунд
                    "sort_by_duration": True
                }
                st.rerun()
        
        with quick_trace_row2_col2:
            if st.button("🐌 Очень долгие (>10мин)", key="trace_very_long"):
                st.session_state.quick_trace_filter = {
                    "status_filter": "Все",
                    "min_duration_sec": 600.0,  # > 10 минут
                    "max_duration_sec": 604800.0,  # до 7 дней
                    "sort_by_duration": True
                }
                st.rerun()
        
        with quick_trace_row2_col3:
            if st.button("🔧 SQL операции", key="trace_sql_ops"):
                st.session_state.quick_trace_filter = {
                    "operation_filter": "sql_generation",
                    "span_name_filter": "sql",
                    "include_nested_spans": True
                }
                st.rerun()
        
        with quick_trace_row2_col4:
            if st.button("📊 Сложные трассы (>10 спанов)", key="trace_complex"):
                st.session_state.quick_trace_filter = {
                    "min_spans": 10,
                    "max_spans": 10000,  # Обновляем до нового лимита
                    "include_nested_spans": True
                }
                st.rerun()

        # Отображение трасс
        if filtered_traces:
            # Статистика фильтрации
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("📊 Всего трасс", len(trace_files))
            with col2:
                st.metric("🔍 После фильтрации", len(filtered_traces))
            with col3:
                filter_ratio = (len(filtered_traces) / len(trace_files) * 100) if trace_files else 0
                st.metric("📈 Соответствие фильтрам", f"{filter_ratio:.1f}%")
            
            st.markdown(f"### 📋 Трассы ({len(filtered_traces)} найдено)")
            
            # Показываем трассы с дополнительной информацией
            display_limit = min(20, len(filtered_traces))
            for i, trace_file in enumerate(filtered_traces[:display_limit]):
                show_trace_summary_enhanced(telemetry_manager, trace_file, i+1)
            
            if len(filtered_traces) > display_limit:
                st.info(f"Показано {display_limit} из {len(filtered_traces)} трасс. Используйте дополнительные фильтры для уточнения результатов.")
        else:
            st.info("🔍 Трассы не найдены. Измените фильтры или проверьте наличие данных.")
            st.markdown("**💡 Попробуйте:**")
            st.markdown("- Расширить временной диапазон")
            st.markdown("- Уменьшить ограничения по количеству спанов")
            st.markdown("- Проверить правильность регулярных выражений")
            st.markdown("- Использовать быстрые фильтры выше")
        
        # Кнопки управления
        col1, col2, col3 = st.columns(3)
        
        with col1:
            if st.button("🔄 Обновить список"):
                st.rerun()
        
        with col2:
            if st.button("🧹 Очистить старые трассы"):
                cleanup_old_traces(telemetry_manager)
        
        with col3:
            if trace_files and st.button("📥 Экспорт трасс"):
                export_traces(telemetry_manager, filtered_traces)
    
    except Exception as e:
        st.error(f"❌ Ошибка загрузки трасс: {e}")
        st.exception(e)

def get_filtered_traces(telemetry_manager, trace_files, date_filter, 
                       run_id_filter, agent_filter, status_filter):
    """Фильтрация трасс по заданным критериям"""
    
    filtered = trace_files
    
    # Фильтр по дате
    if date_filter:
        filtered = [
            f for f in filtered 
            if f.get("modified_time", datetime.min).date() >= date_filter
        ]
    
    # Фильтр по Run ID (точное совпадение при полной длине)
    if run_id_filter:
        rid = run_id_filter.strip()
        if len(rid) >= 8:
            # Если похоже на полный/длинный run_id — используем startswith/equals
            filtered = [
                f for f in filtered
                if f.get("run_id", "").lower().startswith(rid.lower()) or f.get("run_id", "").lower() == rid.lower()
            ]
        else:
            filtered = [
                f for f in filtered 
                if rid.lower() in f.get("run_id", "").lower()
            ]
    
    # Фильтр по агенту (нужно загрузить содержимое трасс)
    if agent_filter:
        filtered_by_agent = []
        for trace_file in filtered:
            try:
                trace_content = telemetry_manager.load_trace_file(trace_file["run_id"])
                if any(agent_filter.lower() in span.get("name", "").lower() 
                      for span in trace_content.get("spans", [])):
                    filtered_by_agent.append(trace_file)
            except:
                continue
        filtered = filtered_by_agent
    
    # Фильтр по статусу
    if status_filter != "Все":
        status_filtered = []
        for trace_file in filtered:
            try:
                trace_content = telemetry_manager.load_trace_file(trace_file["run_id"])
                
                has_errors = any(
                    span.get("status", {}).get("status_code") == "ERROR"
                    for span in trace_content.get("spans", [])
                )
                
                # Правильная проверка завершенности - все спаны имеют end_time
                spans = trace_content.get("spans", [])
                trace_status = get_trace_status(spans)
                is_completed = trace_status["is_completed"]
                
                if status_filter == "Успешные" and is_completed and not trace_status["has_errors"]:
                    status_filtered.append(trace_file)
                elif status_filter == "С ошибками" and trace_status["has_errors"]:
                    status_filtered.append(trace_file)
                elif status_filter == "Активные" and not is_completed:
                    status_filtered.append(trace_file)
            except:
                continue
        
        filtered = status_filtered
    
    return filtered

def get_filtered_traces_advanced(telemetry_manager, trace_files, 
                                date_filter=None, run_id_filter="", agent_filter="", status_filter="Все",
                                min_spans=0, max_spans=10000, min_duration_ms=0, max_duration_ms=604800000,
                                span_name_filter="", attribute_filter="", operation_filter="Все",
                                error_text_filter="", use_regex=False, show_only_root_spans=False,
                                include_nested_spans=True, sort_by_duration=False):
    """Расширенная фильтрация трасс по множественным критериям"""
    
    import re
    
    filtered = trace_files.copy()
    
    # Базовые фильтры (используем существующую логику)
    filtered = get_filtered_traces(telemetry_manager, filtered, date_filter, 
                                  run_id_filter, agent_filter, status_filter)
    
    # Дополнительные расширенные фильтры
    advanced_filtered = []
    
    for trace_file in filtered:
        try:
            # Загружаем содержимое трассы
            trace_content = telemetry_manager.load_trace_file(trace_file["run_id"])
            spans = trace_content.get("spans", [])
            
            if not spans:
                continue
            
            # Фильтр по количеству спанов
            span_count = len(spans)
            if span_count < min_spans or span_count > max_spans:
                continue
            
            # Вычисляем длительность трассы
            start_times = [s.get("start_time_unix_nano", 0) for s in spans if s.get("start_time_unix_nano")]
            end_times = [s.get("end_time_unix_nano", 0) for s in spans if s.get("end_time_unix_nano")]
            
            if start_times and end_times:
                duration_ms = (max(end_times) - min(start_times)) / 1_000_000
                
                # Фильтр по длительности
                if duration_ms < min_duration_ms or duration_ms > max_duration_ms:
                    continue
                
                # Добавляем длительность в файл трассы для сортировки
                trace_file["calculated_duration_ms"] = duration_ms
            else:
                trace_file["calculated_duration_ms"] = 0
                if min_duration_ms > 0:  # Если требуется минимальная длительность, пропускаем
                    continue
            
            # Определяем спаны для поиска
            spans_to_search = spans
            if show_only_root_spans:
                spans_to_search = [s for s in spans if not s.get("parent_span_id")]
            elif not include_nested_spans:
                spans_to_search = [s for s in spans if not s.get("parent_span_id")]
            
            # Фильтр по имени спана
            if span_name_filter:
                if not search_in_spans_by_name(spans_to_search, span_name_filter, use_regex):
                    continue
            
            # Фильтр по атрибутам
            if attribute_filter:
                if not search_in_spans_by_attributes(spans_to_search, attribute_filter, use_regex):
                    continue
            
            # Фильтр по типу операции
            if operation_filter != "Все":
                if not search_in_spans_by_operation(spans_to_search, operation_filter):
                    continue
            
            # Фильтр по тексту ошибки
            if error_text_filter:
                if not search_in_spans_by_error_text(spans_to_search, error_text_filter, use_regex):
                    continue
            
            # Если все фильтры пройдены, добавляем трассу
            advanced_filtered.append(trace_file)
            
        except Exception as e:
            # Логируем ошибку, но продолжаем обработку
            continue
    
    # Сортировка результатов
    if sort_by_duration:
        advanced_filtered.sort(key=lambda x: x.get("calculated_duration_ms", 0), reverse=True)
    
    return advanced_filtered

def search_in_spans_by_name(spans, name_filter, use_regex=False):
    """Поиск по именам спанов"""
    
    import re
    
    for span in spans:
        span_name = span.get("name", "")
        
        try:
            if use_regex:
                if re.search(name_filter, span_name, re.IGNORECASE):
                    return True
            else:
                if name_filter.lower() in span_name.lower():
                    return True
        except re.error:
            # Fallback to simple search if regex is invalid
            if name_filter.lower() in span_name.lower():
                return True
    
    return False

def search_in_spans_by_attributes(spans, attribute_filter, use_regex=False):
    """Поиск по атрибутам спанов"""
    
    import re
    
    # Парсим фильтр атрибутов (формат: "ключ:значение" или просто "ключ")
    if ":" in attribute_filter:
        key_filter, value_filter = attribute_filter.split(":", 1)
        key_filter = key_filter.strip()
        value_filter = value_filter.strip()
    else:
        key_filter = attribute_filter.strip()
        value_filter = None
    
    for span in spans:
        attributes = span.get("attributes", {})
        
        for attr_key, attr_value in attributes.items():
            try:
                # Проверяем ключ
                key_match = False
                if use_regex:
                    if re.search(key_filter, attr_key, re.IGNORECASE):
                        key_match = True
                else:
                    if key_filter.lower() in attr_key.lower():
                        key_match = True
                
                if not key_match:
                    continue
                
                # Если указано только ключ, возвращаем True
                if value_filter is None:
                    return True
                
                # Проверяем значение
                attr_value_str = str(attr_value)
                if use_regex:
                    if re.search(value_filter, attr_value_str, re.IGNORECASE):
                        return True
                else:
                    if value_filter.lower() in attr_value_str.lower():
                        return True
                        
            except re.error:
                # Fallback to simple search
                if key_filter.lower() in attr_key.lower():
                    if value_filter is None:
                        return True
                    elif value_filter.lower() in str(attr_value).lower():
                        return True
    
    return False

def search_in_spans_by_operation(spans, operation_filter):
    """Поиск по типу операции"""
    
    for span in spans:
        span_name = span.get("name", "").lower()
        attributes = span.get("attributes", {})
        
        # Проверяем имя спана и атрибуты на соответствие типу операции
        operation_lower = operation_filter.lower()
        
        if (operation_lower in span_name or 
            any(operation_lower in str(v).lower() for v in attributes.values()) or
            any(operation_lower in k.lower() for k in attributes.keys())):
            return True
    
    return False

def search_in_spans_by_error_text(spans, error_filter, use_regex=False):
    """Поиск по тексту ошибок в спанах"""
    
    import re
    
    for span in spans:
        # Проверяем статус спана
        status = span.get("status", {})
        if status.get("status_code") == "ERROR":
            error_message = status.get("message", "")
            
            try:
                if use_regex:
                    if re.search(error_filter, error_message, re.IGNORECASE):
                        return True
                else:
                    if error_filter.lower() in error_message.lower():
                        return True
            except re.error:
                # Fallback to simple search
                if error_filter.lower() in error_message.lower():
                    return True
        
        # Также проверяем события спана на наличие ошибок
        events = span.get("events", [])
        for event in events:
            event_name = event.get("name", "")
            if "error" in event_name.lower() or "exception" in event_name.lower():
                event_attributes = event.get("attributes", {})
                for attr_value in event_attributes.values():
                    attr_str = str(attr_value)
                    try:
                        if use_regex:
                            if re.search(error_filter, attr_str, re.IGNORECASE):
                                return True
                        else:
                            if error_filter.lower() in attr_str.lower():
                                return True
                    except re.error:
                        if error_filter.lower() in attr_str.lower():
                            return True
    
    return False

def show_trace_summary_enhanced(telemetry_manager, trace_file, index=1):
    """Улучшенное отображение краткой информации о трассе"""
    
    run_id = trace_file["run_id"]
    detail_flag_key = f"detail_open_{run_id}"
    duration_info = ""
    
    # Добавляем информацию о длительности если доступна
    if "calculated_duration_ms" in trace_file:
        duration_ms = trace_file["calculated_duration_ms"]
        if duration_ms > 1000:
            duration_info = f" ({duration_ms/1000:.1f}s)"
        else:
            duration_info = f" ({duration_ms:.1f}ms)"
    
    is_active = st.session_state.get("active_trace_run_id") == run_id
    # Автораскрытие деталей, если пришло через query params
    if st.session_state.get("auto_open_trace_details") and is_active:
        st.session_state[detail_flag_key] = True
        # Сбрасываем флаг, чтобы не открывать бесконечно
        st.session_state["auto_open_trace_details"] = False
    is_detail_open = st.session_state.get(detail_flag_key, False)
    with st.expander(
        f"#{index} 🔍 {run_id[:12]}... {trace_file['modified_time'].strftime('%H:%M:%S')}{duration_info}", 
        expanded=is_active or is_detail_open
    ):
        # Показ финального ответа пользователю ТОЛЬКО для завершённых трасс
        try:
            trace_check = telemetry_manager.load_trace_file(run_id)
            spans_check = trace_check.get("spans", [])
            trace_status = get_trace_status(spans_check)

            if trace_status["status"] == "running":
                st.info("⏳ Трасса выполняется. Ответ пользователю будет доступен после завершения.")
            else:
                if trace_status.get("has_errors"):
                    st.warning(f"⚠️ Обнаружены ошибки, но трасса завершена: {trace_status['error_reason']}")
                final_answer = _get_final_answer_for_run(telemetry_manager, run_id)
                st.markdown("#### 🟢 Ответ пользователю")
                if final_answer:
                    # Пытаемся распарсить JSON и показать только content (без сырого ответа)
                    try:
                        import json
                        if isinstance(final_answer, str):
                            try:
                                parsed_answer = json.loads(final_answer)
                                if isinstance(parsed_answer, dict) and "content" in parsed_answer:
                                    content = parsed_answer["content"]
                                    st.markdown(content)
                                    with st.expander("📄 Детали ответа", expanded=False):
                                        if "tool_calls" in parsed_answer and parsed_answer["tool_calls"]:
                                            st.markdown("**Tool calls:**")
                                            try:
                                                if isinstance(parsed_answer["tool_calls"], (dict, list)):
                                                    st.json(parsed_answer["tool_calls"])
                                                else:
                                                    st.code(str(parsed_answer["tool_calls"]), language="json")
                                            except Exception as e:
                                                st.error(f"Ошибка отображения tool_calls: {e}")
                                                st.code(str(parsed_answer["tool_calls"]), language="text")
                                        if "token_usage" in parsed_answer:
                                            st.markdown("**Использование токенов:**")
                                            try:
                                                if isinstance(parsed_answer["token_usage"], (dict, list)):
                                                    st.json(parsed_answer["token_usage"])
                                                else:
                                                    st.code(str(parsed_answer["token_usage"]), language="json")
                                            except Exception as e:
                                                st.error(f"Ошибка отображения token_usage: {e}")
                                                st.code(str(parsed_answer["token_usage"]), language="text")
                            except json.JSONDecodeError:
                                pass
                        # Если не строка или нет content — ничего не показываем здесь
                    except Exception:
                        pass
                    # Поиск уже сохранённого отчёта в КАЖДОМ корневом спане
                    saved_report_html = None
                    try:
                        root_spans = [s for s in spans_check if not s.get("parent_span_id")]
                        for root in root_spans:
                            for ev in (root.get("events") or []):
                                if (ev.get("name") or "").lower() == "report_generated":
                                    attrs = ev.get("attributes", {}) or {}
                                    b64 = attrs.get("report.content_b64_gzip") or attrs.get("report_b64_gzip")
                                    if b64:
                                        try:
                                            saved_report_html = gzip.decompress(base64.b64decode(b64)).decode("utf-8", errors="replace")
                                            break
                                        except Exception:
                                            pass
                            if saved_report_html:
                                break
                    except Exception:
                        pass

                    # Кнопка формирования отчёта (если ещё нет сохранённого)
                    try:
                        # Проверяем как сохранённый отчёт, так и флаг в session_state
                        report_already_generated = saved_report_html or st.session_state.get(f"report_generated_{run_id}", False)
                        if not report_already_generated and st.button("🧾 Сформировать отчёт", key=f"make_report_{run_id}"):
                            # Извлекаем session_id из атрибутов спанов; fallback на run_id
                            session_id = None
                            for s in spans_check:
                                attrs = s.get("attributes", {}) or {}
                                for k in ("session_id", "session.id", "sessionId", "session", "run_id", "run.id"):
                                    if attrs.get(k):
                                        session_id = attrs.get(k)
                                        break
                                if session_id:
                                    break
                            if not session_id:
                                session_id = run_id

                            # Формируем текст отчёта из содержимого ответа
                            report_text = None
                            try:
                                if isinstance(final_answer, str):
                                    try:
                                        parsed = json.loads(final_answer)
                                        if isinstance(parsed, dict) and "content" in parsed:
                                            report_text = str(parsed.get("content"))
                                        else:
                                            report_text = final_answer
                                    except Exception:
                                        report_text = final_answer
                                elif isinstance(final_answer, dict):
                                    if "content" in final_answer:
                                        report_text = str(final_answer.get("content"))
                                    else:
                                        report_text = json.dumps(final_answer, ensure_ascii=False)
                                else:
                                    report_text = str(final_answer)
                            except Exception:
                                report_text = str(final_answer)

                            # Генерация HTML и сохранение в трассу (gzip+base64 в событии report_generated)
                            try:
                                path_to_html = html_visualizer.advanced_visualization(report_text, session_id, show=True)
                                with open(path_to_html, 'r', encoding='utf-8') as f:
                                    html_content = f.read()
                                gz = gzip.compress(html_content.encode('utf-8'))
                                b64 = base64.b64encode(gz).decode('ascii')

                                traces_dir = Path(project_root) / "logs/traces"
                                jsonl_path = traces_dir / f"{run_id}.jsonl"
                                tmp_path = traces_dir / f"{run_id}.jsonl.tmp"
                                lines = []
                                if jsonl_path.exists():
                                    with open(jsonl_path, 'r', encoding='utf-8') as fr:
                                        lines = fr.readlines()
                                # Разбираем весь файл, чтобы выбрать правильный корневой (agent_run_*)
                                objs = []
                                for line in lines:
                                    try:
                                        objs.append(json.loads(line))
                                    except Exception:
                                        # сохраняем как есть (строка не-JSON)
                                        objs.append(line)
                                # Ищем индекс целевого корневого
                                root_indices = [i for i, o in enumerate(objs) if isinstance(o, dict) and not o.get("parent_span_id")]
                                target_idx = None
                                for i in root_indices:
                                    o = objs[i]
                                    name = (o.get("name") or "").lower()
                                    if name.startswith("agent_run_"):
                                        target_idx = i
                                        break
                                if target_idx is None and root_indices:
                                    target_idx = root_indices[0]
                                # Перезаписываем файл с добавленным событием
                                with open(tmp_path, 'w', encoding='utf-8') as fw:
                                    for i, o in enumerate(objs):
                                        if isinstance(o, dict) and target_idx is not None and i == target_idx:
                                            events = o.get("events") or []
                                            events = [e for e in events if (e.get("name") or "").lower() != "report_generated"]
                                            events.append({
                                                "name": "report_generated",
                                                "attributes": {
                                                    "report.mime_type": "text/html",
                                                    "report.filename": f"interactive_plots_{session_id}.html",
                                                    "report.generated_at": datetime.now().isoformat(),
                                                    "report.size_bytes": len(html_content.encode('utf-8')),
                                                    "report.session_id": session_id,
                                                    "report.content_b64_gzip": b64
                                                }
                                            })
                                            o["events"] = events
                                            fw.write(json.dumps(o, ensure_ascii=False) + "\n")
                                        else:
                                            if isinstance(o, dict):
                                                fw.write(json.dumps(o, ensure_ascii=False) + "\n")
                                            else:
                                                fw.write(o)
                                tmp_path.replace(jsonl_path)

                                st.success("Отчёт сформирован и сохранён в трассе")
                                saved_report_html = html_content
                                # Сохраняем в session_state, чтобы скрыть кнопку после генерации
                                st.session_state[f"report_generated_{run_id}"] = True
                                st.rerun()  # Перезагружаем для обновления UI
                            except Exception as viz_e:
                                st.error(f"Не удалось сформировать/сохранить отчёт: {viz_e}")
                    except Exception:
                        pass

                    # Если есть сохранённый отчёт — показываем его и даём скачать
                    if saved_report_html:
                        with st.expander("📄 Отчёт", expanded=True):
                            components.html(saved_report_html, height=800, scrolling=True)
                            st.download_button(
                                label="⬇️ Скачать отчёт",
                                data=saved_report_html,
                                file_name=f"interactive_plots_{run_id}.html",
                                mime="text/html"
                            )
                    # Копирование ответа
                    with st.expander("📋 Скопировать ответ", expanded=False):
                        # Для копирования используем content если есть, иначе весь ответ
                        copy_text = final_answer
                        try:
                            import json
                            if isinstance(final_answer, str):
                                parsed = json.loads(final_answer)
                                if isinstance(parsed, dict) and "content" in parsed:
                                    copy_text = parsed["content"]
                        except:
                            pass
                        st.text_area("Ответ для копирования", value=str(copy_text), height=120, label_visibility="collapsed")
                else:
                    st.caption("Ответ не извлечён из трассы/логов. Откройте детальный просмотр или события спанов.")
        except Exception:
            st.caption("Не удалось извлечь ответ")
        
        try:
            # Загружаем содержимое трассы
            trace_content = telemetry_manager.load_trace_file(run_id)
            spans = trace_content.get("spans", [])
            
            if not spans:
                st.warning("⚠️ Трасса пуста или содержимое недоступно")
                return
            
            # Расширенная статистика
            col1, col2, col3, col4 = st.columns(4)
            
            with col1:
                st.metric("📊 Спанов", len(spans))
                
                # Статистика по типам спанов
                root_spans = [s for s in spans if not s.get("parent_span_id")]
                nested_spans = len(spans) - len(root_spans)
                st.caption(f"Корневых: {len(root_spans)}, Вложенных: {nested_spans}")
                
                # Проверяем, есть ли незавершенные спаны
                incomplete_spans = [s for s in spans if not s.get("end_time_unix_nano")]
                if incomplete_spans:
                    st.warning(f"⏳ {len(incomplete_spans)} спанов выполняется...")
                    # Автообновление для активных трасс
                    if st.button("🔄 Обновить", help="Обновить данные трассы"):
                        st.rerun()
            
            with col2:
                # Определяем статус по корневому спану(ам) запуска
                trace_status = get_trace_status(spans)
                error_spans = [s for s in spans if s.get("status", {}).get("status_code") == "ERROR"]
                if trace_status["has_errors"]:
                    st.error(f"❌ Ошибки: {trace_status['error_reason']}")
                elif trace_status["is_completed"]:
                    st.success("✅ Завершена")
                else:
                    active_count = len([s for s in spans if not s.get("end_time_unix_nano")])
                    st.info(f"🔄 В процессе ({active_count} активных)")
                
                st.metric("📁 Размер файла", f"{trace_file.get('size_bytes', 0)} байт")
            
            with col3:
                # Улучшенная информация о длительности
                if spans:
                    start_times = [span.get("start_time_unix_nano", 0) for span in spans if span.get("start_time_unix_nano")]
                    end_times = [span.get("end_time_unix_nano", 0) for span in spans if span.get("end_time_unix_nano")]
                    
                    if start_times and end_times:
                        duration_ns = max(end_times) - min(start_times)
                        duration_ms = duration_ns / 1_000_000
                        
                        if duration_ms > 1000:
                            st.metric("⏱️ Длительность", f"{duration_ms/1000:.2f}s")
                        else:
                            st.metric("⏱️ Длительность", f"{duration_ms:.1f}ms")
                        
                        # Средняя длительность спана
                        avg_span_duration = duration_ms / len(spans)
                        st.caption(f"Среднее на спан: {avg_span_duration:.1f}ms")
            
            with col4:
                # Основные операции и агенты
                operations = set()
                agents = set()
                
                for span in spans:
                    # Собираем операции
                    span_name = span.get("name", "")
                    if span_name:
                        operations.add(span_name.split()[0] if span_name else "Unknown")
                    
                    # Собираем агентов
                    attributes = span.get("attributes", {})
                    agent_name = attributes.get("agent_name")
                    if agent_name:
                        agents.add(agent_name)
                
                if agents:
                    st.info(f"**Агенты:** {', '.join(list(agents)[:3])}")
                    if len(agents) > 3:
                        st.caption(f"и еще {len(agents)-3}...")
                
                if operations:
                    main_ops = list(operations)[:2]
                    st.info(f"**Операции:** {', '.join(main_ops)}")
            
            # Детальный анализ спанов
            if st.button(f"🔍 Анализ спанов", key=f"analyze_{run_id}"):
                st.session_state["active_trace_run_id"] = run_id
                show_trace_span_analysis(trace_content, run_id)
            
            # Детальный просмотр, копирование и отмена
            col1, col2, col3, col4 = st.columns(4)
            
            with col1:
                # Переключатель детального просмотра с сохранением состояния
                if not st.session_state.get(detail_flag_key, False):
                    if st.button(f"🔍 Детальный просмотр", key=f"detail_{run_id}"):
                        st.session_state[detail_flag_key] = True
                        st.session_state["active_trace_run_id"] = run_id
                else:
                    if st.button(f"⬅️ Скрыть детали", key=f"hide_{run_id}"):
                        st.session_state[detail_flag_key] = False
                        st.session_state["active_trace_run_id"] = run_id
            
            with col2:
                if st.button(f"📋 Скопировать Run ID", key=f"copy_{run_id}"):
                    st.session_state["active_trace_run_id"] = run_id
                    st.code(run_id)
                # Быстрая ссылка с run_id в query params
                try:
                    base_url = dict(st.query_params)
                    base_url = {k: v for k, v in base_url.items() if k != "run_id"}
                except Exception:
                    base_url = {}
                link_params = dict(base_url)
                link_params["run_id"] = run_id
                if st.button("🔗 Ссылка на эту трассу", key=f"link_{run_id}"):
                    try:
                        # Очистить предыдущий run_id и задать новый
                        if "run_id" in st.query_params:
                            del st.query_params["run_id"]
                        for k, v in link_params.items():
                            st.query_params[k] = v
                        st.info("Ссылка обновлена с run_id в параметрах URL")
                    except Exception:
                        st.code(run_id)
            
            with col3:
                if error_spans and st.button(f"❌ Показать ошибки", key=f"errors_{run_id}"):
                    st.session_state["active_trace_run_id"] = run_id
                    show_trace_errors(error_spans)
            
            with col4:
                # Кнопка отмены для активной трассы (не завершена и без ошибки)
                try:
                    if not trace_status.get("is_completed") and trace_status.get("status") != "error":
                        if st.button("⏹️ Отменить", key=f"trace_card_cancel_{run_id}"):
                            cancelled = False
                            try:
                                from workflow.streamlit_api import WorkflowManager
                                wf_manager = WorkflowManager()
                                cancelled = wf_manager.cancel_workflow(run_id) or cancelled
                            except Exception:
                                pass
                            try:
                                from agent_streamlit_api import AgentManager
                                agent_manager = AgentManager()
                                cancelled = agent_manager.cancel_agent_run(run_id) or cancelled
                            except Exception:
                                pass
                            if cancelled:
                                st.success("✅ Отменено")
                                st.rerun()
                            else:
                                st.warning("Не удалось отменить")
                except Exception:
                    pass
            
            # Рендер детального просмотра, если включен флаг
            if st.session_state.get(detail_flag_key, False):
                show_detailed_trace(telemetry_manager, run_id, trace_content)
        
        except Exception as e:
            st.error(f"❌ Ошибка загрузки трассы: {e}")

def show_trace_span_analysis(trace_content, run_id):
    """Детальный анализ спанов трассы"""
    
    st.markdown(f"#### 📊 Анализ спанов для трассы: {run_id[:12]}...")
    
    spans = trace_content.get("spans", [])
    
    if not spans:
        st.warning("Нет спанов для анализа")
        return
    
    # Анализ по типам операций
    operation_stats = {}
    duration_stats = {}
    
    for span in spans:
        span_name = span.get("name", "Unknown")
        operation_type = span_name.split()[0] if span_name else "Unknown"
        
        operation_stats[operation_type] = operation_stats.get(operation_type, 0) + 1
        
        # Считаем длительность
        start_time = span.get("start_time_unix_nano", 0)
        end_time = span.get("end_time_unix_nano", 0)
        if start_time and end_time:
            duration_ms = (end_time - start_time) / 1_000_000
            if operation_type not in duration_stats:
                duration_stats[operation_type] = []
            duration_stats[operation_type].append(duration_ms)
    
    # Отображение статистики
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("**📊 Количество операций:**")
        for op_type, count in sorted(operation_stats.items(), key=lambda x: x[1], reverse=True):
            st.markdown(f"- **{op_type}**: {count} спанов")
    
    with col2:
        st.markdown("**⏱️ Средняя длительность:**")
        for op_type, durations in duration_stats.items():
            if durations:
                avg_duration = sum(durations) / len(durations)
                max_duration = max(durations)
                st.markdown(f"- **{op_type}**: {avg_duration:.1f}ms (макс: {max_duration:.1f}ms)")

def show_trace_errors(error_spans):
    """Отображение ошибок из спанов"""
    
    st.markdown("#### ❌ Ошибки в трассе")
    
    for i, span in enumerate(error_spans, 1):
        status = span.get("status", {})
        error_message = status.get("message", "Неизвестная ошибка")
        span_name = span.get("name", "Unknown span")
        
        with st.expander(f"Ошибка #{i}: {span_name}", expanded=True):
            st.error(f"**Сообщение:** {error_message}")
            
            # Дополнительная информация
            attributes = span.get("attributes", {})
            if attributes:
                st.markdown("**Атрибуты:**")
                for key, value in list(attributes.items())[:5]:  # Показываем первые 5
                    st.markdown(f"- {key}: {value}")

def _get_raw_result_for_report(telemetry_manager, run_id: str):
    """Извлекает сырой результат для создания отчёта (без форматирования)."""
    try:
        import os
        import glob
        import json
        
        # Пытаемся найти JSONL файл трассы
        trace_file_path = os.path.join("logs/traces", f"{run_id}.jsonl")
        if not os.path.exists(trace_file_path):
            pattern = os.path.join("logs/traces", f"*{run_id}*.jsonl")
            files = glob.glob(pattern)
            if files:
                trace_file_path = files[0]
            else:
                trace_file_path = None
        
        spans = []
        
        # Читаем JSONL файл напрямую (если найден)
        if trace_file_path and os.path.exists(trace_file_path):
            with open(trace_file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            span = json.loads(line)
                            spans.append(span)
                        except:
                            continue
        else:
            # Fallback к telemetry_manager
            trace = telemetry_manager.load_trace_file(run_id)
            spans = trace.get("spans", [])
        
        # Определяем тип запуска по корневому span
        run_type = "unknown"
        root_span = None
        
        for span in spans:
            if not span.get("parent_span_id"):
                root_span = span
                span_name = span.get("name", "").lower()
                
                if "workflowengine" in span_name:
                    run_type = "workflow"
                elif "agent_run_" in span_name:
                    run_type = "agent"
                elif "tool" in span_name:
                    run_type = "tool"
                break
        
        # Для workflow запусков: ищем сырой результат от FinalAggregator
        if run_type == "workflow" and root_span:
            attrs = root_span.get("attributes", {}) or {}
            
            for result_key in ["output.value", "final_output", "result", "workflow_result"]:
                result_value = attrs.get(result_key)
                if result_value:
                    try:
                        if isinstance(result_value, str):
                            parsed = json.loads(result_value)
                        else:
                            parsed = result_value
                        
                        # Если это workflow результат, извлекаем сырой контент
                        if isinstance(parsed, dict) and parsed.get("type") == "workflow_result":
                            outputs = parsed.get("outputs", {})
                            if outputs:
                                # Берем последний результат (сырой)
                                last_step_key = max(outputs.keys()) if outputs else None
                                if last_step_key:
                                    return outputs[last_step_key]  # Возвращаем сырой результат
                    except (json.JSONDecodeError, TypeError):
                        # Если не JSON, возвращаем как есть
                        return result_value
        
        # Для всех остальных типов: общий поиск
        for span in reversed(spans):
            attrs = span.get("attributes", {}) or {}
            
            for result_key in ["output.value", "final_output", "result", "answer"]:
                result_value = attrs.get(result_key)
                if result_value:
                    try:
                        if isinstance(result_value, str):
                            parsed = json.loads(result_value)
                        else:
                            parsed = result_value
                        
                        # Если это workflow результат, извлекаем сырой контент
                        if isinstance(parsed, dict) and parsed.get("type") == "workflow_result":
                            outputs = parsed.get("outputs", {})
                            if outputs:
                                last_step_key = max(outputs.keys()) if outputs else None
                                if last_step_key:
                                    return outputs[last_step_key]
                    except (json.JSONDecodeError, TypeError):
                        # Если не JSON, возвращаем как есть
                        return result_value
        
        return None
    except Exception:
        return None

def _get_final_answer_for_run(telemetry_manager, run_id: str):
    """Пытается извлечь финальный ответ пользователя из трассы/логов (эвристика)."""
    try:
        import os
        import glob
        import json
        
        # Пытаемся найти JSONL файл трассы
        trace_file_path = os.path.join("logs/traces", f"{run_id}.jsonl")
        if not os.path.exists(trace_file_path):
            # Ищем любой файл с этим run_id в названии
            pattern = os.path.join("logs/traces", f"*{run_id}*.jsonl")
            files = glob.glob(pattern)
            if files:
                trace_file_path = files[0]
            else:
                trace_file_path = None
        
        spans = []
        
        # Читаем JSONL файл напрямую (если найден)
        if trace_file_path and os.path.exists(trace_file_path):
            with open(trace_file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            span = json.loads(line)
                            spans.append(span)
                        except:
                            continue
        else:
            # Fallback к telemetry_manager
            trace = telemetry_manager.load_trace_file(run_id)
            spans = trace.get("spans", [])
        
        # НОВАЯ ЛОГИКА: Сначала определяем ТИП запуска по корневому span
        run_type = "unknown"
        root_span = None
        
        # Находим корневой span (без parent_span_id)
        for span in spans:
            if not span.get("parent_span_id"):
                root_span = span
                span_name = span.get("name", "").lower()
                
                if "workflowengine" in span_name:
                    run_type = "workflow"
                elif "finalanswertool" in span_name:
                    run_type = "agent"
                elif "agent_run_" in span_name:
                    run_type = "agent"
                elif "tool" in span_name:
                    run_type = "tool"
                break
        
        # Теперь ищем результат В ЗАВИСИМОСТИ ОТ ТИПА
        if run_type == "workflow":
            # ДЛЯ WORKFLOW: Ищем результат от FinalAggregator в корневом span
            if root_span:
                attrs = root_span.get("attributes", {}) or {}
                
                # Проверяем все возможные ключи в корневом span
                for result_key in ["output.value", "final_output", "result", "workflow_result"]:
                    result_value = attrs.get(result_key)
                    if result_value:
                        try:
                            # Пытаемся парсить как JSON
                            if isinstance(result_value, str):
                                parsed = json.loads(result_value)
                            else:
                                parsed = result_value
                            
                            # Проверяем, это ли workflow результат от FinalAggregator
                            if isinstance(parsed, dict) and parsed.get("type") == "workflow_result":
                                # Извлекаем основной результат из последнего шага
                                outputs = parsed.get("outputs", {})
                                if outputs:
                                    # Находим последний шаг по execution_path
                                    execution_path = parsed.get("execution_path", [])
                                    if execution_path:
                                        last_step_id = execution_path[-1].get("step_id")
                                        if last_step_id and last_step_id in outputs:
                                            last_result = outputs[last_step_id]
                                            # Извлекаем только значение output
                                            output_value = last_result.get("output", last_result)
                                            return f"📊 Workflow результат: {parsed.get('workflow_name', 'Unknown')}\n\n{output_value}\n\n✅ {parsed.get('summary', 'N/A')}"
                                    
                                    # Fallback: берем любой результат
                                    first_key = next(iter(outputs.keys()))
                                    first_result = outputs[first_key]
                                    output_value = first_result.get("output", first_result)
                                    return f"📊 Workflow результат: {parsed.get('workflow_name', 'Unknown')}\n\n{output_value}"
                                
                                # Если нет outputs, возвращаем общую информацию
                                return f"📊 Workflow '{parsed.get('workflow_name', 'Unknown')}' завершён\nШагов выполнено: {len(outputs)}"
                        except (json.JSONDecodeError, TypeError):
                            # Если не JSON, возвращаем как есть
                            if result_value:
                                return result_value
                            continue
            
            # Если в корневом span ничего нет, ищем в дочерних spans
            # (это для старых workflow или если агрегатор сохраняет в отдельном span)
            for span in reversed(spans):
                attrs = span.get("attributes", {}) or {}
                span_name = span.get("name", "").lower()
                
                # Ищем span от FinalAggregator или с workflow результатом
                if "aggregator" in span_name or "final" in span_name:
                    for result_key in ["output.value", "final_output", "result"]:
                        result_value = attrs.get(result_key)
                        if result_value:
                            try:
                                if isinstance(result_value, str):
                                    parsed = json.loads(result_value)
                                else:
                                    parsed = result_value
                                
                                if isinstance(parsed, dict) and parsed.get("type") == "workflow_result":
                                    outputs = parsed.get("outputs", {})
                                    if outputs:
                                        # Находим последний шаг по execution_path
                                        execution_path = parsed.get("execution_path", [])
                                        if execution_path:
                                            last_step_id = execution_path[-1].get("step_id")
                                            if last_step_id and last_step_id in outputs:
                                                last_result = outputs[last_step_id]
                                                # Извлекаем только значение output
                                                output_value = last_result.get("output", last_result)
                                                return f"📊 Workflow результат: {parsed.get('workflow_name', 'Unknown')}\n\n{output_value}\n\n✅ {parsed.get('summary', 'N/A')}"
                                        
                                        # Fallback: берем любой результат
                                        first_key = next(iter(outputs.keys()))
                                        first_result = outputs[first_key]
                                        output_value = first_result.get("output", first_result)
                                        return f"📊 Workflow результат: {parsed.get('workflow_name', 'Unknown')}\n\n{output_value}"
                                    return f"📊 Workflow '{parsed.get('workflow_name', 'Unknown')}' завершён\nШагов выполнено: {len(outputs)}"
                            except (json.JSONDecodeError, TypeError):
                                if result_value:
                                    return result_value
                                continue
        
        elif run_type == "agent":
            # ДЛЯ АГЕНТОВ: Ищем FinalAnswerTool spans и LLM final_answer calls
            
            # 1. Ищем FinalAnswerTool spans
            for span in reversed(spans):
                if span.get("name") == "FinalAnswerTool":
                    attrs = span.get("attributes", {})
                    input_value = attrs.get("input.value")
                    if input_value:
                        try:
                            parsed = json.loads(input_value)
                            answer = parsed.get("kwargs", {}).get("answer")
                            if answer:
                                return answer
                        except:
                            pass
            
            # 2. Ищем в LLM spans с final_answer tool calls
            for span in reversed(spans):
                if "generate" in span.get("name", "").lower():
                    attrs = span.get("attributes", {})
                    output_value = attrs.get("output.value")
                    if output_value:
                        try:
                            parsed = json.loads(output_value)
                            tool_calls = parsed.get("tool_calls", [])
                            for tool_call in tool_calls:
                                func = tool_call.get("function", {})
                                if func.get("name") == "final_answer":
                                    args = func.get("arguments")
                                    if args:
                                        try:
                                            args_parsed = json.loads(args) if isinstance(args, str) else args
                                            answer = args_parsed.get("answer")
                                            if answer:
                                                return answer
                                        except:
                                            pass
                        except:
                            pass
        
        # FALLBACK: Универсальный поиск для остальных типов или если ничего не найдено
        for span in reversed(spans):
            attrs = span.get("attributes", {}) or {}
            
            # Проверяем все возможные ключи с результатами
            for result_key in ["output.value", "final_output", "result", "answer"]:
                result_value = attrs.get(result_key)
                if result_value:
                    return result_value
        
        # 4. Ищем спаны с финальным выводом (старая логика для пайплайнов)
        for span in reversed(spans):
            name = (span.get("name") or "").lower()
            if any(k in name for k in ["final", "answer", "summary", "result"]):
                attrs = span.get("attributes", {}) or {}
                for key in ["final_output", "result", "answer", "summary"]:
                    if key in attrs and attrs[key]:
                        return attrs[key]
                for ev in span.get("events", []) or []:
                    ev_attrs = ev.get("attributes", {}) or {}
                    for key in ["final_output", "result", "answer", "summary"]:
                        if key in ev_attrs and ev_attrs[key]:
                            return ev_attrs[key]
        
        # 5. Ищем в пер‑run логах
        try:
            from unified_logging import get_logging_manager
            lm = get_logging_manager()
            logs = lm.get_run_logs(run_id, limit=500)
            for rec in reversed(logs):
                msg = (rec.message or "").lower()
                if any(w in msg for w in ["final", "answer", "result"]):
                    return rec.message
        except Exception:
            pass
    except Exception:
        return None
    return None

def show_trace_summary(telemetry_manager, trace_file):
    """Отображение краткой информации о трассе (оригинальная функция для совместимости)"""
    
    return show_trace_summary_enhanced(telemetry_manager, trace_file)

def show_trace_summary_original(telemetry_manager, trace_file):
    """Оригинальная функция отображения краткой информации о трассе"""
    
    run_id = trace_file["run_id"]
    
    with st.expander(f"🔍 {run_id} ({trace_file['modified_time'].strftime('%H:%M:%S')})", expanded=False):
        # Показ финального ответа ТОЛЬКО для завершённых трасс
        try:
            trace_check = telemetry_manager.load_trace_file(run_id)
            spans_check = trace_check.get("spans", [])
            trace_status = get_trace_status(spans_check)

            if trace_status["is_completed"]:
                if trace_status.get("has_errors"):
                    st.warning(f"⚠️ Обнаружены ошибки, но трасса завершена: {trace_status['error_reason']}")
                final_answer = _get_final_answer_for_run(telemetry_manager, run_id)
                if final_answer:
                    st.markdown("### 🟢 Финальный ответ")
                    # В кратком виде сырой ответ не показываем; только последующие действия (отчёт)
                    # Кнопка формирования отчёта
                    try:
                        # Для краткого вида также учитываем сохранённость отчёта и скрываем кнопку
                        saved_report_html_summary = None
                        try:
                            root_spans = [s for s in spans_check if not s.get("parent_span_id")]
                            for root in root_spans:
                                for ev in (root.get("events") or []):
                                    if (ev.get("name") or "").lower() == "report_generated":
                                        attrs = ev.get("attributes", {}) or {}
                                        b64 = attrs.get("report.content_b64_gzip") or attrs.get("report_b64_gzip")
                                        if b64:
                                            try:
                                                saved_report_html_summary = gzip.decompress(base64.b64decode(b64)).decode("utf-8", errors="replace")
                                                break
                                            except Exception:
                                                pass
                                if saved_report_html_summary:
                                    break
                        except Exception:
                            pass

                        # Проверяем как сохранённый отчёт, так и флаг в session_state
                        report_already_generated_summary = saved_report_html_summary or st.session_state.get(f"report_generated_{run_id}", False)
                        if not report_already_generated_summary and st.button("🧾 Сформировать отчёт", key=f"make_report_summary_{run_id}"):
                            # Извлекаем session_id
                            session_id = None
                            for s in spans_check:
                                attrs = s.get("attributes", {}) or {}
                                for k in ("session_id", "session.id", "sessionId", "session", "run_id", "run.id"):
                                    if attrs.get(k):
                                        session_id = attrs.get(k)
                                        break
                                if session_id:
                                    break
                            if not session_id:
                                session_id = run_id
                            # Формируем тело отчёта - используем специальную функцию для сырого результата
                            report_text = None
                            try:
                                # Пытаемся получить сырой результат (без форматирования) для качественного отчёта
                                raw_result = _get_raw_result_for_report(telemetry_manager, run_id)
                                if raw_result:
                                    report_text = str(raw_result)
                                else:
                                    # Fallback к обычному final_answer с очисткой форматирования
                                    if isinstance(final_answer, str):
                                        # Проверяем, это ли форматированный workflow результат
                                        if final_answer.startswith("📊 Workflow результат:"):
                                            # Извлекаем чистый контент без эмодзи и форматирования
                                            lines = final_answer.split('\n')
                                            # Ищем строку "Основной результат:" и берём всё после неё
                                            content_start = False
                                            clean_content = []
                                            for line in lines:
                                                if line.strip() == "Основной результат:":
                                                    content_start = True
                                                    continue
                                                if content_start:
                                                    clean_content.append(line)
                                            report_text = '\n'.join(clean_content).strip()
                                            
                                            # Если не нашли основной результат, используем всё как есть
                                            if not report_text:
                                                report_text = final_answer
                                        else:
                                            report_text = final_answer
                                    elif isinstance(final_answer, dict) and "content" in final_answer:
                                        report_text = str(final_answer.get("content"))
                                    else:
                                        report_text = str(final_answer)
                            except Exception:
                                report_text = str(final_answer)
                            # Вызов визуализатора
                            try:
                                path_to_html = html_visualizer.advanced_visualization(report_text, session_id, show=True)
                                with open(path_to_html, 'r', encoding='utf-8') as f:
                                    html_content = f.read()
                                gz = gzip.compress(html_content.encode('utf-8'))
                                b64 = base64.b64encode(gz).decode('ascii')

                                traces_dir = Path(project_root) / "logs/traces"
                                jsonl_path = traces_dir / f"{run_id}.jsonl"
                                tmp_path = traces_dir / f"{run_id}.jsonl.tmp"
                                lines = []
                                if jsonl_path.exists():
                                    with open(jsonl_path, 'r', encoding='utf-8') as fr:
                                        lines = fr.readlines()
                                # Разбираем и выбираем целевой корневой (agent_run_*)
                                objs = []
                                for line in lines:
                                    try:
                                        objs.append(json.loads(line))
                                    except Exception:
                                        objs.append(line)
                                root_indices = [i for i, o in enumerate(objs) if isinstance(o, dict) and not o.get("parent_span_id")]
                                target_idx = None
                                for i in root_indices:
                                    o = objs[i]
                                    name = (o.get("name") or "").lower()
                                    if name.startswith("agent_run_"):
                                        target_idx = i
                                        break
                                if target_idx is None and root_indices:
                                    target_idx = root_indices[0]
                                with open(tmp_path, 'w', encoding='utf-8') as fw:
                                    for i, o in enumerate(objs):
                                        if isinstance(o, dict) and target_idx is not None and i == target_idx:
                                            events = o.get("events") or []
                                            events = [e for e in events if (e.get("name") or "").lower() != "report_generated"]
                                            events.append({
                                                "name": "report_generated",
                                                "attributes": {
                                                    "report.mime_type": "text/html",
                                                    "report.filename": f"interactive_plots_{session_id}.html",
                                                    "report.generated_at": datetime.now().isoformat(),
                                                    "report.size_bytes": len(html_content.encode('utf-8')),
                                                    "report.session_id": session_id,
                                                    "report.content_b64_gzip": b64
                                                }
                                            })
                                            o["events"] = events
                                            fw.write(json.dumps(o, ensure_ascii=False) + "\n")
                                        else:
                                            if isinstance(o, dict):
                                                fw.write(json.dumps(o, ensure_ascii=False) + "\n")
                                            else:
                                                fw.write(o)
                                tmp_path.replace(jsonl_path)

                                st.success("Отчёт сформирован и сохранён в трассе")
                                saved_report_html_summary = html_content
                                # Сохраняем в session_state, чтобы скрыть кнопку после генерации
                                st.session_state[f"report_generated_{run_id}"] = True
                                st.rerun()  # Перезагружаем для обновления UI
                            except Exception as viz_e:
                                st.error(f"Не удалось сформировать/сохранить отчёт: {viz_e}")
                    except Exception:
                        pass

                    # Если отчёт был ранее сохранён — показать
                    try:
                        if saved_report_html_summary:
                            with st.expander("📄 Отчёт", expanded=True):
                                components.html(saved_report_html_summary, height=800, scrolling=True)
                                st.download_button(
                                    label="⬇️ Скачать отчёт",
                                    data=saved_report_html_summary,
                                    file_name=f"interactive_plots_{run_id}.html",
                                    mime="text/html"
                                )
                    except Exception:
                        pass
        except Exception:
            pass
        
        try:
            # Загружаем содержимое трассы
            trace_content = telemetry_manager.load_trace_file(run_id)
            spans = trace_content.get("spans", [])
            
            if not spans:
                st.warning("⚠️ Трасса пуста или содержимое недоступно")
                return
            
            # Основная информация
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric("📊 Спанов", len(spans))
                st.metric("📁 Размер файла", f"{trace_file['size_bytes']} байт")
            
            with col2:
                # Определяем статус
                has_errors = any(
                    span.get("status", {}).get("status_code") == "ERROR"
                    for span in spans
                )
                
                if has_errors:
                    st.error("❌ Есть ошибки")
                else:
                    st.success("✅ Без ошибок")
                
                # Продолжительность
                if spans:
                    start_times = [span.get("start_time_unix_nano", 0) for span in spans if span.get("start_time_unix_nano")]
                    end_times = [span.get("end_time_unix_nano", 0) for span in spans if span.get("end_time_unix_nano")]
                    
                    if start_times and end_times:
                        duration_ns = max(end_times) - min(start_times)
                        duration_ms = duration_ns / 1_000_000
                        st.metric("⏱️ Длительность", f"{duration_ms:.1f}ms")
            
            with col3:
                # Основной агент/workflow
                root_spans = [span for span in spans if not span.get("parent_span_id")]
                if root_spans:
                    main_operation = root_spans[0].get("name", "Unknown")
                    st.info(f"**Операция:** {main_operation}")
                
                # Атрибуты
                for span in spans[:1]:  # Берем первый спан
                    attributes = span.get("attributes", {})
                    if "agent_name" in attributes:
                        st.info(f"**Агент:** {attributes['agent_name']}")
                    if "session_id" in attributes:
                        st.info(f"**Сессия:** {attributes['session_id']}")
            
            # Детальный просмотр
            col1, col2 = st.columns(2)
            
            with col1:
                if st.button(f"🔍 Детальный просмотр", key=f"detail_{run_id}"):
                    show_detailed_trace(telemetry_manager, run_id, trace_content)
            
            with col2:
                if st.button(f"📋 Скопировать Run ID", key=f"copy_{run_id}"):
                    st.code(run_id)
        
        except Exception as e:
            st.error(f"❌ Ошибка загрузки трассы: {e}")

def show_detailed_trace(telemetry_manager, run_id, trace_content):
    """Детальное отображение трассы"""
    
    spans = trace_content.get("spans", [])
    
    # Заголовок с информацией о статусе трассы
    trace_status = get_trace_status(spans)
    if trace_status["status"] == "error":
        st.markdown(f"#### 🔍 Детальный просмотр трассы: {run_id} ❌ (с ошибкой)")
        st.error(f"Причина ошибки: {trace_status['error_reason']}")
    elif trace_status["is_completed"]:
        st.markdown(f"#### 🔍 Детальный просмотр трассы: {run_id} ✅ (завершена)")
    else:
        active_count = len([s for s in spans if not s.get("end_time_unix_nano")])
        st.markdown(f"#### 🔍 Детальный просмотр трассы: {run_id} ⏳ (активная)")
        if active_count:
            st.info(f"🔄 Трасса активна: {active_count} спанов выполняется. Обновите страницу для актуальных данных.")
    
    if not spans:
        st.warning("⚠️ Нет спанов в трассе")
        return
    
    # Вкладки для разных представлений
    detail_tab1, detail_tab2, detail_tab3 = st.tabs(["🌳 Дерево спанов", "📊 Таблица", "📋 JSON"])
    
    with detail_tab1:
        # Две колонки: слева дерево, справа детали выбранного спана
        left_col, right_col = st.columns([0.45, 0.55])
        # Кнопка отмены рядом с заголовком для активной трассы
        try:
            if not trace_status["is_completed"] and trace_status["status"] != "error":
                cancel_col1, cancel_col2 = st.columns([0.85, 0.15])
                with cancel_col2:
                    if st.button("⏹️ Отменить", key=f"trace_cancel_{run_id}"):
                        # Пробуем отменить как workflow, затем как агента
                        try:
                            from workflow.streamlit_api import WorkflowManager
                            wf_manager = WorkflowManager()
                            if wf_manager.cancel_workflow(run_id):
                                st.success("✅ Пайплайн отменен")
                                st.rerun()
                        except Exception:
                            pass
                        try:
                            from agent_streamlit_api import AgentManager
                            agent_manager = AgentManager()
                            if agent_manager.cancel_agent_run(run_id):
                                st.success("✅ Агент отменен")
                                st.rerun()
                        except Exception:
                            pass
        except Exception:
            pass
        with left_col:
            show_span_tree_interactive(spans, run_id)
        with right_col:
            show_selected_span_details(spans, run_id)
    
    with detail_tab2:
        show_span_table(spans)
    
    with detail_tab3:
        if isinstance(trace_content, (dict, list)):
            try:
                st.json(trace_content)
            except Exception as e:
                st.error(f"Ошибка отображения JSON: {e}")
                st.code(str(trace_content), language="text")
        else:
            st.code(str(trace_content), language="text")

def show_span_tree_interactive(spans, run_id: str):
    """Интерактивное дерево спанов с раскрытием узлов и выбором спана."""
    # Сортировка спанов по времени
    spans_sorted = sorted(spans, key=lambda s: s.get("start_time_unix_nano", 0))
    span_by_id = {s["span_id"]: s for s in spans_sorted}
    children = {}
    parents = {}
    for s in spans_sorted:
        pid = s.get("parent_span_id")
        if pid:
            children.setdefault(pid, []).append(s)
            parents[s["span_id"]] = pid
    # Корнями считаем как спаны без parent_span_id, так и спаны,
    # у которых parent_span_id отсутствует среди span_id (осиротевшие спаны)
    roots = [
        s for s in spans_sorted
        if not s.get("parent_span_id") or s.get("parent_span_id") not in span_by_id
    ]

    # Выбранный спан и состояние раскрытых узлов храним в session_state
    sel_key = f"selected_span_id_{run_id}"
    exp_key = f"expanded_span_ids_{run_id}"
    selected_span_id = st.session_state.get(sel_key, roots[0]["span_id"] if roots else None)
    if exp_key not in st.session_state:
        # По умолчанию раскрываем корневые узлы
        st.session_state[exp_key] = [r["span_id"] for r in roots]
    expanded_ids = set(st.session_state.get(exp_key, []))

    # Убедимся, что путь к выбранному всегда раскрыт (добавляем, но не очищаем предыдущие)
    cur = selected_span_id
    while cur in parents:
        expanded_ids.add(parents[cur])
        cur = parents[cur]
    # Сохраняем обновлённый набор раскрытых узлов
    st.session_state[exp_key] = list(expanded_ids)

    def duration_ms(span):
        start = span.get("start_time_unix_nano", 0)
        end = span.get("end_time_unix_nano", 0)
        return (end - start) / 1_000_000 if end and start and end > start else 0.0

    def node_label(span):
        # Делаем метку стабильной между перерисовками (не включаем меняющиеся значения)
        name = span.get("name", "Unknown")
        short_id = span.get("span_id", "")[:6]
        return f"{name} · {short_id}"

    def render_node(span, level: int = 0):
        span_id = span["span_id"]
        is_expanded = (span_id in expanded_ids) or (level == 0)
        with st.expander(node_label(span), expanded=is_expanded):
            # Только кнопка выбора текущего узла, без дополнительного текста
            if st.button("Выбрать", key=f"select_btn_{run_id}_{span_id}"):
                st.session_state[sel_key] = span_id
                # Закрепляем путь к выбранному и сам узел раскрытыми
                path_ids = []
                cur_id = span_id
                while cur_id in parents:
                    path_ids.append(parents[cur_id])
                    cur_id = parents[cur_id]
                new_expanded = set(st.session_state.get(exp_key, []))
                new_expanded.update(path_ids)
                new_expanded.add(span_id)
                st.session_state[exp_key] = list(new_expanded)
            # Дети
            for child in children.get(span_id, []):
                render_node(child, level + 1)

    for root in roots:
        render_node(root, 0)

def show_selected_span_details(spans, run_id: str):
    """Отображает подробности по выбранному спану справа от дерева с интегрированными логами."""
    sel_key = f"selected_span_id_{run_id}"
    span_by_id = {s["span_id"]: s for s in spans}
    selected_span_id = st.session_state.get(sel_key)
    if not selected_span_id and spans:
        selected_span_id = spans[0]["span_id"]
    if not selected_span_id or selected_span_id not in span_by_id:
        st.info("Выберите спан слева для просмотра деталей")
        return
    
    span = span_by_id[selected_span_id]
    status = span.get("status", {})
    start = span.get("start_time_unix_nano", 0)
    end = span.get("end_time_unix_nano", 0)
    duration = (end - start) / 1_000_000 if end and start and end > start else 0.0

    # Заголовок с переключателем показа логов
    col_title, col_toggle = st.columns([3, 1])
    with col_title:
        st.markdown("### Детали спана")
    with col_toggle:
        show_logs_key = f"show_logs_{run_id}"
        show_logs = st.checkbox("📝 Показать логи", key=show_logs_key, help="Показать связанные логи под деталями спана")
    
    # Метрики спана
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Статус", status.get("status_code", "OK"))
    with col2:
        st.metric("Длительность", f"{duration:.1f} ms")
    with col3:
        st.metric("Span ID", selected_span_id[:8] + "...")

    # Атрибуты спана
    with st.expander("🔖 Атрибуты", expanded=True):
        attributes = span.get("attributes", {})
        if isinstance(attributes, (dict, list)):
            try:
                st.json(attributes)
            except Exception as e:
                st.error(f"Ошибка отображения атрибутов: {e}")
                st.code(str(attributes), language="text")
        else:
            st.code(str(attributes), language="text")
    
    # События спана
    if span.get("events"):
        with st.expander("📋 События"):
            events = span.get("events")
            if isinstance(events, (dict, list)):
                try:
                    st.json(events)
                except Exception as e:
                    st.error(f"Ошибка отображения событий: {e}")
                    st.code(str(events), language="text")
            else:
                st.code(str(events), language="text")
    
    # Интегрированные логи (если включены)
    if show_logs:
        st.markdown("---")
        st.markdown("### 📝 Связанные логи")
        
        try:
            from unified_logging import get_logging_manager
            
            logging_manager = get_logging_manager()
            
            # Получаем логи для этого спана
            span_logs = logging_manager.get_logs_for_span(run_id, selected_span_id)
            
            if span_logs:
                # Фильтры для логов
                col_filter1, col_filter2 = st.columns(2)
                
                with col_filter1:
                    log_level_filter = st.selectbox(
                        "Уровень логов",
                        ["Все", "ERROR", "WARNING", "INFO", "DEBUG"],
                        key=f"span_log_level_{run_id}_{selected_span_id}"
                    )
                
                with col_filter2:
                    log_search = st.text_input(
                        "Поиск в логах",
                        placeholder="Поиск по тексту...",
                        key=f"span_log_search_{run_id}_{selected_span_id}"
                    )
                
                # Применяем фильтры
                filtered_logs = span_logs
                if log_level_filter != "Все":
                    filtered_logs = [log for log in filtered_logs if log.level == log_level_filter]
                
                if log_search:
                    filtered_logs = [log for log in filtered_logs if log_search.lower() in log.message.lower()]
                
                # Сортируем по времени
                filtered_logs.sort(key=lambda x: x.timestamp)
                
                if filtered_logs:
                    st.write(f"**Найдено логов:** {len(filtered_logs)} из {len(span_logs)}")
                    
                    # Отображаем логи в компактном виде
                    for i, log in enumerate(filtered_logs[:50]):  # Ограничиваем количество
                        level_colors = {
                            "ERROR": "#ff4444",
                            "WARNING": "#ffaa00", 
                            "INFO": "#4444ff",
                            "DEBUG": "#888888"
                        }
                        
                        level_icons = {
                            "ERROR": "🔴",
                            "WARNING": "🟡", 
                            "INFO": "🔵",
                            "DEBUG": "⚪"
                        }
                        
                        color = level_colors.get(log.level, "#000000")
                        icon = level_icons.get(log.level, "⚫")
                        
                        # Компактное отображение лога
                        st.markdown(
                            f'<div style="border-left: 3px solid {color}; padding: 5px 10px; margin: 3px 0; '
                            f'background-color: {color}15; border-radius: 3px;">'
                            f'<div style="font-size: 0.8em; color: #666;">'
                            f'{icon} <strong>{log.timestamp.strftime("%H:%M:%S.%f")[:-3]}</strong> '
                            f'[{log.level}] <em>{log.logger_name}</em>'
                            f'</div>'
                            f'<div style="margin-top: 2px;">{log.message}</div>'
                            f'</div>',
                            unsafe_allow_html=True
                        )
                    
                    if len(filtered_logs) > 50:
                        st.info(f"Показаны первые 50 логов из {len(filtered_logs)}. Используйте фильтры для уточнения поиска.")
                
                else:
                    st.info("🔍 Нет логов, соответствующих фильтрам")
            
            else:
                # Объясняем пользователю, почему логи не найдены
                st.info("📭 Для данного спана связанные логи не найдены")
                
                with st.expander("ℹ️ Почему логи могут отсутствовать"):
                    st.markdown("""
                    **Возможные причины:**
                    
                    🔹 **Логи не писались с `run_id`** - используйте `get_run_logger(run_id, logger_name)`
                    
                    🔹 **Логи без `span_id`** - для корреляции нужно логировать внутри OpenTelemetry span контекста
                    
                    🔹 **Старые логи** - корреляция работает только для новых логов с обновленным форматом
                    
                    **Пример правильного логирования:**
                    ```python
                    from unified_logging import get_run_logger
                    from opentelemetry import trace
                    
                    # В контексте span'а
                    with tracer.start_as_current_span("my_operation") as span:
                        logger = get_run_logger(run_id, "my_component")
                        logger.info("Операция выполняется")  # Автоматически связывается со span
                    ```
                    """)
                
                # Показываем возможные логи для этого run_id без span_id
                try:
                    # Попробуем найти хотя бы общие логи для этого run_id
                    correlated_data = logging_manager.get_correlated_logs_and_spans(run_id)
                    uncorrelated_logs = correlated_data.get("uncorrelated_logs", [])
                    
                    if uncorrelated_logs:
                        st.markdown("**📝 Общие логи для этого запуска (без привязки к спанам):**")
                        
                        # Показываем первые несколько общих логов
                        for log in uncorrelated_logs[:5]:
                            level_icons = {"ERROR": "🔴", "WARNING": "🟡", "INFO": "🔵", "DEBUG": "⚪"}
                            icon = level_icons.get(log.level, "⚫")
                            
                            st.markdown(
                                f'<div style="padding: 3px 8px; margin: 2px 0; background-color: #f8f9fa; border-radius: 3px; border-left: 2px solid #ddd;">'
                                f'<div style="font-size: 0.8em; color: #666;">'
                                f'{icon} <strong>{log.timestamp.strftime("%H:%M:%S")}</strong> [{log.level}] <em>{log.logger_name}</em>'
                                f'</div>'
                                f'<div style="margin-top: 1px; color: #333;">{log.message}</div>'
                                f'</div>',
                                unsafe_allow_html=True
                            )
                        
                        if len(uncorrelated_logs) > 5:
                            st.info(f"... и еще {len(uncorrelated_logs) - 5} общих логов (см. вкладку \"Логи системы\")")
                            
                except Exception:
                    pass  # Если не удалось загрузить - не показываем ошибку
        
        except Exception as e:
            st.error(f"❌ Ошибка загрузки логов: {e}")
            with st.expander("Детали ошибки"):
                import traceback
                st.code(traceback.format_exc())

def show_span_table(spans):
    """Отображение спанов в табличном виде"""
    
    # Подготавливаем данные для таблицы
    table_data = []
    
    for span in spans:
        start_time = span.get("start_time_unix_nano", 0)
        end_time = span.get("end_time_unix_nano", 0)
        duration_ms = (end_time - start_time) / 1_000_000 if end_time > start_time else 0
        
        status = span.get("status", {})
        status_text = "ERROR" if status.get("status_code") == "ERROR" else "OK"
        
        attributes = span.get("attributes", {})
        
        table_data.append({
            "Span ID": span["span_id"][:8] + "...",
            "Название": span.get("name", "Unknown"),
            "Статус": status_text,
            "Длительность (ms)": f"{duration_ms:.1f}",
            "Агент": attributes.get("agent_name", ""),
            "Операция": attributes.get("operation_type", ""),
            "Родитель": span.get("parent_span_id", "")[:8] + "..." if span.get("parent_span_id") else "Root"
        })
    
    if table_data:
        df = pd.DataFrame(table_data)
        st.dataframe(df, use_container_width=True)

def cleanup_old_traces(telemetry_manager):
    """Очистка старых трасс"""
    
    st.markdown("### 🧹 Очистка старых трасс")
    
    with st.form("cleanup_form"):
        days_to_keep = st.number_input(
            "Сохранить трассы за последние (дней)",
            min_value=1,
            max_value=365,
            value=7
        )
        
        st.markdown(f"**⚠️ Будут удалены все трассы старше {days_to_keep} дней**")
        
        submitted = st.form_submit_button("🗑️ Подтвердить очистку", type="primary")
        
        if submitted:
            with st.spinner("Очистка старых трасс..."):
                try:
                    removed_count = telemetry_manager.cleanup_old_traces(max_age_days=days_to_keep)
                    st.success(f"✅ Удалено {removed_count} старых файлов трасс")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ Ошибка очистки: {e}")

def export_traces(telemetry_manager, trace_files):
    """Экспорт трасс"""
    
    export_format = st.selectbox(
        "Формат экспорта",
        ["JSON", "CSV сводка", "JSONL"]
    )
    
    if st.button("📥 Экспорт"):
        with st.spinner("Подготовка экспорта..."):
            try:
                if export_format == "JSON":
                    # Собираем все трассы в один JSON
                    all_traces = {}
                    for trace_file in trace_files:
                        trace_content = telemetry_manager.load_trace_file(trace_file["run_id"])
                        all_traces[trace_file["run_id"]] = trace_content
                    
                    export_data = json.dumps(all_traces, indent=2, default=str)
                    filename = f"traces_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                    mime_type = "application/json"
                
                elif export_format == "CSV сводка":
                    # Создаем CSV сводку
                    csv_data = []
                    for trace_file in trace_files:
                        try:
                            trace_content = telemetry_manager.load_trace_file(trace_file["run_id"])
                            spans = trace_content.get("spans", [])
                            
                            has_errors = any(
                                span.get("status", {}).get("status_code") == "ERROR"
                                for span in spans
                            )
                            
                            # Вычисляем общую длительность
                            if spans:
                                start_times = [s.get("start_time_unix_nano", 0) for s in spans if s.get("start_time_unix_nano")]
                                end_times = [s.get("end_time_unix_nano", 0) for s in spans if s.get("end_time_unix_nano")]
                                duration_ms = (max(end_times) - min(start_times)) / 1_000_000 if start_times and end_times else 0
                            else:
                                duration_ms = 0
                            
                            csv_data.append({
                                "run_id": trace_file["run_id"],
                                "timestamp": trace_file["modified_time"],
                                "spans_count": len(spans),
                                "has_errors": has_errors,
                                "duration_ms": duration_ms,
                                "file_size": trace_file["size_bytes"]
                            })
                        except:
                            continue
                    
                    df = pd.DataFrame(csv_data)
                    export_data = df.to_csv(index=False)
                    filename = f"traces_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                    mime_type = "text/csv"
                
                else:  # JSONL
                    # Каждая трасса на отдельной строке
                    jsonl_lines = []
                    for trace_file in trace_files:
                        trace_content = telemetry_manager.load_trace_file(trace_file["run_id"])
                        jsonl_lines.append(json.dumps(trace_content, default=str))
                    
                    export_data = "\n".join(jsonl_lines)
                    filename = f"traces_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
                    mime_type = "application/json"
                
                st.download_button(
                    label=f"💾 Скачать {export_format}",
                    data=export_data,
                    file_name=filename,
                    mime=mime_type
                )
                
                st.success(f"✅ Экспорт готов: {len(trace_files)} трасс")
            
            except Exception as e:
                st.error(f"❌ Ошибка экспорта: {e}")

def show_system_logs():
    """Отображение системных логов"""
    
    st.markdown("## 📝 Системные логи")
    
    # Панель управления обновлением
    ctrl1, ctrl2, ctrl3 = st.columns([1,1,6])
    with ctrl1:
        if st.button("🔄 Обновить логи", key="logs_refresh"):
            st.rerun()
    with ctrl2:
        auto_logs = st.checkbox("Авто", key="logs_auto_refresh", help="Автообновление каждые 5 секунд")
        if auto_logs:
            try:
                import time as _t
                _t.sleep(5)
                st.rerun()
            except Exception:
                pass

    try:
        from unified_logging import get_logging_manager
        
        logging_manager = get_logging_manager()
        
        # Информация о логировании
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.info("📝 **Логирование:** Активно")
            st.info("📁 **Путь:** logs/")
        
        with col2:
            # Получаем файлы логов
            logs_dir = Path(project_root) / "logs"
            log_files = list(logs_dir.glob("*.log")) if logs_dir.exists() else []
            st.metric("📁 Файлов логов", len(log_files))
        
        with col3:
            if log_files:
                total_size = sum(f.stat().st_size for f in log_files) / (1024 * 1024)
                st.metric("💾 Общий размер", f"{total_size:.1f} MB")
        
        # Мульти-поиск по всем логам
        if log_files:
            add_multi_file_log_search()
            
            st.markdown("---")
            
            # Выбор файла лога
            st.markdown("### 📁 Просмотр отдельного файла лога")
            
            selected_log = st.selectbox(
                "Файл лога",
                options=[f.name for f in log_files],
                help="Выберите файл лога для детального просмотра"
            )
            
            if selected_log:
                log_file_path = logs_dir / selected_log
                show_log_file_content(log_file_path)
        else:
            st.info("📭 Файлы логов не найдены")
            st.markdown("Логи будут появляться здесь после запуска агентов и пайплайнов.")
    
    except Exception as e:
        st.error(f"❌ Ошибка загрузки логов: {e}")

def show_log_file_content(log_file_path):
    """Отображение содержимого файла лога"""
    
    try:
        import re
        
        # Расширенные фильтры
        st.markdown("### 🔍 Фильтры и поиск")
        
        # Первый ряд фильтров
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            lines_to_show = st.number_input(
                "Количество строк",
                min_value=10,
                max_value=5000,
                value=500,
                help="Количество последних строк для отображения"
            )
        
        with col2:
            # Применяем быстрый фильтр если есть
            default_log_level = "Все"
            if 'quick_filter' in st.session_state:
                default_log_level = st.session_state.quick_filter.get("log_level_filter", "Все")
                
            log_level_filter = st.selectbox(
                "Уровень логирования",
                ["Все", "ERROR", "WARNING", "INFO", "DEBUG"],
                index=["Все", "ERROR", "WARNING", "INFO", "DEBUG"].index(default_log_level),
                help="Фильтр по уровню логирования"
            )
        
        with col3:
            # Применяем быстрый фильтр если есть
            default_search = ""
            if 'quick_filter' in st.session_state:
                default_search = st.session_state.quick_filter.get("search_term", "")
                
            search_term = st.text_input(
                "Поиск в логах",
                value=default_search,
                placeholder="Поиск по тексту...",
                help="Поиск по содержимому строки лога"
            )
        
        with col4:
            # Применяем быстрый фильтр если есть
            default_regex = False
            if 'quick_filter' in st.session_state:
                default_regex = st.session_state.quick_filter.get("use_regex", False)
                
            use_regex = st.checkbox(
                "Регулярные выражения",
                value=default_regex,
                help="Использовать регулярные выражения для поиска"
            )
        
        # Второй ряд фильтров
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            # Фильтр по времени
            time_filter_enabled = st.checkbox("Фильтр по времени")
            
        with col2:
            if time_filter_enabled:
                from_time = st.time_input(
                    "От времени",
                    value=datetime.now().replace(hour=0, minute=0, second=0).time(),
                    help="Показать логи с указанного времени"
                )
            else:
                from_time = None
        
        with col3:
            if time_filter_enabled:
                to_time = st.time_input(
                    "До времени",
                    value=datetime.now().time(),
                    help="Показать логи до указанного времени"
                )
            else:
                to_time = None
        
        with col4:
            # Применяем быстрый фильтр если есть
            default_agent = ""
            if 'quick_filter' in st.session_state:
                default_agent = st.session_state.quick_filter.get("agent_module_filter", "")
                
            # Фильтр по агенту/модулю
            agent_module_filter = st.text_input(
                "Агент/Модуль",
                value=default_agent,
                placeholder="Имя агента или модуля...",
                help="Фильтр по имени агента или модуля"
            )
        
        # Третий ряд фильтров
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            # Применяем быстрый фильтр если есть
            default_event = "Все"
            if 'quick_filter' in st.session_state:
                default_event = st.session_state.quick_filter.get("event_type_filter", "Все")
                
            # Фильтр по типу события
            event_type_filter = st.selectbox(
                "Тип события",
                ["Все", "started", "completed", "failed", "step", "tool_call", "error", "warning"],
                index=["Все", "started", "completed", "failed", "step", "tool_call", "error", "warning"].index(default_event),
                help="Фильтр по типу события в логах"
            )
        
        with col2:
            # Инвертировать поиск
            invert_search = st.checkbox(
                "Исключить найденное",
                help="Показать строки, НЕ содержащие поисковый запрос"
            )
        
        with col3:
            # Учитывать регистр
            case_sensitive = st.checkbox(
                "Учитывать регистр",
                help="Различать заглавные и строчные буквы при поиске"
            )
        
        with col4:
            # Контекст вокруг найденных строк
            context_lines = st.number_input(
                "Строк контекста",
                min_value=0,
                max_value=10,
                value=0,
                help="Количество строк до и после найденной для контекста"
            )
        
        # Читаем файл лога
        with open(log_file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # Берем последние строки
        recent_lines = lines[-lines_to_show:]
        
        # Применяем фильтры
        filtered_lines = apply_log_filters(
            recent_lines, 
            log_level_filter=log_level_filter,
            search_term=search_term,
            use_regex=use_regex,
            time_filter_enabled=time_filter_enabled,
            from_time=from_time,
            to_time=to_time,
            agent_module_filter=agent_module_filter,
            event_type_filter=event_type_filter,
            invert_search=invert_search,
            case_sensitive=case_sensitive
        )
        
        # Добавляем контекст если нужно
        if context_lines > 0 and filtered_lines:
            filtered_lines = add_context_lines(
                lines, filtered_lines, context_lines, lines_to_show
            )
        
        # Отображение
        st.markdown(f"### 📋 Содержимое: {log_file_path.name}")
        
        # Статистика фильтрации
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("📊 Всего строк", len(recent_lines))
        with col2:
            st.metric("🔍 После фильтрации", len(filtered_lines))
        with col3:
            filter_ratio = (len(filtered_lines) / len(recent_lines) * 100) if recent_lines else 0
            st.metric("📈 Соответствие фильтрам", f"{filter_ratio:.1f}%")
        
        if filtered_lines:
            # Форматированное отображение логов
            log_text = "".join(filtered_lines)
            
            # Подсветка разных уровней и поискового запроса
            log_lines_formatted = []
            for line in filtered_lines:
                formatted_line = format_log_line(line, search_term, case_sensitive, use_regex)
                log_lines_formatted.append(formatted_line)
            
            # Отображаем в текстовом блоке (ограничиваем для производительности)
            display_lines = log_lines_formatted[-100:] if len(log_lines_formatted) > 100 else log_lines_formatted
            formatted_text = "\n".join(display_lines)
            
            st.text_area(
                f"Логи (показано {len(display_lines)} из {len(filtered_lines)} отфильтрованных строк)",
                value=formatted_text,
                height=500,
                help="Форматированные логи с цветовой индикацией и подсветкой поиска"
            )
            
            # Опции экспорта и дополнительного анализа
            col1, col2, col3 = st.columns(3)
            
            with col1:
                # Экспорт отфильтрованных логов
                if st.button("📥 Экспорт отфильтрованных"):
                    export_filtered_logs(filtered_lines, log_file_path.name)
            
            with col2:
                # Статистика по уровням логирования
                if st.button("📊 Статистика уровней"):
                    show_log_level_stats(filtered_lines)
            
            with col3:
                # Анализ временных паттернов
                if st.button("⏰ Временные паттерны"):
                    show_time_patterns(filtered_lines)
            
            # Сырое содержимое
            with st.expander("📋 Сырое содержимое (отфильтрованное)"):
                st.code(log_text, language="text")
                
            # Дополнительная информация о фильтрах
            with st.expander("🔍 Информация о фильтрах"):
                active_filters = get_active_filters_info(
                    log_level_filter, search_term, use_regex, time_filter_enabled,
                    from_time, to_time, agent_module_filter, event_type_filter,
                    invert_search, case_sensitive, context_lines
                )
                
                if active_filters:
                    st.markdown("**Активные фильтры:**")
                    for filter_info in active_filters:
                        st.markdown(f"- {filter_info}")
                else:
                    st.info("Фильтры не применены")
        else:
            st.info("📭 Нет строк, соответствующих выбранным фильтрам")
            
            # Предложения по изменению фильтров
            st.markdown("**💡 Попробуйте:**")
            st.markdown("- Увеличить количество строк для анализа")
            st.markdown("- Изменить поисковый запрос")
            st.markdown("- Сбросить фильтры и начать заново")
            st.markdown("- Проверить правильность регулярного выражения (если используется)")
        
        # Предустановленные фильтры
        st.markdown("### 🔖 Быстрые фильтры")
        
        quick_filter_col1, quick_filter_col2, quick_filter_col3, quick_filter_col4 = st.columns(4)
        
        with quick_filter_col1:
            if st.button("🔴 Только ошибки"):
                st.session_state.quick_filter = {
                    "log_level_filter": "ERROR",
                    "search_term": "",
                    "agent_module_filter": "",
                    "event_type_filter": "Все"
                }
                st.rerun()
        
        with quick_filter_col2:
            if st.button("🤖 Агенты"):
                st.session_state.quick_filter = {
                    "log_level_filter": "Все",
                    "search_term": "agent",
                    "agent_module_filter": "",
                    "event_type_filter": "Все"
                }
                st.rerun()
        
        with quick_filter_col3:
            if st.button("🔧 SQL запросы"):
                st.session_state.quick_filter = {
                    "log_level_filter": "Все",
                    "search_term": "SQL|query|SELECT|INSERT|UPDATE|DELETE",
                    "agent_module_filter": "",
                    "event_type_filter": "Все",
                    "use_regex": True
                }
                st.rerun()
        
        with quick_filter_col4:
            if st.button("🔄 Сбросить фильтры"):
                if 'quick_filter' in st.session_state:
                    del st.session_state.quick_filter
                st.rerun()

        # Кнопки управления
        col1, col2, col3 = st.columns(3)
        
        with col1:
            if st.button("🔄 Обновить"):
                st.rerun()
        
        with col2:
            if st.button("📥 Скачать файл"):
                with open(log_file_path, 'r', encoding='utf-8') as f:
                    log_content = f.read()
                
                st.download_button(
                    label="💾 Скачать лог",
                    data=log_content,
                    file_name=log_file_path.name,
                    mime="text/plain"
                )
        
        with col3:
            if st.button("🗑️ Очистить файл"):
                if st.button("⚠️ Подтвердить очистку"):
                    with open(log_file_path, 'w') as f:
                        f.write("")
                    st.success("✅ Файл лога очищен")
                    st.rerun()
    
    except Exception as e:
        st.error(f"❌ Ошибка чтения файла лога: {e}")

def apply_log_filters(lines, log_level_filter="Все", search_term="", use_regex=False,
                     time_filter_enabled=False, from_time=None, to_time=None,
                     agent_module_filter="", event_type_filter="Все",
                     invert_search=False, case_sensitive=False):
    """Применение всех фильтров к строкам лога"""
    
    import re
    from datetime import datetime, time
    
    filtered_lines = lines.copy()
    
    # Фильтр по уровню логирования
    if log_level_filter != "Все":
        filtered_lines = [
            line for line in filtered_lines 
            if log_level_filter in line
        ]
    
    # Фильтр по времени
    if time_filter_enabled and from_time and to_time:
        time_filtered = []
        for line in filtered_lines:
            line_time = extract_time_from_log(line)
            if line_time and from_time <= line_time <= to_time:
                time_filtered.append(line)
        filtered_lines = time_filtered
    
    # Фильтр по агенту/модулю
    if agent_module_filter:
        agent_filtered = []
        search_pattern = agent_module_filter.lower()
        for line in filtered_lines:
            line_lower = line.lower()
            if (search_pattern in line_lower or 
                f"agent:{search_pattern}" in line_lower or
                f"module:{search_pattern}" in line_lower or
                f"[{search_pattern}]" in line_lower):
                agent_filtered.append(line)
        filtered_lines = agent_filtered
    
    # Фильтр по типу события
    if event_type_filter != "Все":
        event_filtered = []
        for line in filtered_lines:
            line_lower = line.lower()
            if event_type_filter.lower() in line_lower:
                event_filtered.append(line)
        filtered_lines = event_filtered
    
    # Поиск по тексту
    if search_term:
        search_filtered = []
        try:
            if use_regex:
                flags = 0 if case_sensitive else re.IGNORECASE
                pattern = re.compile(search_term, flags)
                for line in filtered_lines:
                    if pattern.search(line):
                        search_filtered.append(line)
            else:
                search_text = search_term if case_sensitive else search_term.lower()
                for line in filtered_lines:
                    line_text = line if case_sensitive else line.lower()
                    if search_text in line_text:
                        search_filtered.append(line)
        except re.error:
            # Если регулярное выражение некорректно, используем обычный поиск
            search_text = search_term if case_sensitive else search_term.lower()
            for line in filtered_lines:
                line_text = line if case_sensitive else line.lower()
                if search_text in line_text:
                    search_filtered.append(line)
        
        filtered_lines = search_filtered
    
    # Инвертирование результата поиска
    if invert_search and search_term:
        all_lines = lines.copy()
        # Применяем все фильтры кроме поиска
        temp_filtered = apply_log_filters(
            all_lines, log_level_filter, "", use_regex,
            time_filter_enabled, from_time, to_time,
            agent_module_filter, event_type_filter,
            False, case_sensitive
        )
        # Исключаем найденные строки
        filtered_lines = [line for line in temp_filtered if line not in filtered_lines]
    
    return filtered_lines

def extract_time_from_log(log_line):
    """Извлечение времени из строки лога"""
    
    import re
    from datetime import time
    
    # Паттерны для поиска времени в разных форматах
    time_patterns = [
        r'(\d{2}):(\d{2}):(\d{2})',  # HH:MM:SS
        r'(\d{1,2}):(\d{2}):(\d{2})',  # H:MM:SS
        r'T(\d{2}):(\d{2}):(\d{2})',  # ISO время с T
        r'\s(\d{2}):(\d{2}):(\d{2})\s',  # время с пробелами
    ]
    
    for pattern in time_patterns:
        match = re.search(pattern, log_line)
        if match:
            try:
                hour = int(match.group(1))
                minute = int(match.group(2))
                second = int(match.group(3))
                return time(hour, minute, second)
            except (ValueError, IndexError):
                continue
    
    return None

def add_context_lines(all_lines, filtered_lines, context_count, max_lines):
    """Добавление контекстных строк вокруг найденных"""
    
    if not filtered_lines or context_count == 0:
        return filtered_lines
    
    # Найдем индексы отфильтрованных строк в общем списке
    context_lines = set()
    recent_lines = all_lines[-max_lines:]
    
    for filtered_line in filtered_lines:
        try:
            line_index = recent_lines.index(filtered_line)
            # Добавляем контекст
            start_idx = max(0, line_index - context_count)
            end_idx = min(len(recent_lines), line_index + context_count + 1)
            
            for i in range(start_idx, end_idx):
                context_lines.add((i, recent_lines[i]))
        except ValueError:
            continue
    
    # Сортируем по индексу и возвращаем только строки
    sorted_context = sorted(context_lines, key=lambda x: x[0])
    return [line for _, line in sorted_context]

def format_log_line(line, search_term="", case_sensitive=False, use_regex=False):
    """Форматирование строки лога с подсветкой"""
    
    # Определяем уровень логирования для эмодзи
    if "ERROR" in line:
        emoji = "🔴"
    elif "WARNING" in line:
        emoji = "🟡"
    elif "INFO" in line:
        emoji = "ℹ️"
    elif "DEBUG" in line:
        emoji = "🔍"
    else:
        emoji = "📝"
    
    formatted_line = f"{emoji} {line.strip()}"
    
    # Подсветка поискового запроса (в упрощенном виде)
    if search_term and search_term in line:
        # Заменяем найденный текст на версию с маркерами
        if not case_sensitive:
            # Простая подсветка без учета регистра
            search_lower = search_term.lower()
            line_lower = line.lower()
            if search_lower in line_lower:
                formatted_line += " 🎯"
        else:
            if search_term in line:
                formatted_line += " 🎯"
    
    return formatted_line

def export_filtered_logs(filtered_lines, original_filename):
    """Экспорт отфильтрованных логов"""
    
    if not filtered_lines:
        st.warning("Нет данных для экспорта")
        return
    
    export_text = "".join(filtered_lines)
    filename = f"filtered_{original_filename}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    
    st.download_button(
        label="💾 Скачать отфильтрованные логи",
        data=export_text,
        file_name=filename,
        mime="text/plain"
    )

def show_log_level_stats(filtered_lines):
    """Показать статистику по уровням логирования"""
    
    level_counts = {
        "ERROR": 0,
        "WARNING": 0,
        "INFO": 0,
        "DEBUG": 0,
        "OTHER": 0
    }
    
    for line in filtered_lines:
        if "ERROR" in line:
            level_counts["ERROR"] += 1
        elif "WARNING" in line:
            level_counts["WARNING"] += 1
        elif "INFO" in line:
            level_counts["INFO"] += 1
        elif "DEBUG" in line:
            level_counts["DEBUG"] += 1
        else:
            level_counts["OTHER"] += 1
    
    st.markdown("#### 📊 Статистика по уровням логирования")
    
    total = sum(level_counts.values())
    if total > 0:
        for level, count in level_counts.items():
            if count > 0:
                percentage = (count / total) * 100
                st.markdown(f"**{level}**: {count} строк ({percentage:.1f}%)")
        
        # Визуализация
        import pandas as pd
        chart_data = pd.DataFrame(
            list(level_counts.items()),
            columns=['Уровень', 'Количество']
        )
        chart_data = chart_data[chart_data['Количество'] > 0]
        
        if not chart_data.empty:
            st.bar_chart(chart_data.set_index('Уровень'))
    else:
        st.info("Нет данных для анализа")

def show_time_patterns(filtered_lines):
    """Анализ временных паттернов в логах"""
    
    st.markdown("#### ⏰ Временные паттерны")
    
    time_counts = {}
    
    for line in filtered_lines:
        log_time = extract_time_from_log(line)
        if log_time:
            hour = log_time.hour
            time_counts[hour] = time_counts.get(hour, 0) + 1
    
    if time_counts:
        # Создаем график по часам
        import pandas as pd
        
        hours = list(range(24))
        counts = [time_counts.get(h, 0) for h in hours]
        
        chart_data = pd.DataFrame({
            'Час': hours,
            'Количество логов': counts
        }).set_index('Час')
        
        st.line_chart(chart_data)
        
        # Пиковые часы
        max_hour = max(time_counts.items(), key=lambda x: x[1])
        st.info(f"🕐 Пиковая активность: {max_hour[0]}:00 ({max_hour[1]} событий)")
    else:
        st.info("Не удалось извлечь временную информацию из логов")

def get_active_filters_info(log_level_filter, search_term, use_regex, time_filter_enabled,
                           from_time, to_time, agent_module_filter, event_type_filter,
                           invert_search, case_sensitive, context_lines):
    """Получение информации об активных фильтрах"""
    
    active_filters = []
    
    if log_level_filter != "Все":
        active_filters.append(f"Уровень логирования: {log_level_filter}")
    
    if search_term:
        search_info = f"Поиск: '{search_term}'"
        if use_regex:
            search_info += " (регулярное выражение)"
        if case_sensitive:
            search_info += " (с учетом регистра)"
        if invert_search:
            search_info += " (инвертированный)"
        active_filters.append(search_info)
    
    if time_filter_enabled and from_time and to_time:
        active_filters.append(f"Время: с {from_time} до {to_time}")
    
    if agent_module_filter:
        active_filters.append(f"Агент/Модуль: {agent_module_filter}")
    
    if event_type_filter != "Все":
        active_filters.append(f"Тип события: {event_type_filter}")
    
    if context_lines > 0:
        active_filters.append(f"Контекст: ±{context_lines} строк")
    
    return active_filters

def show_analytics():
    """Аналитика логов и трасс"""
    
    st.markdown("## 📈 Аналитика выполнения")
    
    try:
        from telemetry import get_telemetry_manager
        
        telemetry_manager = get_telemetry_manager()
        
        if not telemetry_manager.is_enabled():
            st.warning("⚠️ Телеметрия отключена. Включите для получения аналитики.")
            return
        
        trace_files = telemetry_manager.get_trace_files()
        # Исключаем служебную трассу unknown
        trace_files = [tf for tf in trace_files if tf.get("run_id") != "unknown"]
        
        if not trace_files:
            st.info("📊 Нет данных для аналитики")
            return
        
        # Временной период для анализа
        col1, col2 = st.columns(2)
        
        with col1:
            period = st.selectbox(
                "📅 Период анализа",
                ["Последние 24 часа", "Последние 7 дней", "Последние 30 дней", "Все время"]
            )
        
        with col2:
            if st.button("📊 Построить аналитику"):
                build_analytics(telemetry_manager, trace_files, period)
        
        # Показываем существующую аналитику
        show_performance_metrics(telemetry_manager, trace_files)
        show_error_analysis(telemetry_manager, trace_files)
        show_usage_patterns(telemetry_manager, trace_files)
    
    except Exception as e:
        st.error(f"❌ Ошибка аналитики: {e}")

def build_analytics(telemetry_manager, trace_files, period):
    """Построение аналитики"""
    
    st.markdown("#### 📊 Генерация аналитики")
    
    # Определяем временной диапазон
    now = datetime.now()
    period_mapping = {
        "Последние 24 часа": now - timedelta(hours=24),
        "Последние 7 дней": now - timedelta(days=7),
        "Последние 30 дней": now - timedelta(days=30),
        "Все время": datetime.min
    }
    
    start_date = period_mapping.get(period, datetime.min)
    
    # Фильтруем трассы по времени
    filtered_traces = [
        tf for tf in trace_files 
        if tf.get("modified_time", datetime.min) >= start_date
    ]
    
    st.info(f"Анализ {len(filtered_traces)} трасс за период: {period}")
    
    if filtered_traces:
        # Быстрая статистика
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric("📊 Трасс", len(filtered_traces))
        
        with col2:
            total_events = sum(tf.get("events_count", 0) for tf in filtered_traces)
            st.metric("🔍 Событий", total_events)
        
        with col3:
            avg_events = total_events / len(filtered_traces) if filtered_traces else 0
            st.metric("📈 Среднее событий/трасса", f"{avg_events:.1f}")

def show_performance_metrics(telemetry_manager, trace_files):
    """Метрики производительности"""
    
    st.markdown("### ⚡ Метрики производительности")
    
    try:
        # Анализируем последние 10 трасс
        recent_traces = sorted(trace_files, key=lambda x: x.get("modified_time", datetime.min))[-10:]
        
        performance_data = []
        
        for trace_file in recent_traces:
            try:
                trace_content = telemetry_manager.load_trace_file(trace_file["run_id"])
                spans = trace_content.get("spans", [])
                
                if spans:
                    # Вычисляем метрики
                    start_times = [s.get("start_time_unix_nano", 0) for s in spans if s.get("start_time_unix_nano")]
                    end_times = [s.get("end_time_unix_nano", 0) for s in spans if s.get("end_time_unix_nano")]
                    
                    if start_times and end_times:
                        total_duration_ms = (max(end_times) - min(start_times)) / 1_000_000
                        
                        # Определяем тип операции
                        root_spans = [s for s in spans if not s.get("parent_span_id")]
                        operation_type = root_spans[0].get("name", "Unknown") if root_spans else "Unknown"
                        
                        performance_data.append({
                            "Run ID": trace_file["run_id"][:8] + "...",
                            "Операция": operation_type,
                            "Длительность (ms)": f"{total_duration_ms:.1f}",
                            "Спанов": len(spans),
                            "Время": trace_file["modified_time"].strftime("%H:%M:%S")
                        })
            except:
                continue
        
        if performance_data:
            df = pd.DataFrame(performance_data)
            st.dataframe(df, use_container_width=True)
            
            # График производительности
            durations = [float(row["Длительность (ms)"]) for row in performance_data]
            if durations:
                st.line_chart(durations)
                
                avg_duration = sum(durations) / len(durations)
                st.info(f"📊 Средняя длительность: {avg_duration:.1f}ms")
        else:
            st.info("📊 Нет данных о производительности")
    
    except Exception as e:
        st.error(f"❌ Ошибка анализа производительности: {e}")

def show_error_analysis(telemetry_manager, trace_files):
    """Анализ ошибок"""
    
    st.markdown("### ❌ Анализ ошибок")
    
    try:
        error_stats = {
            "total_traces": len(trace_files),
            "traces_with_errors": 0,
            "error_types": {},
            "recent_errors": []
        }
        
        for trace_file in trace_files[-20:]:  # Последние 20 трасс
            try:
                trace_content = telemetry_manager.load_trace_file(trace_file["run_id"])
                spans = trace_content.get("spans", [])
                
                trace_has_errors = False
                for span in spans:
                    status = span.get("status", {})
                    if status.get("status_code") == "ERROR":
                        trace_has_errors = True
                        
                        # Классифицируем ошибку
                        error_message = status.get("message", "Unknown error")
                        error_type = classify_error(error_message)
                        
                        error_stats["error_types"][error_type] = error_stats["error_types"].get(error_type, 0) + 1
                        
                        # Добавляем в недавние ошибки
                        if len(error_stats["recent_errors"]) < 5:
                            error_stats["recent_errors"].append({
                                "run_id": trace_file["run_id"],
                                "time": trace_file["modified_time"],
                                "message": error_message[:100],
                                "span_name": span.get("name", "Unknown")
                            })
                
                if trace_has_errors:
                    error_stats["traces_with_errors"] += 1
            
            except:
                continue
        
        # Отображение статистики ошибок
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric("📊 Всего трасс", error_stats["total_traces"])
        
        with col2:
            st.metric("❌ С ошибками", error_stats["traces_with_errors"])
        
        with col3:
            error_rate = (error_stats["traces_with_errors"] / error_stats["total_traces"] * 100) if error_stats["total_traces"] > 0 else 0
            st.metric("📈 Процент ошибок", f"{error_rate:.1f}%")
        
        # Типы ошибок
        if error_stats["error_types"]:
            st.markdown("**🏷️ Типы ошибок:**")
            for error_type, count in sorted(error_stats["error_types"].items(), key=lambda x: x[1], reverse=True):
                st.markdown(f"- **{error_type}**: {count} раз")
        
        # Недавние ошибки
        if error_stats["recent_errors"]:
            st.markdown("**🕐 Недавние ошибки:**")
            for error in error_stats["recent_errors"]:
                st.error(f"**{error['span_name']}** ({error['time'].strftime('%H:%M:%S')}): {error['message']}")
    
    except Exception as e:
        st.error(f"❌ Ошибка анализа ошибок: {e}")

def classify_error(error_message):
    """Классификация ошибок по типам"""
    
    error_message_lower = error_message.lower()
    
    if "connection" in error_message_lower or "network" in error_message_lower:
        return "Сетевые ошибки"
    elif "timeout" in error_message_lower:
        return "Ошибки таймаута"
    elif "permission" in error_message_lower or "access" in error_message_lower:
        return "Ошибки доступа"
    elif "validation" in error_message_lower or "invalid" in error_message_lower:
        return "Ошибки валидации"
    elif "sql" in error_message_lower or "database" in error_message_lower:
        return "Ошибки БД"
    elif "memory" in error_message_lower or "out of" in error_message_lower:
        return "Ошибки памяти"
    else:
        return "Прочие ошибки"

def show_usage_patterns(telemetry_manager, trace_files):
    """Паттерны использования"""
    
    st.markdown("### 📈 Паттерны использования")
    
    try:
        # Анализ по времени
        hourly_usage = {}
        agent_usage = {}
        
        for trace_file in trace_files[-50:]:  # Последние 50 трасс
            try:
                hour = trace_file["modified_time"].hour
                hourly_usage[hour] = hourly_usage.get(hour, 0) + 1
                
                # Анализ по агентам
                trace_content = telemetry_manager.load_trace_file(trace_file["run_id"])
                spans = trace_content.get("spans", [])
                
                for span in spans:
                    attributes = span.get("attributes", {})
                    agent_name = attributes.get("agent_name")
                    if agent_name:
                        agent_usage[agent_name] = agent_usage.get(agent_name, 0) + 1
                        break  # Один агент на трассу
            except:
                continue
        
        # График по часам
        if hourly_usage:
            st.markdown("**🕐 Использование по часам:**")
            
            hours = list(range(24))
            usage_counts = [hourly_usage.get(h, 0) for h in hours]
            
            chart_data = pd.DataFrame({
                "Час": hours,
                "Запуски": usage_counts
            }).set_index("Час")
            
            st.bar_chart(chart_data)
        
        # Использование по агентам
        if agent_usage:
            st.markdown("**🤖 Использование по агентам:**")
            
            sorted_agents = sorted(agent_usage.items(), key=lambda x: x[1], reverse=True)
            
            for agent, count in sorted_agents[:10]:  # Топ-10
                st.markdown(f"- **{agent}**: {count} запусков")
    
    except Exception as e:
        st.error(f"❌ Ошибка анализа паттернов: {e}")

def show_telemetry_settings():
    """Настройки телеметрии"""
    
    st.markdown("## ⚙️ Настройки телеметрии")
    
    try:
        from telemetry import get_telemetry_manager
        from configuration_api import get_configuration_manager
        
        telemetry_manager = get_telemetry_manager()
        config_manager = get_configuration_manager()
        config = config_manager.get_config()
        
        # Основные настройки
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("### 📊 Основные настройки")
            
            current_status = telemetry_manager.is_enabled()
            
            new_status = st.checkbox(
                "Включить телеметрию",
                value=current_status,
                help="Включить/отключить сбор трасс OpenTelemetry"
            )
            
            if new_status != current_status:
                if new_status:
                    telemetry_manager.enable()
                    st.success("✅ Телеметрия включена")
                else:
                    telemetry_manager.disable()
                    st.success("✅ Телеметрия отключена")
                
                st.rerun()
            
            # Настройки хранения
            trace_retention_days = st.number_input(
                "📅 Хранить трассы (дней)",
                min_value=1,
                max_value=365,
                value=config.telemetry.trace_retention_days,
                help="Количество дней хранения файлов трасс"
            )
            
            max_trace_file_size_mb = st.number_input(
                "📁 Макс. размер файла трассы (MB)",
                min_value=1.0,
                max_value=100.0,
                value=config.telemetry.max_trace_file_size_mb,
                help="Максимальный размер одного файла трассы"
            )
        
        with col2:
            st.markdown("### 🔍 Настройки сбора")
            
            collect_detailed_spans = st.checkbox(
                "Детальные спаны",
                value=config.telemetry.collect_detailed_spans,
                help="Собирать детальную информацию о каждом шаге"
            )
            
            collect_memory_metrics = st.checkbox(
                "Метрики памяти",
                value=config.telemetry.collect_memory_metrics,
                help="Собирать информацию об использовании памяти"
            )
            
            collect_performance_metrics = st.checkbox(
                "Метрики производительности",
                value=config.telemetry.collect_performance_metrics,
                help="Собирать метрики времени выполнения"
            )
        
        # Сохранение настроек
        if st.button("💾 Сохранить настройки"):
            # Обновляем конфигурацию
            new_config = config
            new_config.telemetry.trace_retention_days = trace_retention_days
            new_config.telemetry.max_trace_file_size_mb = max_trace_file_size_mb
            new_config.telemetry.collect_detailed_spans = collect_detailed_spans
            new_config.telemetry.collect_memory_metrics = collect_memory_metrics
            new_config.telemetry.collect_performance_metrics = collect_performance_metrics
            
            config_manager.update_config(new_config)
            st.success("✅ Настройки сохранены")
        
        # Информация о хранилище
        st.markdown("### 💾 Информация о хранилище")
        
        logs_dir = Path(project_root) / "logs"
        traces_dir = logs_dir / "traces"
        
        if traces_dir.exists():
            trace_files = list(traces_dir.glob("*.jsonl"))
            total_size = sum(f.stat().st_size for f in trace_files) / (1024 * 1024)
            
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric("📁 Файлов трасс", len(trace_files))
            
            with col2:
                st.metric("💾 Общий размер", f"{total_size:.1f} MB")
            
            with col3:
                avg_size = total_size / len(trace_files) if trace_files else 0
                st.metric("📊 Средний размер", f"{avg_size:.2f} MB")
            
            st.info(f"**Путь хранения:** `{traces_dir}`")
        else:
            st.info("📁 Директория трасс будет создана при первом использовании")
        
        # Обслуживание
        st.markdown("### 🧹 Обслуживание")
        
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("🧹 Очистить все трассы"):
                if st.button("⚠️ Подтвердить полную очистку"):
                    try:
                        if traces_dir.exists():
                            for trace_file in traces_dir.glob("*.jsonl"):
                                trace_file.unlink()
                        st.success("✅ Все трассы очищены")
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ Ошибка очистки: {e}")
        
        with col2:
            if st.button("📊 Пересчитать статистику"):
                st.info("📊 Статистика пересчитана")
                st.rerun()
    
    except Exception as e:
        st.error(f"❌ Ошибка настроек телеметрии: {e}")

def add_multi_file_log_search():
    """Добавляет возможность поиска по нескольким файлам логов"""
    
    st.markdown("### 🔍 Поиск по всем логам")
    
    try:
        logs_dir = Path(project_root) / "logs"
        log_files = list(logs_dir.glob("*.log")) if logs_dir.exists() else []
        
        if not log_files:
            st.info("📭 Файлы логов не найдены")
            return
        
        # Фильтры для мульти-поиска
        col1, col2, col3 = st.columns(3)
        
        with col1:
            multi_search_term = st.text_input(
                "🔍 Поиск по всем логам",
                placeholder="Введите поисковый запрос...",
                help="Поиск будет выполнен по всем файлам логов"
            )
        
        with col2:
            multi_use_regex = st.checkbox(
                "Регулярные выражения (мульти)",
                help="Использовать регулярные выражения для поиска по всем файлам"
            )
        
        with col3:
            max_results_per_file = st.number_input(
                "Макс. результатов на файл",
                min_value=1,
                max_value=100,
                value=10,
                help="Максимальное количество найденных строк на файл"
            )
        
        # Кнопка поиска
        if st.button("🔍 Найти во всех логах") and multi_search_term:
            multi_search_results = []
            
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            for i, log_file in enumerate(log_files):
                status_text.text(f"Поиск в {log_file.name}...")
                progress_bar.progress((i + 1) / len(log_files))
                
                try:
                    with open(log_file, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                    
                    # Применяем поиск
                    found_lines = []
                    
                    try:
                        if multi_use_regex:
                            import re
                            pattern = re.compile(multi_search_term, re.IGNORECASE)
                            for line_num, line in enumerate(lines, 1):
                                if pattern.search(line):
                                    found_lines.append((line_num, line.strip()))
                                    if len(found_lines) >= max_results_per_file:
                                        break
                        else:
                            search_lower = multi_search_term.lower()
                            for line_num, line in enumerate(lines, 1):
                                if search_lower in line.lower():
                                    found_lines.append((line_num, line.strip()))
                                    if len(found_lines) >= max_results_per_file:
                                        break
                    except re.error:
                        # Fallback to simple search if regex is invalid
                        search_lower = multi_search_term.lower()
                        for line_num, line in enumerate(lines, 1):
                            if search_lower in line.lower():
                                found_lines.append((line_num, line.strip()))
                                if len(found_lines) >= max_results_per_file:
                                    break
                    
                    if found_lines:
                        multi_search_results.append({
                            'file': log_file.name,
                            'path': str(log_file),
                            'results': found_lines,
                            'total_lines': len(lines)
                        })
                
                except Exception as e:
                    st.warning(f"⚠️ Ошибка чтения {log_file.name}: {e}")
                    continue
            
            progress_bar.empty()
            status_text.empty()
            
            # Отображаем результаты
            if multi_search_results:
                st.markdown(f"### 🎯 Результаты поиска: '{multi_search_term}'")
                
                total_matches = sum(len(result['results']) for result in multi_search_results)
                st.info(f"Найдено {total_matches} совпадений в {len(multi_search_results)} файлах")
                
                for result in multi_search_results:
                    with st.expander(
                        f"📁 {result['file']} ({len(result['results'])} совпадений)",
                        expanded=len(multi_search_results) <= 3
                    ):
                        for line_num, line_content in result['results']:
                            # Подсветка найденного текста
                            if multi_search_term.lower() in line_content.lower():
                                # Простая подсветка
                                st.markdown(f"**Строка {line_num}:** {line_content} 🎯")
                            else:
                                st.markdown(f"**Строка {line_num}:** {line_content}")
                        
                        # Кнопка для открытия файла
                        if st.button(f"📖 Открыть {result['file']}", key=f"open_{result['file']}"):
                            st.session_state.selected_log_file = result['file']
                            st.info(f"Выберите '{result['file']}' в списке файлов логов выше")
                
                # Экспорт результатов поиска
                if st.button("📥 Экспорт результатов поиска"):
                    export_text = f"Результаты поиска: '{multi_search_term}'\n"
                    export_text += f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    export_text += "=" * 50 + "\n\n"
                    
                    for result in multi_search_results:
                        export_text += f"Файл: {result['file']}\n"
                        export_text += f"Путь: {result['path']}\n"
                        export_text += f"Совпадений: {len(result['results'])}\n"
                        export_text += "-" * 30 + "\n"
                        
                        for line_num, line_content in result['results']:
                            export_text += f"Строка {line_num}: {line_content}\n"
                        
                        export_text += "\n"
                    
                    st.download_button(
                        label="💾 Скачать результаты",
                        data=export_text,
                        file_name=f"multi_search_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                        mime="text/plain"
                    )
            else:
                st.warning(f"🔍 Ничего не найдено по запросу: '{multi_search_term}'")
                st.markdown("**💡 Попробуйте:**")
                st.markdown("- Изменить поисковый запрос")
                st.markdown("- Использовать другие ключевые слова")
                st.markdown("- Проверить правильность регулярного выражения")
    
    except Exception as e:
        st.error(f"❌ Ошибка мульти-поиска: {e}")



if __name__ == "__main__":
    main()
