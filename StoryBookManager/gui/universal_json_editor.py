"""
Универсальный редактор JSON
=========================

Автоматически генерирует интерфейс на основе JSON Schema с поддержкой
кастомизации через UI-конфигурацию.
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import json
from typing import Dict, Any, List, Optional, Union, Callable
from pathlib import Path
import logging


def create_collapsible_frame(parent, title, collapsed=False):
    """Создает сворачиваемый фрейм (аккордеон)"""
    import tkinter as tk
    from tkinter import ttk
    
    # Главный контейнер
    main_frame = ttk.Frame(parent)
    main_frame.pack(fill="x", pady=2)
    
    # Переменная состояния
    is_collapsed = tk.BooleanVar(value=collapsed)
    
    # Заголовок с кнопкой
    header_frame = ttk.Frame(main_frame)
    header_frame.pack(fill="x")
    
    # Стрелка
    arrow_label = ttk.Label(header_frame, text="▼" if not collapsed else "▶")
    arrow_label.pack(side="left", padx=(0, 5))
    
    # Заголовок
    title_label = ttk.Label(header_frame, text=title, font=("Arial", 10, "bold"))
    title_label.pack(side="left")
    
    # Контент фрейм
    content_frame = ttk.Frame(main_frame)
    if not collapsed:
        content_frame.pack(fill="x", pady=(5, 0))
    
    def toggle_collapse():
        if is_collapsed.get():
            # Разворачиваем
            content_frame.pack(fill="x", pady=(5, 0))
            arrow_label.config(text="▼")
            is_collapsed.set(False)
        else:
            # Сворачиваем
            content_frame.pack_forget()
            arrow_label.config(text="▶")
            is_collapsed.set(True)
    
    # Привязываем клик к заголовку
    header_frame.bind("<Button-1>", lambda e: toggle_collapse())
    arrow_label.bind("<Button-1>", lambda e: toggle_collapse())
    title_label.bind("<Button-1>", lambda e: toggle_collapse())
    
    return content_frame


def add_context_menu(widget):
    """Добавляет стандартное контекстное меню к текстовому виджету"""
    
    def show_context_menu(event):
        # Создаем контекстное меню
        context_menu = tk.Menu(widget, tearoff=0)
        
        # Определяем тип виджета для правильной работы с текстом
        is_text_widget = isinstance(widget, (tk.Text, scrolledtext.ScrolledText))
        
        # Проверяем, есть ли выделенный текст
        try:
            if is_text_widget:
                has_selection = widget.tag_ranges(tk.SEL)
                selected_text = widget.get(tk.SEL_FIRST, tk.SEL_LAST) if has_selection else ""
            else:  # Entry, Spinbox
                has_selection = widget.selection_present()
                selected_text = widget.selection_get() if has_selection else ""
        except Exception:
            has_selection = False
            selected_text = ""
        
        # Проверяем буфер обмена
        try:
            clipboard_text = widget.clipboard_get()
            has_clipboard = bool(clipboard_text)
        except Exception:
            has_clipboard = False
        
        # Функции для операций
        def copy_text():
            if selected_text:
                widget.clipboard_clear()
                widget.clipboard_append(selected_text)
        
        def cut_text():
            if selected_text:
                widget.clipboard_clear()
                widget.clipboard_append(selected_text)
                if is_text_widget:
                    widget.delete(tk.SEL_FIRST, tk.SEL_LAST)
                else:
                    widget.delete(tk.SEL_FIRST, tk.SEL_LAST)
        
        def paste_text():
            try:
                clipboard_text = widget.clipboard_get()
                if is_text_widget:
                    if widget.tag_ranges(tk.SEL):
                        widget.delete(tk.SEL_FIRST, tk.SEL_LAST)
                    widget.insert(tk.INSERT, clipboard_text)
                else:
                    if widget.selection_present():
                        widget.delete(tk.SEL_FIRST, tk.SEL_LAST)
                    widget.insert(tk.INSERT, clipboard_text)
            except Exception:
                pass
        
        def select_all():
            if is_text_widget:
                widget.tag_add(tk.SEL, "1.0", tk.END)
                widget.mark_set(tk.INSERT, "1.0")
                widget.see(tk.INSERT)
            else:
                widget.select_range(0, tk.END)
                widget.icursor(tk.END)
        
        # Добавляем пункты меню
        context_menu.add_command(
            label="Копировать", 
            command=copy_text,
            state=tk.NORMAL if has_selection else tk.DISABLED,
            accelerator="Ctrl+C"
        )
        context_menu.add_command(
            label="Вырезать", 
            command=cut_text,
            state=tk.NORMAL if has_selection else tk.DISABLED,
            accelerator="Ctrl+X"
        )
        context_menu.add_command(
            label="Вставить", 
            command=paste_text,
            state=tk.NORMAL if has_clipboard else tk.DISABLED,
            accelerator="Ctrl+V"
        )
        context_menu.add_separator()
        context_menu.add_command(
            label="Выделить всё", 
            command=select_all,
            accelerator="Ctrl+A"
        )
        
        # Показываем меню
        try:
            context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            context_menu.grab_release()
    
    # Привязываем правый клик
    widget.bind("<Button-3>", show_context_menu)  # Windows/Linux
    widget.bind("<Button-2>", show_context_menu)  # macOS

logger = logging.getLogger(__name__)


def generate_hybrid_schema(ui_config: Dict[str, Any], data: Any, schema_type: str = "auto") -> Dict[str, Any]:
    """
    БАЗОВАЯ генерация схемы: структура из данных + настройки из UI config.
    
    Принцип: 
    - Данные определяют КАКИЕ поля показать
    - UI config определяет КАК их показать
    - Поля без UI config → поведение по умолчанию
    """
    
    def infer_field_schema_from_values(values: list) -> Dict[str, Any]:
        """Генерирует схему поля на основе всех его значений"""
        if not values:
            return {"type": ["string", "null"]}
        
        # Собираем все встреченные типы
        types_seen = set()
        has_null = False
        
        for value in values:
            if value is None:
                has_null = True
            elif isinstance(value, bool):
                types_seen.add("boolean")
            elif isinstance(value, int):
                types_seen.add("integer")
            elif isinstance(value, float):
                types_seen.add("number")
            elif isinstance(value, str):
                types_seen.add("string")
            elif isinstance(value, list):
                types_seen.add("array")
            elif isinstance(value, dict):
                types_seen.add("object")
        
        # Определяем результирующий тип
        types_list = list(types_seen)
        if has_null:
            types_list.append("null")
        
        if len(types_list) == 1:
            return {"type": types_list[0]}
        elif len(types_list) > 1:
            return {"type": types_list}
        else:
            return {"type": ["string", "null"]}
    
    def infer_schema_from_data(value: Any) -> Dict[str, Any]:
        """Определяет JSON Schema из структуры данных"""
        if value is None:
            return {"type": ["string", "null"]}
        elif isinstance(value, bool):
            return {"type": "boolean"}
        elif isinstance(value, int):
            return {"type": "integer"}
        elif isinstance(value, float):
            return {"type": "number"}
        elif isinstance(value, str):
            return {"type": "string"}
        elif isinstance(value, list):
            if not value:  # Пустой список
                return {
                    "type": "array",
                    "items": {"type": "string"}  # По умолчанию строки
                }
            
            # Определяем тип элементов по первому элементу
            first_item = value[0]
            if isinstance(first_item, dict):
                # Массив объектов - генерируем схему для объекта
                # Собираем все поля из всех элементов
                all_fields = set()
                for item in value:
                    if isinstance(item, dict):
                        all_fields.update(item.keys())
                
                # Собираем все значения каждого поля (включая None для отсутствующих)
                field_values = {}
                for field_name in all_fields:
                    field_values[field_name] = []
                    for item in value:
                        if isinstance(item, dict):
                            # Если поле отсутствует, считаем его None
                            val = item.get(field_name, None)
                            field_values[field_name].append(val)
                
                # Генерируем схему для каждого поля на основе всех его значений
                merged_properties = {}
                for field_name, values in field_values.items():
                    merged_properties[field_name] = infer_field_schema_from_values(values)
                
                return {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": merged_properties
                    }
                }
            else:
                # Массив примитивов
                item_schema = infer_schema_from_data(first_item)
                return {
                    "type": "array",
                    "items": item_schema
                }
        
        elif isinstance(value, dict):
            properties = {}
            for key, val in value.items():
                properties[key] = infer_schema_from_data(val)
            
            return {
                "type": "object",
                "properties": properties
            }
        
        else:
            # Неизвестный тип - считаем строкой
            return {"type": "string"}
    
    # Получаем базовую структуру из данных
    base_schema = infer_schema_from_data(data)
    
    # Получаем UI конфигурацию для данного типа
    type_config = ui_config.get(schema_type, {})
    field_config = type_config.get("field_config", {})
    
    # Обогащаем схему информацией из UI config
    def enrich_schema_with_ui_config(schema_part: Dict[str, Any], config_context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Обогащает схему UI конфигурацией
        
        config_context: контекст конфигурации для поиска настроек полей
        """
        if config_context is None:
            config_context = field_config
            
        if schema_part.get("type") == "object" and "properties" in schema_part:
            # Обрабатываем объект
            enriched_properties = {}
            for field_name, field_schema in schema_part["properties"].items():
                # Ищем конфигурацию для поля в текущем контексте
                field_key = f"{field_name}_field"
                field_ui_config = config_context.get(field_key, config_context.get(field_name, {}))
                
                # Рекурсивно обрабатываем поле
                enriched_field = enrich_schema_with_ui_config(field_schema, config_context)
                
                # Добавляем UI метаданные в схему
                if field_ui_config:
                    enriched_field["ui_config"] = field_ui_config
                
                enriched_properties[field_name] = enriched_field
            
            return {
                **schema_part,
                "properties": enriched_properties
            }
        
        elif schema_part.get("type") == "array" and "items" in schema_part:
            # Обрабатываем массив
            items_schema = schema_part["items"]
            enriched_items = enrich_schema_with_ui_config(items_schema, config_context)
            
            return {
                **schema_part,
                "items": enriched_items
            }
        
        else:
            # Листовое поле - возвращаем как есть
            return schema_part
    
    # Специальная обработка для случая, когда наши данные - это элементы массива
    # (например, data содержит поля shot'а, а не массив shots)
    
    # Если schema_type указывает на массив (shots), но данные - объект
    # то наши данные - это элемент массива, нужно искать конфигурацию в items
    items_config = field_config.get("items", {})
    if items_config and isinstance(data, dict):
        logger.info(f"🎯 Используем конфигурацию items для полей объекта")
        enriched_schema = enrich_schema_with_ui_config(base_schema, items_config)
    else:
        enriched_schema = enrich_schema_with_ui_config(base_schema, field_config)
    
    logger.info(f"🔄 Гибридная схема создана: {len(enriched_schema.get('properties', {}))} полей с UI настройками")
    return enriched_schema


VALID_WIDGETS = {
    "entry", "text_area", "combobox", "spinbox", "checkbox",
    "list_editor", "nested_group", "universal_array_editor",
    "dropdown_selector", "date_picker",
}

VALID_INTERFACES = {"tabs", "dropdown_selector", "list", "accordion"}

VALID_LAYOUTS = {"vertical", "grid", "horizontal"}


class SchemaIntrospector:
    """Анализ JSON Schema для генерации интерфейса"""

    def __init__(self):
        self.ui_config = self._load_ui_config()
        self._validate_ui_config(self.ui_config)
    
    def _load_ui_config(self) -> Dict[str, Any]:
        """Загрузка конфигурации UI для кастомизации полей.

        Raises:
            FileNotFoundError: если ui_config.json не найден
            ValueError: если JSON невалиден
        """
        config_path = Path(__file__).parent.parent / "config" / "ui_config.json"

        if not config_path.exists():
            raise FileNotFoundError(
                f"Файл конфигурации UI не найден: {config_path}"
            )

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Некорректный JSON в ui_config.json (строка {e.lineno}): {e.msg}"
            ) from e

        logger.info(f"UI конфигурация загружена: {len(config)} секций")
        return config

    def _validate_ui_config(self, config: Dict[str, Any]):
        """Проверяет ui_config на неизвестные виджеты, interface и layout."""
        skip_keys = {"translations", "editor_settings", "field_labels"}
        for section_name, section in config.items():
            if section_name in skip_keys or not isinstance(section, dict):
                continue
            field_config = section.get("field_config", {})
            self._validate_field_configs(section_name, field_config)
            for group_name, group_cfg in section.get("field_groups", {}).items():
                if not isinstance(group_cfg, dict):
                    continue
                layout = group_cfg.get("layout")
                if layout and layout not in VALID_LAYOUTS:
                    logger.warning(
                        f"[{section_name}.{group_name}] Неизвестный layout "
                        f"'{layout}'. "
                        f"Допустимые: {', '.join(sorted(VALID_LAYOUTS))}"
                    )

    def _validate_field_configs(self, section: str, field_config: Dict[str, Any]):
        """Рекурсивно проверяет widget, interface в field_config."""
        for field_name, field_cfg in field_config.items():
            if not isinstance(field_cfg, dict):
                continue
            widget = field_cfg.get("widget")
            if widget and widget not in VALID_WIDGETS:
                logger.warning(
                    f"[{section}] Неизвестный виджет '{widget}' "
                    f"для поля '{field_name}'. "
                    f"Допустимые: {', '.join(sorted(VALID_WIDGETS))}"
                )
            interface = field_cfg.get("interface")
            if interface and interface not in VALID_INTERFACES:
                logger.warning(
                    f"[{section}] Неизвестный interface '{interface}' "
                    f"для поля '{field_name}'. "
                    f"Допустимые: {', '.join(sorted(VALID_INTERFACES))}"
                )
            for key, val in field_cfg.items():
                if key.endswith("_field") and isinstance(val, dict):
                    nested_widget = val.get("widget")
                    if nested_widget and nested_widget not in VALID_WIDGETS:
                        logger.warning(
                            f"[{section}] Неизвестный виджет '{nested_widget}' "
                            f"для поля '{key}'. "
                            f"Допустимые: {', '.join(sorted(VALID_WIDGETS))}"
                        )

    def analyze_schema(self, schema_type: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        """
        Анализирует схему и возвращает метаданные для генерации UI
        
        Returns:
            {
                "fields": {...},      # Информация о полях
                "groups": {...},      # Группировка полей
                "layout": "...",      # Тип компоновки
                "validation": {...}   # Правила валидации
                "editor_enabled": bool # Включен ли универсальный редактор
            }
        """
        # Проверяем настройки редактора
        editor_settings = self.ui_config.get("editor_settings", {})
        editor_enabled = editor_settings.get("use_universal_editor", True)
        
        ui_config = self.ui_config.get(schema_type, {})
        
        # Поддержка верхнеуровневых массивов (characters, locations, beats)
        if schema.get("type") == "array":
            items_schema = schema.get("items", {})
            items_properties = items_schema.get("properties", {})
            # Собираем конфиг поля-массива: ключи вида <prop>_field для подполей
            # Для массивов берем конфигурацию из field_config.items
            raw_field_config = ui_config.get("field_config", {}).get("items", {}) or {}
            array_field_config: Dict[str, Any] = {}
            # Ключи, которые относятся к самому массиву, а не к полям элементов
            array_level_keys = (
                "interface", "tab_title_field", "add_button_text", 
                "widget", "hidden_fields", "display_field", "display_fields", 
                "display_separator", "field_order", "field_groups",
                "hide_selector_label", "hide_edit_label", "hide_label"
            )
            
            # Пробрасываем настройки уровня массива
            for special_key in array_level_keys:
                if special_key in raw_field_config:
                    array_field_config[special_key] = raw_field_config[special_key]
            
            # Переносим настройки полей элементов внутрь конфига массива
            for prop_name, prop_cfg in raw_field_config.items():
                if prop_name in array_level_keys:
                    continue
                # Если ключ уже заканчивается на _field - оставляем как есть
                if prop_name.endswith("_field"):
                    array_field_config[prop_name] = prop_cfg
                else:
                    array_field_config[f"{prop_name}_field"] = prop_cfg
            
            array_field_schema = {"type": "array", "items": items_schema}
            widget_type = self._determine_widget(array_field_schema, array_field_config)
            fields_info = {
                "items": {
                    "name": "items",
                    "type": "array",
                    "title": ui_config.get("title", "Элементы"),
                    "description": "",
                    "required": False,
                    "widget": widget_type,
                    "config": array_field_config,
                    "schema": array_field_schema,
                    "items_schema": items_schema
                }
            }
            groups_info = self._analyze_groups(fields_info, ui_config.get("field_groups", {}))
            validation_info = {
                "required": [],
                "additional_properties": True
            }
            return {
                "fields": fields_info,
                "groups": groups_info,
                "validation": validation_info,
                "schema_type": schema_type,
                "editor_enabled": editor_enabled,
                                "debug_mode": editor_settings.get("debug_mode", False)
            }
        
        # Анализируем поля схемы (для объектов)
        fields_info = self._analyze_fields(schema.get("properties", {}), ui_config.get("field_config", {}))
        
        # Группировка полей
        groups_info = self._analyze_groups(fields_info, ui_config.get("field_groups", {}))
        
        # Валидация
        validation_info = {
            "required": schema.get("required", []),
            "additional_properties": schema.get("additionalProperties", True)
        }
        
        return {
            "fields": fields_info,
            "groups": groups_info,
            "validation": validation_info,
            "schema_type": schema_type,
            "editor_enabled": editor_enabled,
                        "debug_mode": editor_settings.get("debug_mode", False)
        }
    
    def _analyze_fields(self, properties: Dict[str, Any], field_config: Dict[str, Any]) -> Dict[str, Any]:
        """Анализирует поля схемы"""
        fields = {}
        
        for field_name, field_schema in properties.items():
            config = field_config.get(field_name, {})
            
            # Пропускаем скрытые поля
            if config.get("hidden", False):
                continue
            
            field_info = {
                "name": field_name,
                "type": field_schema.get("type"),
                "title": config.get("title", self._humanize_field_name(field_name)),
                "description": field_schema.get("description", ""),
                "required": False,  # Будет установлено в analyze_schema
                "widget": self._determine_widget(field_schema, config),
                "config": config,
                "schema": field_schema
            }
            
            # Специальная обработка для разных типов
            if field_info["type"] == "array":
                field_info["items_schema"] = field_schema.get("items", {})
            elif field_info["type"] == "object":
                field_info["properties"] = field_schema.get("properties", {})
            
            fields[field_name] = field_info
        
        return fields
    
    def _analyze_groups(self, fields_info: Dict[str, Any], groups_config: Dict[str, Any]) -> Dict[str, Any]:
        """Анализирует группировку полей"""
        if not groups_config:
            # Дефолтная группировка - все поля в одну группу
            return {
                "default": {
                    "title": "Настройки",
                    "fields": list(fields_info.keys()),
                    "layout": "vertical",
                    "collapsible": False
                }
            }
        
        groups = {}
        for group_name, group_config in groups_config.items():
            group_fields = group_config.get("fields", [])
            
            # Обработка wildcard "*" для массивов
            if group_fields == ["*"]:
                group_fields = list(fields_info.keys())
            
            # Фильтруем только существующие поля
            valid_fields = [f for f in group_fields if f in fields_info]
            
            if valid_fields:
                groups[group_name] = {
                    "title": group_config.get("title", group_name.replace("_", " ").title()),
                    "fields": valid_fields,
                    "layout": group_config.get("layout", "vertical"),
                    "collapsible": group_config.get("collapsible", False),
                    "collapsed": group_config.get("collapsed",
                                 group_config.get("collapsed_by_default", False)),
                    "hide_label": group_config.get("hide_label", False),
                    "columns": group_config.get("columns", 1)
                }
        
        return groups
    
    def _determine_widget(self, field_schema: Dict[str, Any], config: Dict[str, Any]) -> str:
        """Определяет тип виджета для поля"""
        # Явно указанный виджет имеет приоритет
        if "widget" in config:
            return config["widget"]
        
        field_type = field_schema.get("type")
        
        # Автоматическое определение виджета по типу и свойствам
        if field_type == "string":
            if "enum" in field_schema:
                return "combobox"
            elif field_schema.get("maxLength", 0) > 200:
                return "text_area"
            else:
                return "entry"
        elif field_type == "integer":
            if "minimum" in field_schema and "maximum" in field_schema:
                return "spinbox"
            else:
                return "entry"
        elif field_type == "number":
            return "entry"
        elif field_type == "boolean":
            return "checkbox"
        elif field_type == "array":
            items_schema = field_schema.get("items", {})
            items_type = items_schema.get("type")
            
            if items_type == "string":
                return "list_editor"
            elif items_type == "object":
                # Это массив объектов - используем универсальный редактор
                return "universal_array_editor"
            else:
                return "array_editor"
        elif field_type == "object":
            return "nested_group"
        else:
            return "entry"
    
    def _humanize_field_name(self, field_name: str) -> str:
        """Преобразует имя поля в человеко-читаемое с учетом ui_config"""
        # Берем переводы из внешнего ui_config.json
        ui_labels = self.ui_config.get("translations") or self.ui_config.get("field_labels") or {}
        if isinstance(ui_labels, dict):
            label = ui_labels.get(field_name)
            if isinstance(label, str) and label.strip():
                return label
        # Фоллбек: формируем по имени поля
        return field_name.replace("_", " ").title()


class WidgetFactory:
    """Фабрика виджетов для разных типов данных"""

    @staticmethod
    def save_editor_to_items_list(current_editor, current_index, items_list):
        """Сохраняет данные из current_editor в items_list[current_index].

        Мержит собранные данные с оригинальными, чтобы сохранить hidden-поля.
        Вызывается перед переключением элемента в dropdown-селекторах.
        """
        if current_editor is None or current_index is None:
            return
        if not (0 <= current_index < len(items_list)):
            return
        try:
            form_data = {}
            if isinstance(current_editor, dict):
                if not callable(current_editor.get("get_value")):
                    for field_name, widget_info in current_editor.items():
                        try:
                            form_data[field_name] = widget_info["get_value"]()
                        except Exception:
                            form_data[field_name] = None
                else:
                    form_data = current_editor["get_value"]()
            else:
                form_data = current_editor.get_value()
            original = items_list[current_index]
            if isinstance(original, dict) and isinstance(form_data, dict):
                items_list[current_index] = {**original, **form_data}
            else:
                items_list[current_index] = form_data
        except Exception as e:
            logger.error(f"Ошибка сохранения элемента {current_index}: {e}")

    def __init__(self, parent, on_change_callback: Callable = None, introspector: 'SchemaIntrospector' = None):
        self.parent = parent
        self.on_change = on_change_callback or (lambda: None)
        self.introspector = introspector
    
    def _humanize_field_name(self, field_name: str) -> str:
        """Преобразует имя поля в человеко-читаемое"""
        if self.introspector:
            return self.introspector._humanize_field_name(field_name)
        # Fallback если нет introspector'а
        return field_name.replace("_", " ").title()
    
    def create_widget(self, field_info: Dict[str, Any], value: Any = None) -> Dict[str, Any]:
        """
        Создает виджет для поля
        
        Returns:
            {
                "widget": tkinter_widget,
                "get_value": callable,
                "set_value": callable,
                "validate": callable
            }
        """
        widget_type = field_info["widget"]
        config = field_info.get("config", {})
        
        if widget_type == "entry":
            return self._create_entry(field_info, value)
        elif widget_type == "text_area":
            return self._create_text_area(field_info, value)
        elif widget_type == "combobox":
            return self._create_combobox(field_info, value)
        elif widget_type == "spinbox":
            return self._create_spinbox(field_info, value)
        elif widget_type == "checkbox":
            return self._create_checkbox(field_info, value)
        elif widget_type == "list_editor":
            return self._create_list_editor(field_info, value)
        elif widget_type == "nested_group":
            return self._create_nested_group(field_info, value)
        elif widget_type == "universal_array_editor":
            return self._create_universal_array_editor(field_info, value)
        elif widget_type == "dropdown_selector":
            return self._create_dropdown_selector(field_info, value)
        # Специализированных редакторов больше нет — используем универсальный
        else:
            # Fallback к простому entry
            return self._create_entry(field_info, value)
    
    def _create_entry(self, field_info: Dict[str, Any], value: Any) -> Dict[str, Any]:
        """Создает простое поле ввода"""
        config = field_info.get("config", {})
        widget_var = tk.StringVar(value=str(value or ""))
        widget_var.trace_add('write', lambda *args: self.on_change())
        
        widget = ttk.Entry(
            self.parent,
            textvariable=widget_var,
            width=config.get("width", 30)
        )
        if config.get("readonly", False):
            try:
                widget.state(["readonly"])  # ttk API
            except Exception:
                pass
        
        # Добавляем контекстное меню
        add_context_menu(widget)
        
        def get_typed_value():
            """Получает значение с правильным типом на основе схемы"""
            raw_value = widget_var.get()
            if not raw_value.strip():
                return None
            
            # Определяем тип из схемы
            field_type = field_info.get("schema", {}).get("type")
            
            try:
                if field_type == "integer":
                    return int(raw_value)
                elif field_type == "number":
                    return float(raw_value)
                elif field_type == "boolean":
                    return raw_value.lower() in ("true", "1", "yes", "on")
                else:
                    return raw_value
            except (ValueError, TypeError):
                # Если преобразование не удалось, возвращаем строку
                return raw_value
        
        return {
            "widget": widget,
            "get_value": get_typed_value,
            "set_value": lambda v: widget_var.set(str(v or "")),
            "validate": lambda: self._validate_string(widget_var.get(), field_info)
        }
    
    def _create_text_area(self, field_info: Dict[str, Any], value: Any) -> Dict[str, Any]:
        """Создает многострочное поле ввода"""
        config = field_info.get("config", {})
        
        widget = tk.Text(
            self.parent,
            height=config.get("height", 3),
            width=config.get("width", 50),
            wrap=tk.WORD
        )
        
        if value:
            widget.insert("1.0", str(value))
        if config.get("readonly", False):
            try:
                widget.configure(state="disabled")
            except Exception:
                pass
        
        widget.bind("<KeyRelease>", lambda e: self.on_change())
        
        # Добавляем контекстное меню
        add_context_menu(widget)
        
        return {
            "widget": widget,
            "get_value": lambda: widget.get("1.0", tk.END).rstrip('\n'),
            "set_value": lambda v: (widget.delete("1.0", tk.END), widget.insert("1.0", str(v or ""))),
            "validate": lambda: self._validate_string(widget.get("1.0", tk.END).rstrip('\n'), field_info)
        }
    
    def _create_combobox(self, field_info: Dict[str, Any], value: Any) -> Dict[str, Any]:
        """Создает combobox"""
        config = field_info.get("config", {})
        schema = field_info.get("schema", {})
        
        # Значения из конфига или схемы
        values = list(config.get("values") or schema.get("enum", []))
        
        # Если текущее значение не входит в список, добавляем его
        current_value = str(value or "")
        if current_value and current_value not in values:
            values.insert(0, current_value)
        
        widget_var = tk.StringVar(value=current_value)
        widget_var.trace_add('write', lambda *args: self.on_change())
        
        widget = ttk.Combobox(
            self.parent,
            textvariable=widget_var,
            values=values,
            width=config.get("width", 25),
            state="readonly" if config.get("readonly", False) else "normal"  # По умолчанию редактируемый
        )
        
        # Добавляем контекстное меню (если combobox редактируемый)
        if not config.get("readonly", False):
            add_context_menu(widget)
        
        return {
            "widget": widget,
            "get_value": lambda: widget_var.get(),
            "set_value": lambda v: widget_var.set(str(v or "")),
            "validate": lambda: self._validate_enum(widget_var.get(), field_info)
        }
    
    def _create_spinbox(self, field_info: Dict[str, Any], value: Any) -> Dict[str, Any]:
        """Создает spinbox для чисел"""
        schema = field_info.get("schema", {})
        config = field_info.get("config", {})
        
        min_val = schema.get("minimum", 0)
        max_val = schema.get("maximum", 100)
        
        widget_var = tk.IntVar(value=int(value or min_val))
        widget_var.trace_add('write', lambda *args: self.on_change())
        
        widget = ttk.Spinbox(
            self.parent,
            from_=min_val,
            to=max_val,
            textvariable=widget_var,
            width=config.get("width", 10)
        )
        if config.get("readonly", False):
            try:
                widget.state(["readonly"])  # ttk API
            except Exception:
                pass
        
        # Добавляем контекстное меню
        add_context_menu(widget)
        
        return {
            "widget": widget,
            "get_value": lambda: widget_var.get(),
            "set_value": lambda v: widget_var.set(int(v or min_val)),
            "validate": lambda: self._validate_number(widget_var.get(), field_info)
        }
    
    def _create_checkbox(self, field_info: Dict[str, Any], value: Any) -> Dict[str, Any]:
        """Создает checkbox"""
        widget_var = tk.BooleanVar(value=bool(value))
        widget_var.trace_add('write', lambda *args: self.on_change())
        
        widget = ttk.Checkbutton(
            self.parent,
            variable=widget_var,
            text=field_info.get("title", "")
        )
        
        return {
            "widget": widget,
            "get_value": lambda: widget_var.get(),
            "set_value": lambda v: widget_var.set(bool(v)),
            "validate": lambda: True  # Boolean всегда валиден
        }
    
    def _create_list_editor(self, field_info: Dict[str, Any], value: Any) -> Dict[str, Any]:
        """Создает редактор списка строк"""
        config = field_info.get("config", {})
        
        # Контейнер для списка
        list_frame = ttk.Frame(self.parent)
        
        # Список значений
        items_list = value if isinstance(value, list) else []
        item_vars = []
        item_widgets = []
        
        def rebuild_list():
            # Очищаем старые виджеты
            for widget in item_widgets:
                widget.destroy()
            item_widgets.clear()
            item_vars.clear()
            
            # Создаем новые
            for i, item in enumerate(items_list):
                item_var = tk.StringVar(value=str(item))
                item_var.trace_add('write', lambda *args: self.on_change())
                item_vars.append(item_var)
                
                item_frame = ttk.Frame(list_frame)
                item_frame.pack(fill="x", pady=1)
                
                entry = ttk.Entry(item_frame, textvariable=item_var, width=40)
                entry.pack(side="left", fill="x", expand=True)
                
                # Кнопка удаления
                def remove_item(index=i):
                    if 0 <= index < len(items_list):
                        items_list.pop(index)
                        rebuild_list()
                        self.on_change()
                
                remove_btn = ttk.Button(item_frame, text="❌", width=3, command=remove_item)
                remove_btn.pack(side="right", padx=(5, 0))
                
                item_widgets.extend([item_frame, entry, remove_btn])
        
        def add_item():
            items_list.append("")
            rebuild_list()
            self.on_change()
        
        # Создаем список
        rebuild_list()
        
        # Кнопка добавления
        add_text = config.get("add_button_text", "+ Добавить элемент")
        add_button = ttk.Button(list_frame, text=add_text, command=add_item)
        add_button.pack(pady=5)
        
        return {
            "widget": list_frame,
            "get_value": lambda: [var.get() for var in item_vars if var.get().strip()],
            "set_value": lambda v: (items_list.clear(), items_list.extend(v if isinstance(v, list) else []), rebuild_list()),
            "validate": lambda: True  # Всегда валиден
        }
    
    def _create_nested_group(self, field_info: Dict[str, Any], value: Any) -> Dict[str, Any]:
        """Создает группу для вложенного объекта"""
        config = field_info.get("config", {})
        schema = field_info.get("schema", {})
        
        # Контейнер для группы
        group_frame = ttk.LabelFrame(
            self.parent,
            text=config.get("title", field_info.get("title", "")),
            padding=5
        )
        
        # Создаем виджеты для свойств объекта
        nested_widgets = {}
        properties = schema.get("properties", {})
        
        for prop_name, prop_schema in properties.items():
            prop_value = (value or {}).get(prop_name) if isinstance(value, dict) else None
            
            # Создаем упрощенную информацию о поле
            prop_info = {
                "name": prop_name,
                "type": prop_schema.get("type"),
                "title": prop_name.replace("_", " ").title(),
                "widget": self._determine_simple_widget(prop_schema),
                "schema": prop_schema,
                "config": {}
            }
            
            prop_frame = ttk.Frame(group_frame)
            prop_frame.pack(fill="x", pady=2)
            
            # Метка (показываем только если не скрыта)
            hide_label = prop_info.get("config", {}).get("hide_label", False)
            if not hide_label:
                ttk.Label(prop_frame, text=f"{prop_info['title']}:").pack(side="left", anchor="w")
            
            # Создаем виджет в отдельной фабрике для вложенных элементов
            nested_factory = WidgetFactory(prop_frame, self.on_change, self.introspector)
            widget_info = nested_factory.create_widget(prop_info, prop_value)
            widget_info["widget"].pack(side="right", fill="x", expand=True, padx=(10, 0))
            
            nested_widgets[prop_name] = widget_info
        
        return {
            "widget": group_frame,
            "get_value": lambda: {name: widget["get_value"]() for name, widget in nested_widgets.items()},
            "set_value": lambda v: [widget["set_value"](v.get(name) if isinstance(v, dict) else None) 
                                  for name, widget in nested_widgets.items()],
            "validate": lambda: all(widget["validate"]() for widget in nested_widgets.values())
        }
    
    def _create_universal_array_editor(self, field_info: Dict[str, Any], value: Any) -> Dict[str, Any]:
        """Универсальный редактор массивов объектов"""
        config = field_info.get("config", {})
        schema = field_info.get("schema", {})
        items_schema = schema.get("items", {})
        items_properties = items_schema.get("properties", {})
        
        # Читаем конфигурацию для всех полей объекта из схемы и конфига
        subfield_configs = {}
        hidden_fields = config.get("hidden_fields", [])
        
        # Передаем field_order, hidden_fields и field_groups в subfield_configs
        field_order = config.get("field_order", [])
        if field_order:
            subfield_configs["field_order"] = field_order
        
        if hidden_fields:
            subfield_configs["hidden_fields"] = hidden_fields
            
        field_groups = config.get("field_groups", {})
        if field_groups:
            subfield_configs["field_groups"] = field_groups
        
        # Получаем все поля из схемы И из данных
        all_field_names = set(items_properties.keys())
        if isinstance(value, list) and value:
            # Добавляем поля из первого элемента данных
            first_item = value[0] if value else {}
            if isinstance(first_item, dict):
                all_field_names.update(first_item.keys())
        
        for prop_name in all_field_names:
            field_config = config.get(f"{prop_name}_field", {})
            
            # Проверяем, скрыто ли поле через hidden_fields список
            if prop_name in hidden_fields:
                field_config = {**field_config, "hidden": True}
            
            subfield_configs[prop_name] = field_config
        
        # Контейнер для редактора
        array_frame = ttk.Frame(self.parent)
        
        # Получаем список объектов
        items_list = value if isinstance(value, list) else []
        
        # Определяем тип интерфейса из конфига
        # Для совместимости проверяем и widget, и interface
        # Дефолт — dropdown_selector
        interface_type = config.get("interface") or config.get("widget", "dropdown_selector")
        
        # Мапинг widget названий на interface типы
        if interface_type == "universal_array_editor":
            interface_type = config.get("interface", "dropdown_selector")  # Используем interface если есть
        elif interface_type == "dropdown_selector":
            interface_type = "dropdown_selector"  # Сохраняем как есть
        
        if interface_type == "tabs":
            return self._create_tabbed_array_editor(array_frame, items_list, items_schema, subfield_configs, config)
        elif interface_type == "list":
            return self._create_list_array_editor(array_frame, items_list, items_schema, subfield_configs, config)
        elif interface_type == "accordion":
            return self._create_accordion_array_editor(array_frame, items_list, items_schema, subfield_configs, config)
        elif interface_type == "dropdown_selector":
            # Используем dropdown_selector (как в shots)
            # Передаем subfield_configs отдельно для правильной обработки скрытых полей
            return self._create_dropdown_selector_with_subfields(array_frame, items_list, items_schema, subfield_configs, config)
        else:
            # По умолчанию dropdown_selector
            return self._create_dropdown_selector_with_subfields(array_frame, items_list, items_schema, subfield_configs, config)
    
    def _create_tabbed_array_editor(self, parent_frame, items_list, items_schema, subfield_configs, config):
        """Создает редактор с вкладками для каждого элемента"""
        # Notebook для вкладок
        notebook = ttk.Notebook(parent_frame)
        notebook.pack(fill="both", expand=True)
        
        # Хранилище виджетов
        item_widgets = {}
        
        def create_item_tab(item_data: Dict[str, Any], item_index: int):
            """Создает вкладку для одного элемента"""
            # Определяем заголовок вкладки
            tab_title = self._get_item_tab_title(item_data, item_index, config)
            
            # Фрейм для элемента
            item_frame = ttk.Frame(notebook)
            notebook.add(item_frame, text=tab_title)
            
            # Создаем форму для объекта используя схему
            item_widgets[item_index] = self._create_object_form(
                item_frame, item_data, items_schema, subfield_configs
            )
        
        def rebuild_tabs():
            """Пересоздает все вкладки"""
            for tab in notebook.tabs():
                notebook.forget(tab)
            item_widgets.clear()
            
            for i, item_data in enumerate(items_list):
                create_item_tab(item_data, i)
        
        def add_new_item():
            """Добавляет новый элемент"""
            new_item = self._create_default_object(items_schema, config)
            items_list.append(new_item)
            rebuild_tabs()
            notebook.select(len(items_list) - 1)
            self.on_change()
        
        # Создаем начальные вкладки
        rebuild_tabs()
        
        # Кнопка добавления
        add_button_frame = ttk.Frame(parent_frame)
        add_button_frame.pack(fill="x", pady=5)
        add_text = config.get("add_button_text", "+ Добавить элемент")
        ttk.Button(add_button_frame, text=add_text, command=add_new_item).pack(side="left")
        
        # Кнопка копирования текущей вкладки в буфер обмена
        def copy_current_tab_to_clipboard():
            import json as _json
            try:
                idx = notebook.index(notebook.select())
            except Exception:
                idx = -1
            if not (0 <= idx < len(items_list)):
                return
            # Если для вкладки уже есть виджеты, соберем актуальные значения
            data_to_copy = None
            try:
                if idx in item_widgets and isinstance(item_widgets[idx], dict):
                    tmp = {}
                    for fname, winfo in item_widgets[idx].items():
                        try:
                            tmp[fname] = winfo["get_value"]()
                        except Exception:
                            tmp[fname] = None
                    data_to_copy = tmp
                else:
                    data_to_copy = items_list[idx]
            except Exception:
                data_to_copy = items_list[idx]
            try:
                text = _json.dumps(data_to_copy, ensure_ascii=False, indent=2)
                root = parent_frame.winfo_toplevel()
                root.clipboard_clear()
                root.clipboard_append(text)
            except Exception:
                pass
        ttk.Button(add_button_frame, text="📋 Копировать текущий", command=copy_current_tab_to_clipboard).pack(side="right")
        
        def get_array_data():
            """Получает данные всех элементов"""
            result = []
            for i, item_data in enumerate(items_list):
                if i in item_widgets:
                    item_result = {}
                    widgets = item_widgets[i]
                    
                    for field_name, widget_info in widgets.items():
                        if not subfield_configs.get(field_name, {}).get("hidden", False):
                            item_result[field_name] = widget_info["get_value"]()
                        elif field_name in item_data:
                            item_result[field_name] = item_data[field_name]
                    
                    result.append(item_result)
                else:
                    result.append(item_data)
            return result
        
        def set_array_data(new_data):
            """Устанавливает новые данные"""
            items_list.clear()
            if isinstance(new_data, list):
                items_list.extend(new_data)
            rebuild_tabs()
        
        return {
            "widget": parent_frame,
            "get_value": get_array_data,
            "set_value": set_array_data,
            "validate": lambda: True
        }

    def _create_list_array_editor(self, parent_frame, items_list, items_schema, subfield_configs, config):
        """Создает редактор в виде списка элементов с краткими заголовками"""
        container = ttk.Frame(parent_frame)
        container.pack(fill="both", expand=True)
        
        items_frame = ttk.Frame(container)
        items_frame.pack(fill="both", expand=True)
        
        item_widgets = {}
        
        def rebuild_list():
            for child in items_frame.winfo_children():
                child.destroy()
            item_widgets.clear()
            
            for i, item_data in enumerate(items_list):
                row = ttk.Frame(items_frame)
                row.pack(fill="x", pady=2)
                
                title = self._get_item_tab_title(item_data, i, config)
                ttk.Label(row, text=title).pack(side="left")
                
                def edit(index=i):
                    edit_win = tk.Toplevel(items_frame)
                    edit_win.title(title)
                    form_frame = ttk.Frame(edit_win, padding=10)
                    form_frame.pack(fill="both", expand=True)
                    widgets = self._create_object_form(form_frame, items_list[index], items_schema, subfield_configs)
                    item_widgets[index] = widgets
                    
                    def save_and_close():
                        # значения берутся через get_value при сборе данных
                        edit_win.destroy()
                        self.on_change()
                    ttk.Button(form_frame, text="Сохранить", command=save_and_close).pack(pady=5)
                
                # Копирование данного элемента
                def copy_row(idx=i):
                    import json as _json
                    data_to_copy = None
                    try:
                        if idx in item_widgets and isinstance(item_widgets[idx], dict):
                            tmp = {}
                            for fname, winfo in item_widgets[idx].items():
                                try:
                                    tmp[fname] = winfo["get_value"]()
                                except Exception:
                                    tmp[fname] = None
                            data_to_copy = tmp
                        else:
                            data_to_copy = items_list[idx]
                    except Exception:
                        data_to_copy = items_list[idx]
                    try:
                        text = _json.dumps(data_to_copy, ensure_ascii=False, indent=2)
                        root = parent_frame.winfo_toplevel()
                        root.clipboard_clear()
                        root.clipboard_append(text)
                    except Exception:
                        pass
                ttk.Button(row, text="📋", width=3, command=copy_row).pack(side="right", padx=(4,0))
                ttk.Button(row, text="Изменить", command=edit).pack(side="right")
        
        def add_new_item():
            items_list.append(self._create_default_object(items_schema, config))
            rebuild_list()
            self.on_change()
        
        rebuild_list()
        
        add_text = config.get("add_button_text", "+ Добавить элемент")
        ttk.Button(container, text=add_text, command=add_new_item).pack(pady=5)
        
        def get_array_data():
            result = []
            for i, item in enumerate(items_list):
                if i in item_widgets:
                    item_result = {}
                    for field_name, w in item_widgets[i].items():
                        if not subfield_configs.get(field_name, {}).get("hidden", False):
                            item_result[field_name] = w["get_value"]()
                        elif field_name in item:
                            item_result[field_name] = item[field_name]
                    result.append(item_result)
                else:
                    result.append(item)
            return result
        
        def set_array_data(new_data):
            items_list.clear()
            if isinstance(new_data, list):
                items_list.extend(new_data)
            rebuild_list()
        
        return {
            "widget": parent_frame,
            "get_value": get_array_data,
            "set_value": set_array_data,
            "validate": lambda: True
        }

    def _create_accordion_array_editor(self, parent_frame, items_list, items_schema, subfield_configs, config):
        """Создает редактор с аккордеоном (сворачиваемые панели)"""
        container = ttk.Frame(parent_frame)
        container.pack(fill="both", expand=True)
        
        panels_frame = ttk.Frame(container)
        panels_frame.pack(fill="both", expand=True)
        
        item_widgets = {}
        
        def rebuild_panels():
            for child in panels_frame.winfo_children():
                child.destroy()
            item_widgets.clear()
            
            for i, item_data in enumerate(items_list):
                title = self._get_item_tab_title(item_data, i, config)
                
                # Заголовок панели
                header = ttk.Frame(panels_frame)
                header.pack(fill="x", pady=(4, 0))
                
                expanded_var = tk.BooleanVar(value=False)
                
                def toggle(body_frame, var=expanded_var):
                    if var.get():
                        body_frame.pack_forget()
                        var.set(False)
                    else:
                        body_frame.pack(fill="x")
                        var.set(True)
                
                ttk.Checkbutton(header, text=title, variable=expanded_var, command=lambda idx=i: None).pack(side="left")
                
                # Кнопка копирования для этого элемента
                def copy_panel(idx=i):
                    import json as _json
                    data_to_copy = None
                    try:
                        if idx in item_widgets and isinstance(item_widgets[idx], dict):
                            tmp = {}
                            for fname, winfo in item_widgets[idx].items():
                                try:
                                    tmp[fname] = winfo["get_value"]()
                                except Exception:
                                    tmp[fname] = None
                            data_to_copy = tmp
                        else:
                            data_to_copy = items_list[idx]
                    except Exception:
                        data_to_copy = items_list[idx]
                    try:
                        text = _json.dumps(data_to_copy, ensure_ascii=False, indent=2)
                        root = parent_frame.winfo_toplevel()
                        root.clipboard_clear()
                        root.clipboard_append(text)
                    except Exception:
                        pass
                ttk.Button(header, text="📋", width=3, command=copy_panel).pack(side="right")
                
                # Тело панели
                body = ttk.Frame(panels_frame)
                # изначально свернуто
                
                widgets = self._create_object_form(body, item_data, items_schema, subfield_configs)
                item_widgets[i] = widgets
                
                def on_header_click(b=body, v=expanded_var):
                    if v.get():
                        b.pack_forget()
                        v.set(False)
                    else:
                        b.pack(fill="x")
                        v.set(True)
                
                header.bind("<Button-1>", lambda e, h=header: on_header_click())
        
        def add_new_item():
            items_list.append(self._create_default_object(items_schema, config))
            rebuild_panels()
            self.on_change()
        
        rebuild_panels()
        
        add_text = config.get("add_button_text", "+ Добавить элемент")
        ttk.Button(container, text=add_text, command=add_new_item).pack(pady=5)
        
        def get_array_data():
            result = []
            for i, item in enumerate(items_list):
                if i in item_widgets:
                    item_result = {}
                    for field_name, w in item_widgets[i].items():
                        if not subfield_configs.get(field_name, {}).get("hidden", False):
                            item_result[field_name] = w["get_value"]()
                        elif field_name in item:
                            item_result[field_name] = item[field_name]
                    result.append(item_result)
                else:
                    result.append(item)
            return result
        
        def set_array_data(new_data):
            items_list.clear()
            if isinstance(new_data, list):
                items_list.extend(new_data)
            rebuild_panels()
        
        return {
            "widget": parent_frame,
            "get_value": get_array_data,
            "set_value": set_array_data,
            "validate": lambda: True
        }
    
    def _create_object_form(self, parent, object_data, object_schema, subfield_configs):
        """Создает форму для объекта на основе схемы"""
        properties = object_schema.get("properties", {})
        widgets = {}
        
        # Проверяем, есть ли группировка полей
        field_groups = subfield_configs.get("field_groups", {})
        if field_groups:
            return self._create_grouped_object_form(parent, object_data, object_schema, subfield_configs, field_groups)
        
        # Применяем порядок полей из field_order
        field_order = subfield_configs.get("field_order", [])
        
        # Сортируем поля: сначала из field_order, потом остальные в алфавитном порядке
        ordered_fields = []
        
        # Получаем поля из схемы И из данных объекта
        schema_fields = set(properties.keys())
        data_fields = set(object_data.keys()) if isinstance(object_data, dict) else set()
        all_fields = schema_fields.union(data_fields)
        remaining_fields = all_fields.copy()
        
        # Добавляем поля в указанном порядке
        for field_name in field_order:
            if field_name in all_fields:
                ordered_fields.append(field_name)
                remaining_fields.discard(field_name)
        
        # Добавляем оставшиеся поля в алфавитном порядке
        ordered_fields.extend(sorted(remaining_fields))
        
        # Группируем поля для inline отображения
        i = 0
        while i < len(ordered_fields):
            prop_name = ordered_fields[i]
            prop_schema = properties.get(prop_name, {"type": "string"})  # Дефолтная схема если не найдена
            field_key = f"{prop_name}_field"
            prop_config = subfield_configs.get(field_key, subfield_configs.get(prop_name, {}))
            
            # Если конфигурация не найдена, создаем базовую
            if not prop_config:
                prop_config = {"widget": "entry"}
            
            # Пропускаем скрытые поля
            if prop_config.get("hidden", False) or prop_name in subfield_configs.get("hidden_fields", []):
                i += 1
                continue
            
            # Проверяем, является ли поле inline
            is_inline = prop_config.get("inline", False)
            
            if is_inline:
                # Собираем группу inline полей
                inline_group = [prop_name]
                j = i + 1
                while j < len(ordered_fields):
                    next_field = ordered_fields[j]
                    next_key = f"{next_field}_field"
                    next_config = subfield_configs.get(next_key, subfield_configs.get(next_field, {}))
                    
                    # Пропускаем скрытые поля
                    if next_config.get("hidden", False) or next_field in subfield_configs.get("hidden_fields", []):
                        j += 1
                        continue
                    
                    if next_config.get("inline", False):
                        inline_group.append(next_field)
                        j += 1
                    else:
                        break
                
                # Создаем inline фрейм для группы
                inline_frame = ttk.Frame(parent)
                inline_frame.pack(fill="x", pady=2)
                
                for field_name in inline_group:
                    field_schema = properties.get(field_name, {"type": "string"})
                    field_key = f"{field_name}_field"
                    field_config = subfield_configs.get(field_key, subfield_configs.get(field_name, {}))
                    
                    # Если конфигурация не найдена, создаем базовую
                    if not field_config:
                        field_config = {"widget": "entry"}
                    
                    field_info = {
                        "name": field_name,
                        "type": field_schema.get("type"),
                        "title": field_config.get("label", self.introspector._humanize_field_name(field_name)),
                        "widget": self.introspector._determine_widget(field_schema, field_config),
                        "schema": field_schema,
                        "config": field_config
                    }
                    
                    # Контейнер для inline поля
                    field_container = ttk.Frame(inline_frame)
                    field_container.pack(side="left", padx=(0, 10))
                    
                    # Метка (показываем только если не скрыта)
                    hide_label = field_config.get("hide_label", False)
                    if not hide_label:
                        ttk.Label(field_container, text=f"{field_info['title']}:").pack(anchor="w")
                    
                    # Создаем виджет
                    field_value = object_data.get(field_name) if isinstance(object_data, dict) else None
                    widget_factory = WidgetFactory(field_container, self.on_change, self.introspector)
                    widget_info = widget_factory.create_widget(field_info, field_value)
                    
                    # Применяем ширину если указана
                    width = field_config.get("width")
                    if width and hasattr(widget_info["widget"], "config"):
                        try:
                            widget_info["widget"].config(width=width)
                        except Exception:
                            pass
                    
                    widget_info["widget"].pack(pady=(2, 5))
                    widgets[field_name] = widget_info
                
                i = j  # Переходим к следующему не-inline полю
            else:
                # Обычное поле
                field_info = {
                    "name": prop_name,
                    "type": prop_schema.get("type"),
                    "title": prop_config.get("label", self.introspector._humanize_field_name(prop_name)),
                    "widget": self.introspector._determine_widget(prop_schema, prop_config),
                    "schema": prop_schema,
                    "config": prop_config
                }
                
                # Фрейм для поля
                field_frame = ttk.Frame(parent)
                field_frame.pack(fill="x", pady=2)
                
                # Метка (показываем только если не скрыта)
                hide_label = prop_config.get("hide_label", False)
                if not hide_label:
                    ttk.Label(field_frame, text=f"{field_info['title']}:").pack(anchor="w")
                
                # Создаем виджет
                prop_value = object_data.get(prop_name) if isinstance(object_data, dict) else None
                widget_factory = WidgetFactory(field_frame, self.on_change, self.introspector)
                widget_info = widget_factory.create_widget(field_info, prop_value)
                widget_info["widget"].pack(fill="x", pady=(2, 5))
                
                widgets[prop_name] = widget_info
                i += 1
        
        return widgets
    
    def _create_grouped_object_form(self, parent, object_data, object_schema, subfield_configs, field_groups):
        """Создает форму для объекта с группировкой полей"""
        properties = object_schema.get("properties", {})
        widgets = {}
        
        # Создаем группы
        for group_name, group_config in field_groups.items():
            group_fields = group_config.get("fields", [])
            
            # Обработка wildcard "*" - все поля кроме уже использованных
            if group_fields == ["*"]:
                used_fields = set()
                for other_group_name, other_group_config in field_groups.items():
                    if other_group_name != group_name:
                        other_fields = other_group_config.get("fields", [])
                        if other_fields != ["*"]:
                            used_fields.update(other_fields)
                group_fields = [f for f in properties.keys() if f not in used_fields]
            
            # Фильтруем только существующие поля
            valid_fields = [f for f in group_fields if f in properties]
            hidden_fields = subfield_configs.get("hidden_fields", [])
            valid_fields = [f for f in valid_fields if f not in hidden_fields]
            
            if not valid_fields:
                continue
                
            # Создаем группу
            group_title = group_config.get("title", "")
            hide_label = group_config.get("hide_label", False)
            collapsible = group_config.get("collapsible", False)
            collapsed = group_config.get("collapsed",
                        group_config.get("collapsed_by_default", False))
            
            if hide_label:
                group_title = ""
            
            if collapsible:
                # Создаем сворачиваемую группу
                group_frame = create_collapsible_frame(parent, group_title, collapsed)
            else:
                # Обычная группа
                if group_title:
                    group_frame = ttk.LabelFrame(parent, text=group_title, padding=10)
                else:
                    group_frame = ttk.Frame(parent)
                group_frame.pack(fill="x", pady=5)
            
            # Создаем поля в группе с поддержкой inline
            i = 0
            while i < len(valid_fields):
                field_name = valid_fields[i]
                prop_schema = properties[field_name]
                field_key = f"{field_name}_field"
                prop_config = subfield_configs.get(field_key, subfield_configs.get(field_name, {}))
                
                # Проверяем, является ли поле inline
                is_inline = prop_config.get("inline", False)
                
                if is_inline:
                    # Собираем группу inline полей
                    inline_group = [field_name]
                    j = i + 1
                    while j < len(valid_fields):
                        next_field = valid_fields[j]
                        next_key = f"{next_field}_field"
                        next_config = subfield_configs.get(next_key, subfield_configs.get(next_field, {}))
                        
                        if next_config.get("inline", False):
                            inline_group.append(next_field)
                            j += 1
                        else:
                            break
                    
                    # Создаем inline фрейм для группы
                    inline_frame = ttk.Frame(group_frame)
                    inline_frame.pack(fill="x", pady=2)
                    
                    for inline_field_name in inline_group:
                        inline_field_schema = properties[inline_field_name]
                        inline_field_key = f"{inline_field_name}_field"
                        inline_field_config = subfield_configs.get(inline_field_key, subfield_configs.get(inline_field_name, {}))
                        
                        field_info = {
                            "name": inline_field_name,
                            "type": inline_field_schema.get("type"),
                            "title": inline_field_config.get("label", self.introspector._humanize_field_name(inline_field_name)),
                            "widget": self.introspector._determine_widget(inline_field_schema, inline_field_config),
                            "schema": inline_field_schema,
                            "config": inline_field_config
                        }
                        
                        # Контейнер для inline поля
                        field_container = ttk.Frame(inline_frame)
                        field_container.pack(side="left", padx=(0, 10))
                        
                        # Метка (показываем только если не скрыта)
                        hide_label = inline_field_config.get("hide_label", False)
                        if not hide_label:
                            ttk.Label(field_container, text=f"{field_info['title']}:").pack(anchor="w")
                        
                        # Создаем виджет
                        field_value = object_data.get(inline_field_name) if isinstance(object_data, dict) else None
                        widget_factory = WidgetFactory(field_container, self.on_change, self.introspector)
                        widget_info = widget_factory.create_widget(field_info, field_value)
                        
                        # Применяем ширину если указана
                        width = inline_field_config.get("width")
                        if width and hasattr(widget_info["widget"], "config"):
                            try:
                                widget_info["widget"].config(width=width)
                            except Exception:
                                pass
                        
                        widget_info["widget"].pack(pady=(2, 5))
                        widgets[inline_field_name] = widget_info
                    
                    i = j  # Переходим к следующему не-inline полю
                else:
                    # Обычное поле
                    field_info = {
                        "name": field_name,
                        "type": prop_schema.get("type"),
                        "title": prop_config.get("label", self.introspector._humanize_field_name(field_name)),
                        "widget": self.introspector._determine_widget(prop_schema, prop_config),
                        "schema": prop_schema,
                        "config": prop_config
                    }
                    
                    # Фрейм для поля
                    field_frame = ttk.Frame(group_frame)
                    field_frame.pack(fill="x", pady=2)
                    
                    # Метка (показываем только если не скрыта)
                    hide_field_label = prop_config.get("hide_label", False)
                    if not hide_field_label:
                        ttk.Label(field_frame, text=f"{field_info['title']}:").pack(anchor="w")
                    
                    # Создаем виджет
                    prop_value = object_data.get(field_name) if isinstance(object_data, dict) else None
                    widget_factory = WidgetFactory(field_frame, self.on_change, self.introspector)
                    widget_info = widget_factory.create_widget(field_info, prop_value)
                    widget_info["widget"].pack(fill="x", pady=(2, 5))
                    
                    widgets[field_name] = widget_info
                    i += 1
        
        return widgets
    
    def _get_item_tab_title(self, item_data, index, config):
        """Определяет заголовок вкладки для элемента"""
        # Используем поле из настроек UI
        title_field = config.get("tab_title_field")
        if title_field and isinstance(item_data, dict):
            title = item_data.get(title_field, "")
            if title:
                return f"{index + 1}. {str(title)[:20]}..."
        
        # Дефолтный заголовок если поле не настроено или пустое
        return f"Элемент {index + 1}"
    
    def _create_default_object(self, schema, field_config=None):
        """Создает объект по умолчанию на основе схемы"""
        properties = schema.get("properties", {})
        required_fields = schema.get("required", [])
        defaults = {}
        
        for prop_name, prop_schema in properties.items():
            prop_type = prop_schema.get("type")
            if prop_type == "string":
                # Для обязательных полей с minLength > 0 создаем осмысленное значение
                min_length = prop_schema.get("minLength", 0)
                if prop_name in required_fields and min_length > 0:
                    # Берем placeholder из field_config если есть
                    placeholder = self._get_field_placeholder_from_config(prop_name, field_config)
                    defaults[prop_name] = placeholder or f"Новое значение для {prop_name}"
                else:
                    defaults[prop_name] = ""
            elif prop_type == "integer":
                defaults[prop_name] = prop_schema.get("minimum", 0)
            elif prop_type == "number":
                defaults[prop_name] = 0.0
            elif prop_type == "boolean":
                defaults[prop_name] = False
            elif prop_type == "array":
                defaults[prop_name] = []
            elif prop_type == "object":
                defaults[prop_name] = {}
        
        return defaults
    
    def _get_field_placeholder_from_config(self, field_name, field_config):
        """Берет placeholder из конфигурации поля"""
        if not field_config:
            return None
            
        # Ищем в конфиге для конкретного поля 
        field_key = f"{field_name}_field"
        field_settings = field_config.get(field_key, {})
        
        return field_settings.get("placeholder")
    
    def _create_dropdown_selector_with_subfields(self, parent_frame, items_list, items_schema, subfield_configs, config):
        """Создает выпадающий список для выбора элемента массива с правильным использованием subfield_configs"""
        import tkinter as tk
        from tkinter import ttk
        
        # Поддержка составного отображения
        display_field = config.get("display_field", "title")
        display_fields = config.get("display_fields", [])
        display_separator = config.get("display_separator", " - ")
        
        def get_item_display_text(item, index):
            """Получает текст для отображения элемента"""
            if display_fields:
                # Составное отображение из нескольких полей
                parts = []
                for field in display_fields:
                    value = item.get(field, "")
                    if value:
                        parts.append(str(value))
                if parts:
                    return display_separator.join(parts)
                else:
                    return f"Элемент {index + 1}"
            else:
                # Простое отображение одного поля
                return item.get(display_field, f"Элемент {index + 1}")
        
        # Основной фрейм
        main_frame = ttk.Frame(parent_frame)
        
        # Фрейм для выпадающего списка и кнопок
        selector_frame = ttk.Frame(main_frame)
        selector_frame.pack(fill="x", pady=(0, 5))
        
        # Выпадающий список
        selected_var = tk.StringVar()
        dropdown = ttk.Combobox(selector_frame, textvariable=selected_var, state="readonly")
        dropdown.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        # Кнопки
        button_frame = ttk.Frame(selector_frame)
        button_frame.pack(side="right")
        
        edit_btn = ttk.Button(button_frame, text="Изменить", state="disabled")
        edit_btn.pack(side="left", padx=(0, 2))
        
        add_btn = ttk.Button(button_frame, text=config.get("add_button_text", "➕ Добавить"))
        add_btn.pack(side="left", padx=(0, 2))
        
        # Кнопка копирования текущего элемента в буфер обмена
        copy_btn = ttk.Button(button_frame, text="📋 Копировать", state="disabled")
        copy_btn.pack(side="left", padx=(0, 2))
        
        delete_btn = ttk.Button(button_frame, text="🗑️", state="disabled")
        delete_btn.pack(side="left")
        
        # Фрейм для редактирования выбранного элемента
        hide_edit_label = config.get("hide_edit_label", False)
        edit_title = "" if hide_edit_label else "Редактирование элемента"
        edit_frame = ttk.LabelFrame(main_frame, text=edit_title)
        edit_frame.pack(fill="both", expand=True, pady=(5, 0))
        
        current_editor = None
        current_index = None
        
        def update_dropdown():
            """Обновляет список в dropdown"""
            dropdown['values'] = [
                f"{i+1}. {get_item_display_text(item, i)}"
                for i, item in enumerate(items_list)
            ]
            
            if items_list:
                edit_btn.config(state="normal")
                delete_btn.config(state="normal")
                copy_btn.config(state="normal")
                if selected_var.get() == "":
                    dropdown.current(0)
                    on_selection_change()
            else:
                edit_btn.config(state="disabled")
                delete_btn.config(state="disabled")
                copy_btn.config(state="disabled")
                clear_editor()
        
        def _save_current_item():
            WidgetFactory.save_editor_to_items_list(
                current_editor, current_index, items_list
            )

        def on_selection_change(event=None):
            """Обработчик изменения выбора в dropdown"""
            nonlocal current_editor, current_index

            _save_current_item()

            selection = dropdown.current()
            if 0 <= selection < len(items_list):
                current_index = selection
                show_editor(items_list[selection], selection)

        def show_editor(item_data, index):
            """Показывает редактор для выбранного элемента"""
            nonlocal current_editor

            clear_editor()

            current_editor = self._create_object_form(
                edit_frame, item_data, items_schema, subfield_configs
            )

        def clear_editor():
            """Очищает редактор"""
            nonlocal current_editor
            for widget in edit_frame.winfo_children():
                widget.destroy()
            current_editor = None

        def add_item():
            """Добавляет новый элемент"""
            _save_current_item()
            new_item = self._create_default_object(items_schema, subfield_configs)
            items_list.append(new_item)

            update_dropdown()
            dropdown.current(len(items_list) - 1)
            on_selection_change()
            self.on_change()

        def delete_item():
            """Удаляет выбранный элемент"""
            nonlocal current_index
            if current_index is not None and 0 <= current_index < len(items_list):
                items_list.pop(current_index)
                clear_editor()
                update_dropdown()
                self.on_change()

        def copy_item_to_clipboard():
            """Копирует текущий элемент в буфер обмена как JSON"""
            import json as _json
            idx = current_index if current_index is not None else dropdown.current()
            if idx is None or not (0 <= idx < len(items_list)):
                return
            data_to_copy = None
            try:
                if current_editor is not None and idx == current_index:
                    if isinstance(current_editor, dict):
                        if not callable(current_editor.get("get_value")):
                            tmp = {}
                            for fname, winfo in current_editor.items():
                                try:
                                    tmp[fname] = winfo["get_value"]()
                                except Exception:
                                    tmp[fname] = None
                            data_to_copy = tmp
                        else:
                            data_to_copy = current_editor["get_value"]()
                    else:
                        data_to_copy = current_editor.get_value()
                else:
                    data_to_copy = items_list[idx]
            except Exception:
                data_to_copy = items_list[idx]
            try:
                text = _json.dumps(data_to_copy, ensure_ascii=False, indent=2)
                root = main_frame.winfo_toplevel()
                root.clipboard_clear()
                root.clipboard_append(text)
            except Exception:
                pass

        def get_form_data():
            """Получает данные из формы"""
            _save_current_item()
            return items_list
        
        def set_form_data(data):
            """Устанавливает данные в форму"""
            nonlocal items_list
            items_list = data if isinstance(data, list) else []
            update_dropdown()
        
        # Привязываем события
        dropdown.bind("<<ComboboxSelected>>", on_selection_change)
        add_btn.config(command=add_item)
        delete_btn.config(command=delete_item)
        copy_btn.config(command=copy_item_to_clipboard)
        
        # Инициализация
        update_dropdown()
        
        main_frame.pack(fill="both", expand=True)
        
        # Возвращаем как widget именно родительский контейнер, чтобы он корректно упаковывался выше
        return {
            "widget": parent_frame,
            "get_value": get_form_data,
            "set_value": set_form_data
        }

    def _create_dropdown_selector(self, field_info: Dict[str, Any], value: Any) -> Dict[str, Any]:
        """Создает выпадающий список для выбора элемента массива"""
        import tkinter as tk
        from tkinter import ttk
        
        config = field_info.get("config", {})
        items_list = value if isinstance(value, list) else []
        
        # Поддержка составного отображения
        display_field = config.get("display_field", "title")
        display_fields = config.get("display_fields", [])
        display_separator = config.get("display_separator", " - ")
        
        def get_item_display_text(item, index):
            """Получает текст для отображения элемента"""
            if display_fields:
                # Составное отображение из нескольких полей
                parts = []
                for field in display_fields:
                    value = item.get(field, "")
                    if value:
                        parts.append(str(value))
                if parts:
                    return display_separator.join(parts)
                else:
                    return f"Элемент {index + 1}"
            else:
                # Простое отображение одного поля
                return item.get(display_field, f"Элемент {index + 1}")
        
        # Основной фрейм
        main_frame = ttk.Frame(self.parent)
        
        # Фрейм для выпадающего списка и кнопок
        selector_frame = ttk.Frame(main_frame)
        selector_frame.pack(fill="x", pady=(0, 5))
        
        # Выпадающий список
        selected_var = tk.StringVar()
        dropdown = ttk.Combobox(selector_frame, textvariable=selected_var, state="readonly")
        dropdown.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        # Кнопки
        button_frame = ttk.Frame(selector_frame)
        button_frame.pack(side="right")
        
        edit_btn = ttk.Button(button_frame, text="✎", width=3, state="disabled")
        edit_btn.pack(side="left", padx=(0, 2))
        
        add_btn = ttk.Button(button_frame, text="➕", width=3)
        add_btn.pack(side="left", padx=(0, 2))
        
        # Кнопка копирования текущего элемента в буфер обмена (для shots)
        copy_btn = ttk.Button(button_frame, text="📋", width=3, state="disabled")
        copy_btn.pack(side="left", padx=(0, 2))
        
        delete_btn = ttk.Button(button_frame, text="🗑️", width=3, state="disabled")
        delete_btn.pack(side="left")
        
        # Фрейм для редактирования выбранного элемента
        hide_edit_label = config.get("hide_edit_label", False)
        edit_title = "" if hide_edit_label else "Редактирование элемента"
        edit_frame = ttk.LabelFrame(main_frame, text=edit_title)
        edit_frame.pack(fill="both", expand=True, pady=(5, 0))
        
        current_editor = None
        current_index = None
        
        def update_dropdown():
            """Обновляет список в dropdown"""
            dropdown['values'] = [
                f"{i+1}. {get_item_display_text(item, i)}"
                for i, item in enumerate(items_list)
            ]
            
            if items_list:
                edit_btn.config(state="normal")
                delete_btn.config(state="normal")
                copy_btn.config(state="normal")
                if selected_var.get() == "":
                    dropdown.current(0)
                    on_selection_change()
            else:
                edit_btn.config(state="disabled")
                delete_btn.config(state="disabled")
                copy_btn.config(state="disabled")
                clear_editor()
        
        def _save_current_item():
            WidgetFactory.save_editor_to_items_list(
                current_editor, current_index, items_list
            )

        def on_selection_change(event=None):
            """Обработчик изменения выбора в dropdown"""
            nonlocal current_editor, current_index

            _save_current_item()

            selection = dropdown.current()
            if 0 <= selection < len(items_list):
                current_index = selection
                show_editor(items_list[selection], selection)

        def show_editor(item_data, index):
            """Показывает редактор для выбранного элемента"""
            nonlocal current_editor

            clear_editor()

            schema = field_info.get("schema", {})
            items_schema = schema.get("items", {})

            current_editor = self._create_object_form(
                edit_frame, item_data, items_schema, config
            )

        def clear_editor():
            """Очищает редактор"""
            nonlocal current_editor
            for widget in edit_frame.winfo_children():
                widget.destroy()
            current_editor = None

        def add_item():
            """Добавляет новый элемент"""
            _save_current_item()
            schema = field_info.get("schema", {})
            items_schema = schema.get("items", {})

            new_item = self._create_default_object(items_schema, config)
            items_list.append(new_item)

            update_dropdown()
            dropdown.current(len(items_list) - 1)
            on_selection_change()
            self.on_change()

        def delete_item():
            """Удаляет выбранный элемент"""
            if current_index is not None and 0 <= current_index < len(items_list):
                items_list.pop(current_index)
                clear_editor()
                update_dropdown()
                self.on_change()

        def get_data():
            """Возвращает текущие данные"""
            _save_current_item()
            return items_list

        def copy_item_to_clipboard():
            """Копирует текущий элемент dropdown в буфер обмена как JSON"""
            import json as _json
            try:
                idx = dropdown.current()
            except Exception:
                idx = -1
            if not (0 <= idx < len(items_list)):
                return
            # Если текущий редактор открыт на этом элементе — собираем актуальные данные
            data_to_copy = None
            try:
                if current_editor is not None and idx == current_index:
                    tmp = {}
                    for fname, winfo in current_editor.items():
                        try:
                            tmp[fname] = winfo["get_value"]()
                        except Exception:
                            tmp[fname] = None
                    data_to_copy = tmp
                else:
                    data_to_copy = items_list[idx]
            except Exception:
                data_to_copy = items_list[idx]
            try:
                text = _json.dumps(data_to_copy, ensure_ascii=False, indent=2)
                root = main_frame.winfo_toplevel()
                root.clipboard_clear()
                root.clipboard_append(text)
            except Exception:
                pass
        
        # Привязываем события
        dropdown.bind('<<ComboboxSelected>>', on_selection_change)
        add_btn.config(command=add_item)
        delete_btn.config(command=delete_item)
        edit_btn.config(command=lambda: on_selection_change())
        copy_btn.config(command=copy_item_to_clipboard)
        
        # Инициализация
        update_dropdown()
        
        return {
            "widget": main_frame,
            "get_value": get_data,
            "set": lambda new_value: None,  # TODO: реализовать set
            "validate": lambda: True
        }
    
    # Удалены специализированные редакторы страниц и кадров —
    # все сценарии покрывает универсальный редактор массивов
    
    def _determine_simple_widget(self, schema: Dict[str, Any]) -> str:
        """Простое определение виджета для вложенных полей"""
        field_type = schema.get("type")
        if field_type == "string":
            if "enum" in schema:
                return "combobox"
            elif schema.get("maxLength", 0) > 100:
                return "text_area"
            else:
                return "entry"
        elif field_type in ["integer", "number"]:
            return "entry"
        elif field_type == "boolean":
            return "checkbox"
        elif field_type == "array":
            return "list_editor"
        else:
            return "entry"
    
    def _validate_string(self, value: str, field_info: Dict[str, Any]) -> bool:
        """Валидация строкового поля"""
        schema = field_info.get("schema", {})
        
        if schema.get("minLength") and len(value) < schema["minLength"]:
            return False
        if schema.get("maxLength") and len(value) > schema["maxLength"]:
            return False
        
        return True
    
    def _validate_enum(self, value: str, field_info: Dict[str, Any]) -> bool:
        """Валидация enum поля"""
        schema = field_info.get("schema", {})
        enum_values = schema.get("enum", [])
        
        if enum_values and value not in enum_values:
            return False
        
        return True
    
    def _validate_number(self, value: Union[int, float], field_info: Dict[str, Any]) -> bool:
        """Валидация числового поля"""
        schema = field_info.get("schema", {})
        
        try:
            num_value = float(value)
            if schema.get("minimum") is not None and num_value < schema["minimum"]:
                return False
            if schema.get("maximum") is not None and num_value > schema["maximum"]:
                return False
            return True
        except (ValueError, TypeError):
            return False


class UniversalFormGenerator:
    """Генератор динамических форм из JSON Schema"""
    
    def __init__(self, data: Dict[str, Any], schema_type: str = None, on_change_callback: Callable = None, introspector: SchemaIntrospector = None):
        self.data = data or {}
        self.schema_type = schema_type
        self.on_change = on_change_callback or (lambda: None)
        
        self.introspector = introspector or SchemaIntrospector()
        self.widgets = {}  # Хранилище виджетов для получения значений
        
        # БАЗОВЫЙ РЕЖИМ: Данные + UI config (гибридная схема)
        # Schema из SCHEMA_MAPPING используется только как fallback для сложных случаев
        
        # ВСЕГДА используем гибридную схему как основной режим
        ui_config = self.introspector.ui_config
        hybrid_schema = generate_hybrid_schema(ui_config, self.data, schema_type or "auto")
        self.schema_info = self.introspector.analyze_schema(schema_type or "auto", hybrid_schema)
    
    def create_form(self, parent_frame: tk.Widget) -> None:
        """Создает форму в указанном родительском фрейме"""
        # Очищаем родительский фрейм
        for widget in parent_frame.winfo_children():
            widget.destroy()
        
        self.widgets.clear()
        
        # Создаем группы полей
        for group_name, group_info in self.schema_info["groups"].items():
            self._create_group(parent_frame, group_name, group_info)
    
    def _create_group(self, parent: tk.Widget, group_name: str, group_info: Dict[str, Any]) -> None:
        """Создает группу полей"""
        # Определяем заголовок группы (скрываем если hide_label=True)
        hide_label = group_info.get("hide_label", False)
        group_title = "" if hide_label else group_info.get("title", "")
        
        # Контейнер группы
        if group_info.get("collapsible", False):
            # Создаем сворачиваемую группу
            collapsed = group_info.get("collapsed",
                        group_info.get("collapsed_by_default", False))
            group_frame = create_collapsible_frame(parent, group_title, collapsed)
            # Для сворачиваемых групп НЕ вызываем pack() - это уже сделано в create_collapsible_frame
        else:
            group_frame = ttk.LabelFrame(parent, text=group_title, padding=10)
            group_frame.pack(fill="x", pady=5)
        
        layout = group_info.get("layout", "vertical")
        columns = group_info.get("columns", 1)
        
        # ВСЕГДА создаем виджеты
        if layout == "grid" and columns > 1:
            self._create_grid_layout(group_frame, group_info, columns)
        else:
            self._create_vertical_layout(group_frame, group_info)
        
        # Для НЕ сворачиваемых групп - скрываем если collapsed=True
        if not group_info.get("collapsible", False) and group_info.get("collapsed", False):
            group_frame.pack_forget()
    
    def _create_vertical_layout(self, parent: tk.Widget, group_info: Dict[str, Any]) -> None:
        """Создает вертикальную компоновку полей"""
        for field_name in group_info["fields"]:
            if field_name not in self.schema_info["fields"]:
                continue
            
            field_info = self.schema_info["fields"][field_name]
            self._create_field(parent, field_name, field_info, layout="vertical")
    
    def _create_grid_layout(self, parent: tk.Widget, group_info: Dict[str, Any], columns: int) -> None:
        """Создает сеточную компоновку полей"""
        fields = [f for f in group_info["fields"] if f in self.schema_info["fields"]]
        
        for i, field_name in enumerate(fields):
            field_info = self.schema_info["fields"][field_name]
            
            row = i // columns
            col = i % columns
            
            self._create_field(parent, field_name, field_info, layout="grid", grid_pos=(row, col))
    
    def _create_field(self, parent: tk.Widget, field_name: str, field_info: Dict[str, Any], 
                     layout: str = "vertical", grid_pos: tuple = None) -> None:
        """Создает отдельное поле"""
        
        # Контейнер для поля
        if layout == "vertical":
            field_frame = ttk.Frame(parent)
            field_frame.pack(fill="x", pady=2)
            
            # Метка (показываем только если не скрыта)
            hide_label = field_info.get("config", {}).get("hide_label", False)
            if not hide_label:
                label = ttk.Label(field_frame, text=f"{field_info['title']}:")
                label.pack(anchor="w")
            
            # Фабрика виджетов
            widget_factory = WidgetFactory(field_frame, self.on_change, self.introspector)
            
        elif layout == "grid":
            # Метка (показываем только если не скрыта)
            hide_label = field_info.get("config", {}).get("hide_label", False)
            if not hide_label:
                label = ttk.Label(parent, text=f"{field_info['title']}:")
                label.grid(row=grid_pos[0], column=grid_pos[1]*2, sticky="w", padx=(0, 5), pady=2)
            
            # Фабрика виджетов
            widget_factory = WidgetFactory(parent, self.on_change, self.introspector)
        
        # Создаем виджет
        # Безопасное получение значения для любого типа данных
        if isinstance(self.data, dict):
            current_value = self.data.get(field_name)
        elif isinstance(self.data, list) and field_name == "items":
            # Для массивов поле "items" содержит сам массив
            current_value = self.data
        else:
            # Для других случаев (список, но поле не "items") - None
            current_value = None
            
        widget_info = widget_factory.create_widget(field_info, current_value)
        
        # Размещаем виджет
        if layout == "vertical":
            widget_info["widget"].pack(fill="x", pady=(2, 5))
        elif layout == "grid":
            widget_info["widget"].grid(row=grid_pos[0], column=grid_pos[1]*2+1, sticky="ew", pady=2)
            parent.grid_columnconfigure(grid_pos[1]*2+1, weight=1)
        
        # Сохраняем виджет для получения значений
        self.widgets[field_name] = widget_info
    
    def get_form_data(self) -> Any:
        """Получает данные из формы"""
        # Если исходные данные были массивом, возвращаем массив
        if isinstance(self.data, list):
            # Для массива возвращаем значение поля "items" (если есть)
            if "items" in self.widgets:
                try:
                    return self.widgets["items"]["get_value"]()
                except Exception as e:
                    logger.error(f"Ошибка получения массива items: {e}")
                    return self.data  # Возвращаем исходные данные
            else:
                return self.data  # Если нет виджета items, возвращаем исходные данные
        
        # Для объектов работаем как раньше
        result = {}
        
        for field_name, widget_info in self.widgets.items():
            try:
                value = widget_info["get_value"]()
                
                # Обработка пустых значений
                if value == "" or value is None:
                    # Проверяем, является ли поле обязательным
                    if field_name in self.schema_info["validation"]["required"]:
                        result[field_name] = value  # Оставляем для валидации
                    # Для необязательных полей пропускаем пустые значения
                else:
                    result[field_name] = value
                    
            except Exception as e:
                logger.error(f"Ошибка получения значения поля {field_name}: {e}")
                result[field_name] = None
        
        return result
    
    def set_form_data(self, data: Any) -> None:
        """Устанавливает данные в форму"""
        self.data = data or {}
        
        # Для массивов - устанавливаем данные в виджет items
        if isinstance(data, list):
            if "items" in self.widgets:
                try:
                    self.widgets["items"]["set_value"](data)
                except Exception as e:
                    logger.error(f"Ошибка установки массива items: {e}")
            return
        
        # Для объектов - обычная логика
        if not isinstance(data, dict):
            return
            
        for field_name, widget_info in self.widgets.items():
            try:
                value = data.get(field_name)
                widget_info["set_value"](value)
            except Exception as e:
                logger.error(f"Ошибка установки значения поля {field_name}: {e}")
    
    def validate_form(self) -> List[str]:
        """Валидирует данные формы"""
        errors = []
        form_data = self.get_form_data()
        
        # Для массивов валидация другая - проверяем что массив не пустой
        if isinstance(form_data, list):
            if not form_data:
                errors.append("Массив не должен быть пустым")
            # Для массивов больше нечего валидировать на уровне формы
            return errors
        
        # Для объектов - обычная валидация полей
        if not isinstance(form_data, dict):
            errors.append("Некорректный формат данных")
            return errors
            
        # Проверка обязательных полей
        required_fields = self.schema_info["validation"]["required"]
        for field_name in required_fields:
            value = form_data.get(field_name)
            # Поле считается пустым если:
            # - отсутствует (None)
            # - строка пустая или только пробелы
            # - НЕ массив (пустой массив [] валиден)
            is_empty = (
                value is None or 
                (isinstance(value, str) and not value.strip()) or
                (not isinstance(value, (list, dict)) and not value)
            )
            if is_empty:
                field_title = self.schema_info["fields"].get(field_name, {}).get("title", field_name)
                errors.append(f"Поле '{field_title}' обязательно для заполнения")
        
        # Валидация отдельных полей
        for field_name, widget_info in self.widgets.items():
            try:
                if not widget_info["validate"]():
                    field_title = self.schema_info["fields"].get(field_name, {}).get("title", field_name)
                    errors.append(f"Поле '{field_title}' содержит некорректные данные")
            except Exception as e:
                logger.error(f"Ошибка валидации поля {field_name}: {e}")
                errors.append(f"Ошибка валидации поля '{field_name}'")
        
        return errors


# Пример использования
if __name__ == "__main__":
    # Тестирование системы
    import tkinter as tk
    from tkinter import ttk
    
    root = tk.Tk()
    root.title("Тест универсального редактора")
    root.geometry("800x600")
    
    # Тестовые данные для brief
    test_data = {
        "title": "Тестовая сказка",
        "genre": "сказка",
        "target_age": "3-5 лет",
        "language": "ru",
        "description": "Это тестовое описание сказки",
        "main_characters": ["Колобок", "Лиса", "Медведь"],
        "pages_min": 5,
        "pages_max": 10
    }
    
    def on_change():
        print("Данные изменились")
    
    # Создаем генератор формы
    form_generator = UniversalFormGenerator(test_data, "brief", on_change)
    
    # Создаем скроллируемый фрейм
    canvas = tk.Canvas(root)
    scrollbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
    scrollable_frame = ttk.Frame(canvas)
    
    scrollable_frame.bind(
        "<Configure>",
        lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
    )
    
    canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    
    # Создаем форму
    form_generator.create_form(scrollable_frame)
    
    # Кнопки управления
    button_frame = ttk.Frame(root)
    button_frame.pack(side="bottom", fill="x", padx=10, pady=5)
    
    def get_data():
        data = form_generator.get_form_data()
        print("Данные формы:", json.dumps(data, indent=2, ensure_ascii=False))
    
    def validate_data():
        errors = form_generator.validate_form()
        if errors:
            print("Ошибки валидации:", errors)
        else:
            print("Валидация прошла успешно")
    
    def reload_ui_config():
        """Перечитывает UI конфигурацию и пересоздает форму"""
        global form_generator
        try:
            # Сохраняем текущие данные
            current_data = {}
            try:
                current_data = form_generator.get_form_data()
            except Exception:
                current_data = test_data
            
            # Пересоздаем форму с новой конфигурацией (без перезагрузки модуля)
            form_generator = UniversalFormGenerator(current_data, "brief", on_change)
            form_generator.create_form(scrollable_frame)
            
            print("✅ UI конфигурация перечитана и форма обновлена")
            
        except Exception as e:
            print(f"❌ Ошибка перечитывания UI конфигурации: {e}")
    
    ttk.Button(button_frame, text="Получить данные", command=get_data).pack(side="left", padx=5)
    ttk.Button(button_frame, text="Валидировать", command=validate_data).pack(side="left", padx=5)
    ttk.Button(button_frame, text="🔄 Перечитать UI конфиг", command=reload_ui_config).pack(side="left", padx=5)
    
    root.mainloop()