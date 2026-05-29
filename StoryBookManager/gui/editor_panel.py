"""
Панель редактирования JSON файлов
===============================

Структурированный редактор для JSON файлов проекта с валидацией.
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import json
from typing import Optional, Dict, Any, Callable
import logging

from StoryBookManager.core.project_manager import Project, ProjectManager
from StoryBookManager.core.file_manager import FileManager
from StoryBookManager.utils.json_validator import json_validator
from StoryBookManager.gui.universal_json_editor import UniversalFormGenerator, SchemaIntrospector
from StoryBookManager.utils.scroll_utils import (
    bind_mousewheel_to_canvas_frame_ultimate,
    bind_mousewheel_to_text_with_scrollbar,
    bind_mousewheel_to_widget
)

logger = logging.getLogger(__name__)


class EditorPanel(ttk.Frame):
    """Панель редактирования JSON файлов"""
    
    def __init__(self, parent, on_file_changed: Callable, project_manager: ProjectManager):
        super().__init__(parent)
        
        self.on_file_changed = on_file_changed
        self.project_manager = project_manager
        self.current_project: Optional[Project] = None
        self.file_manager: Optional[FileManager] = None
        self.current_file_type: Optional[str] = None
        self.current_data: Optional[Dict[str, Any]] = None
        self.has_changes = False
        
        # Универсальный редактор (теперь базовый режим)
        self._ui_config_error: Optional[str] = None
        try:
            self.introspector = SchemaIntrospector()
        except (FileNotFoundError, ValueError) as e:
            logger.error(f"Не удалось загрузить UI конфигурацию: {e}")
            self.introspector = None
            self._ui_config_error = str(e)
        self.universal_form_generator: Optional[UniversalFormGenerator] = None
        self.use_universal_editor = True  # Всегда включен
        
        self.create_ui()
    
    def create_ui(self):
        """Создание пользовательского интерфейса"""
        # Заголовок
        header_frame = ttk.Frame(self)
        header_frame.pack(fill="x", padx=10, pady=(10, 5))
        
        ttk.Label(header_frame, text="Редактор JSON файлов", style="Title.TLabel").pack(side="left")
        
        # Кнопки управления
        button_frame = ttk.Frame(header_frame)
        button_frame.pack(side="right")
        
        ttk.Button(button_frame, text="💾 Сохранить", command=self.save_current_file).pack(side="left", padx=2)
        ttk.Button(button_frame, text="🔄 Обновить", command=self.reload_current_file).pack(side="left", padx=2)
        ttk.Button(button_frame, text="✓ Валидация", command=self.validate_current_file).pack(side="left", padx=2)
        ttk.Button(button_frame, text="⚙️ UI конфиг", command=self.reload_ui_config).pack(side="left", padx=2)
        
        # Универсальный редактор теперь всегда включен
        ttk.Separator(button_frame, orient="vertical").pack(side="left", padx=5, pady=2, fill="y")
        
        # Индикатор режима
        mode_label = ttk.Label(button_frame, text="🚀 Гибридный режим", foreground="green")
        mode_label.pack(side="left", padx=5)
        
        # Выбор файла
        file_frame = ttk.Frame(self)
        file_frame.pack(fill="x", padx=10, pady=5)
        
        ttk.Label(file_frame, text="Файл:").pack(side="left")
        self.file_var = tk.StringVar()
        self.file_var.trace_add('write', self.on_file_selected)
        self.file_combo = ttk.Combobox(file_frame, textvariable=self.file_var, width=30, state="readonly")
        self.file_combo.pack(side="left", padx=5)        
        # Статус файла
        self.status_label = ttk.Label(file_frame, text="Файл не выбран")
        self.status_label.pack(side="right")
        
        # Разделитель
        ttk.Separator(self, orient="horizontal").pack(fill="x", pady=5)
        
        # Основная область редактирования
        main_frame = ttk.Frame(self)
        main_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        # Notebook для разных режимов редактирования
        self.editor_notebook = ttk.Notebook(main_frame)
        self.editor_notebook.pack(fill="both", expand=True)
        
        # Структурированный редактор
        self.structured_frame = ttk.Frame(self.editor_notebook)
        self.editor_notebook.add(self.structured_frame, text="📝 Структурированный")
        
        # Raw JSON редактор
        self.raw_frame = ttk.Frame(self.editor_notebook)
        self.editor_notebook.add(self.raw_frame, text="🔧 Raw JSON")
        
        # Создание содержимого редакторов
        self.create_structured_editor()
        self.create_raw_editor()
        
        # Панель валидации
        self.create_validation_panel(main_frame)
    
    def create_structured_editor(self):
        """Создание структурированного редактора"""
        # Scrollable frame для форм
        canvas = tk.Canvas(self.structured_frame)
        scrollbar = ttk.Scrollbar(self.structured_frame, orient="vertical", command=canvas.yview)
        self.form_frame = ttk.Frame(canvas)
        
        self.form_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=self.form_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # Обновляем ширину canvas при изменении размера
        def configure_canvas(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            # Устанавливаем ширину формы равной ширине canvas
            canvas_width = event.width
            canvas.itemconfig(canvas.find_all()[0], width=canvas_width)
        
        canvas.bind('<Configure>', configure_canvas)
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # Добавляем поддержку прокрутки колесом мыши для структурированного редактора (окончательная версия)
        bind_mousewheel_to_canvas_frame_ultimate(canvas, self.form_frame)
        
        # Изначально пустая форма
        self.empty_label = ttk.Label(self.form_frame, text="Выберите файл для редактирования")
        self.empty_label.pack(pady=50)
    
    def create_raw_editor(self):
        """Создание редактора сырого JSON"""
        # Текстовое поле с подсветкой синтаксиса
        self.raw_text = scrolledtext.ScrolledText(
            self.raw_frame, 
            wrap=tk.NONE,
            font=("Courier", 10),
            tabs="4c"
        )
        self.raw_text.pack(fill="both", expand=True)
        
        # Простая подсветка JSON синтаксиса
        self.setup_json_highlighting()
        
        # Создание контекстного меню для копирования/вставки
        self.create_context_menu()
        
        # Обработчик изменений
        self.raw_text.bind("<KeyRelease>", self.on_raw_text_changed)
        # Привязка правой кнопки мыши к контекстному меню
        self.raw_text.bind("<Button-3>", self.show_context_menu)
    
    def setup_json_highlighting(self):
        """Настройка подсветки JSON синтаксиса"""
        # Определяем теги для раскраски
        self.raw_text.tag_configure("string", foreground="#008000")  # Зеленый
        self.raw_text.tag_configure("number", foreground="#0000FF")  # Синий
        self.raw_text.tag_configure("keyword", foreground="#800080") # Пурпурный
        self.raw_text.tag_configure("bracket", foreground="#FF0000") # Красный
    
    def create_context_menu(self):
        """Создание контекстного меню для raw редактора"""
        self.context_menu = tk.Menu(self.raw_text, tearoff=0)
        self.context_menu.add_command(label="Вырезать", command=self.cut_text, accelerator="Ctrl+X")
        self.context_menu.add_command(label="Копировать", command=self.copy_text, accelerator="Ctrl+C")
        self.context_menu.add_command(label="Вставить", command=self.paste_text, accelerator="Ctrl+V")
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Выделить все", command=self.select_all_text, accelerator="Ctrl+A")
        
        # Привязка горячих клавиш (независимо от раскладки через keycode)
        # Правильный синтаксис для keycode в Tkinter
        self.raw_text.bind("<Control-Key>", self.handle_control_keycode)
        
        # Также попробуем универсальный обработчик для отладки
        self.raw_text.bind("<Control-KeyPress>", self.handle_control_key)
        
        # Дополнительная отладка - показать все нажатия клавиш
        self.raw_text.bind("<KeyPress>", self.debug_keypress)
    
    def show_context_menu(self, event):
        """Показать контекстное меню"""
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()
    
    def cut_text(self):
        """Вырезать текст"""
        try:
            if self.raw_text.tag_ranges(tk.SEL):
                self.copy_text()
                self.raw_text.delete(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            pass
    
    def copy_text(self):
        """Копировать текст"""
        try:
            if self.raw_text.tag_ranges(tk.SEL):
                text = self.raw_text.get(tk.SEL_FIRST, tk.SEL_LAST)
                self.raw_text.clipboard_clear()
                self.raw_text.clipboard_append(text)
        except tk.TclError:
            pass
    
    def paste_text(self):
        """Вставить текст"""
        try:
            if self.raw_text.tag_ranges(tk.SEL):
                self.raw_text.delete(tk.SEL_FIRST, tk.SEL_LAST)
            clipboard_text = self.raw_text.clipboard_get()
            self.raw_text.insert(tk.INSERT, clipboard_text)
        except tk.TclError:
            pass
    
    def select_all_text(self):
        """Выделить весь текст"""
        self.raw_text.tag_add(tk.SEL, "1.0", tk.END)
        self.raw_text.mark_set(tk.INSERT, "1.0")
        self.raw_text.see(tk.INSERT)
    
    def handle_cut(self, event):
        """Обработчик горячей клавиши Ctrl+X"""
        self.cut_text()
        return "break"
    
    def handle_copy(self, event):
        """Обработчик горячей клавиши Ctrl+C"""
        self.copy_text()
        return "break"
    
    def handle_paste(self, event):
        """Обработчик горячей клавиши Ctrl+V"""
        self.paste_text()
        return "break"
    
    def handle_select_all(self, event):
        """Обработчик горячей клавиши Ctrl+A"""
        self.select_all_text()
        return "break"
    
    def handle_control_key(self, event):
        """Универсальный обработчик Ctrl+ комбинаций (независимо от раскладки)"""
        print(f"🔥 DEBUG: Ctrl+{event.keysym} нажато (код: {event.keycode})")  # Отладка
        
        # Определяем по keysym, а не по символу (работает на любой раскладке)
        if event.keysym.lower() == 'c':
            print("🔥 DEBUG: Выполняется копирование")
            self.copy_text()
            return "break"
        elif event.keysym.lower() == 'v':
            print("🔥 DEBUG: Выполняется вставка")
            self.paste_text()
            return "break"
        elif event.keysym.lower() == 'x':
            print("🔥 DEBUG: Выполняется вырезание")
            self.cut_text()
            return "break"
        elif event.keysym.lower() == 'a':
            print("🔥 DEBUG: Выполняется выделение всего")
            self.select_all_text()
            return "break"
        
        # Для других комбинаций не перехватываем
        return None
    
    def handle_control_keycode(self, event):
        """Обработчик Ctrl+ комбинаций по keycode (независимо от раскладки)"""
        print(f"🎯 DEBUG keycode: {event.keycode}, keysym: {event.keysym}, state: {event.state}")
        
        # Определяем по физическому коду клавиши (не зависит от раскладки)
        if event.keycode == 67:  # C
            print("🔥 DEBUG: Копирование через keycode")
            self.copy_text()
            return "break"
        elif event.keycode == 86:  # V
            print("🔥 DEBUG: Вставка через keycode")
            self.paste_text()
            return "break"
        elif event.keycode == 88:  # X
            print("🔥 DEBUG: Вырезание через keycode")
            self.cut_text()
            return "break"
        elif event.keycode == 65:  # A
            print("🔥 DEBUG: Выделение через keycode")
            self.select_all_text()
            return "break"
        
        return None
    
    def debug_keypress(self, event):
        """Отладочный метод для всех нажатий клавиш"""
        if event.state & 0x4:  # Ctrl нажат
            print(f"🔍 DEBUG: Клавиша {event.keysym} (код: {event.keycode}, состояние: {event.state})")
        return None  # Не блокируем обработку
    
    def create_validation_panel(self, parent):
        """Создание панели валидации"""
        validation_frame = ttk.LabelFrame(parent, text="Валидация", padding=5)
        validation_frame.pack(fill="x", pady=(10, 0))
        
        # Результаты валидации
        self.validation_text = tk.Text(
            validation_frame, 
            height=2,
            wrap=tk.WORD,
            font=("Courier", 9)
        )
        validation_scrollbar = ttk.Scrollbar(validation_frame, command=self.validation_text.yview)
        self.validation_text.configure(yscrollcommand=validation_scrollbar.set)
        
        self.validation_text.pack(side="left", fill="both", expand=True)
        validation_scrollbar.pack(side="right", fill="y")
        
        # Добавляем поддержку прокрутки колесом мыши для области валидации
        bind_mousewheel_to_text_with_scrollbar(self.validation_text, validation_scrollbar)
        
        # Теги для раскраски сообщений
        self.validation_text.tag_configure("error", foreground="#FF0000")
        self.validation_text.tag_configure("warning", foreground="#FF8000")
        self.validation_text.tag_configure("success", foreground="#008000")
        self.validation_text.tag_configure("info", foreground="#0000FF")
    
    def load_project(self, project: Project):
        """Загрузка проекта"""
        try:
            self.current_project = project
            self.file_manager = FileManager(project.project_id)
            
            # Сброс состояния
            self.current_file_type = None
            self.current_data = None
            self.has_changes = False
            
            # Очистка редакторов
            self.clear_editors()
            
            # Обновление списка файлов
            self.update_file_list()
            
            logger.info(f"Проект {project.project_id} загружен в редактор")
            
        except Exception as e:
            logger.error(f"Ошибка загрузки проекта в редактор: {e}")
            messagebox.showerror("Ошибка", f"Не удалось загрузить проект:\n{e}")
            
    def update_file_list(self):
        """Обновление списка доступных файлов в комбобоксе"""
        if not self.current_project or not self.file_manager:
            return
            
        import os
        project_files = self.project_manager.get_project_files(self.current_project.project_id)
        
        file_names = {
            "brief": "Техническое задание",
            "synopsis": "Синопсис",
            "beats": "Сюжетные точки",
            "story": "Текст сказки",
            "characters": "Персонажи",
            "locations": "Локации",
            "consistency_rules": "Правила согласованности",
            "style_text": "Стиль текста",
            "style_images": "Стиль изображений",
            "negative_prompt_list": "Негативные промпты",
            "screenplay": "Сценарий",
            "shots": "Видеокадры",
            "pdf": "PDF книга",
            "markdown": "Markdown книга"
        }
        
        combo_values = []
        for key, path_str in project_files.items():
            if os.path.exists(path_str):
                name = file_names.get(key, key)
                combo_values.append(f"{key} - {name}")
                
        self.file_combo['values'] = combo_values
        
        # Если выбранный файл больше не в списке, сбрасываем выбор
        current_selection = self.file_var.get()
        if current_selection and current_selection not in combo_values:
            self.file_var.set('')
            
    def clear_editors(self):
        """Очистка редакторов"""
        # Очистка структурированного редактора
        for widget in self.form_frame.winfo_children():
            widget.destroy()
        self.empty_label = ttk.Label(self.form_frame, text="Выберите файл для редактирования")
        self.empty_label.pack(pady=50)
        
        # Очистка raw редактора
        self.raw_text.delete("1.0", tk.END)
        
        # Очистка валидации
        self.validation_text.delete("1.0", tk.END)
        
        # Сброс статуса
        self.status_label.config(text="Файл не выбран")
    
    def on_file_selected(self, *args):
        """Обработчик выбора файла"""
        selection = self.file_var.get()
        if not selection or not self.file_manager:
            return
        
        # Извлекаем тип файла
        file_type = selection.split(" - ")[0]
        
        if file_type != self.current_file_type:
            # Проверяем несохраненные изменения
            if self.has_changes:
                result = messagebox.askyesnocancel(
                    "Несохраненные изменения",
                    "У вас есть несохраненные изменения. Сохранить?"
                )
                if result is True:
                    self.save_current_file()
                elif result is None:  # Cancel
                    return
            
            self.load_file(file_type)
    
    def load_file(self, file_type: str):
        """Загрузка файла"""
        if self._ui_config_error:
            messagebox.showerror(
                "Ошибка конфигурации UI",
                f"{self._ui_config_error}\n\n"
                "Структурированный редактор недоступен."
            )
            return

        try:
            self.current_file_type = file_type
            self.current_data = self.file_manager.load_json_file(file_type)
            self.has_changes = False
            
            if self.current_data is None:
                self.status_label.config(text="Файл не найден")
                self.clear_editors()
                return
            
            # Обновляем редакторы
            self.update_structured_editor()
            self.update_raw_editor()
            
            # Валидация
            self.validate_current_file()
            
            self.status_label.config(text="Файл загружен")
            logger.info(f"Загружен файл {file_type}")
            
        except Exception as e:
            logger.error(f"Ошибка загрузки файла {file_type}: {e}")
            messagebox.showerror("Ошибка", f"Не удалось загрузить файл:\n{e}")
    
    def update_structured_editor(self):
        """Обновление структурированного редактора"""
        # Очищаем текущую форму
        for widget in self.form_frame.winfo_children():
            widget.destroy()
        
        if not self.current_data:
            return
        
        # БАЗОВЫЙ РЕЖИМ: Всегда используем гибридный редактор
        try:
            logger.info(f"🚀 Создаем гибридную форму для {self.current_file_type or 'unknown'}")
            self._create_hybrid_form()
            return
                
        except Exception as e:
            logger.error(f"Ошибка обновления редактора: {e}")
            # В случае критической ошибки, показываем сообщение
            error_label = ttk.Label(
                self.form_frame, 
                text=f"Ошибка загрузки редактора: {e}",
                foreground="red"
            )
            error_label.pack(pady=20)
    
    def _create_hybrid_form(self):
        """Создание гибридной формы (БАЗОВЫЙ РЕЖИМ)"""
        try:
            # Проверяем, нужно ли извлечь массив из обертки
            form_data = self.current_data
            
            # Специальная обработка для файлов-массивов (shots, beats, characters, etc.)
            array_types = ["shots", "beats", "characters", "locations"]
            if (self.current_file_type in array_types and 
                isinstance(self.current_data, dict) and 
                "items" in self.current_data and 
                isinstance(self.current_data["items"], list)):
                
                # Используем массив напрямую для генерации формы
                form_data = self.current_data["items"]
                logger.info(f"🎯 Извлекаем массив items для {self.current_file_type}: {len(form_data)} элементов")
            
            # Создаем генератор формы в гибридном режиме
            self.universal_form_generator = UniversalFormGenerator(
                data=form_data,
                schema_type=self.current_file_type,  # Может быть None - это нормально
                on_change_callback=self.on_structured_data_changed,
                introspector=self.introspector
            )
            
            # Создаем форму
            self.universal_form_generator.create_form(self.form_frame)
            
            logger.info(f"✅ Создана гибридная форма для {self.current_file_type or 'auto'}")
            
        except Exception as e:
            logger.error(f"Ошибка создания гибридной формы: {e}")
            raise
    
    def _create_legacy_form(self):
        """Создание legacy формы (старый подход)"""
        # Создаем форму в зависимости от типа файла (старый код)
        if self.current_file_type == "brief":
            self.create_brief_form()
        elif self.current_file_type == "story":
            self.create_story_form()
        elif self.current_file_type == "characters":
            self.create_characters_form()
        elif self.current_file_type == "locations":
            self.create_locations_form()
        elif self.current_file_type == "shots":
            self.create_shots_form()
        elif self.current_file_type == "screenplay":
            self.create_screenplay_form()
        else:
            # Общая форма для остальных файлов
            self.create_generic_form()
        
        # Сброс универсального генератора
        self.universal_form_generator = None
    
    # Методы переключения удалены - теперь всегда используется гибридный режим
    
    def create_brief_form(self):
        """Создание формы для brief.json"""
        data = self.current_data
        
        # Основная информация
        main_frame = ttk.LabelFrame(self.form_frame, text="Основная информация", padding=10)
        main_frame.pack(fill="x", pady=5)
        
        # Название
        ttk.Label(main_frame, text="Название:").grid(row=0, column=0, sticky="w", pady=2)
        self.brief_title = tk.StringVar(value=data.get("title", ""))
        title_entry = ttk.Entry(main_frame, textvariable=self.brief_title, width=50)
        title_entry.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=2)
        
        # Жанр
        ttk.Label(main_frame, text="Жанр:").grid(row=1, column=0, sticky="w", pady=2)
        self.brief_genre = tk.StringVar(value=data.get("genre", ""))
        genre_values = self.get_unique_values_from_project("genre", ["сказка", "рассказ", "притча", "басня"])
        genre_combo = ttk.Combobox(main_frame, textvariable=self.brief_genre, width=20)
        genre_combo['values'] = genre_values
        genre_combo.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=2)
        
        # Возраст
        ttk.Label(main_frame, text="Возраст:").grid(row=2, column=0, sticky="w", pady=2)
        self.brief_age = tk.StringVar(value=data.get("target_age", ""))
        age_entry = ttk.Entry(main_frame, textvariable=self.brief_age, width=20)
        age_entry.grid(row=2, column=1, sticky="w", padx=(10, 0), pady=2)
        
        main_frame.grid_columnconfigure(1, weight=1)
        
        # Описание
        desc_frame = ttk.LabelFrame(self.form_frame, text="Описание", padding=10)
        desc_frame.pack(fill="both", expand=True, pady=5)
        
        self.brief_description = tk.Text(desc_frame, height=4, wrap=tk.WORD)
        self.brief_description.insert("1.0", data.get("description", ""))
        self.brief_description.pack(fill="both", expand=True)
        
        # Персонажи
        chars_frame = ttk.LabelFrame(self.form_frame, text="Главные персонажи", padding=10)
        chars_frame.pack(fill="x", pady=5)
        
        self.brief_characters = []
        characters = data.get("main_characters", [])
        for i, char in enumerate(characters):
            char_var = tk.StringVar(value=char)
            self.brief_characters.append(char_var)
            ttk.Entry(chars_frame, textvariable=char_var, width=30).pack(pady=2)
        
        # Кнопка для добавления персонажа
        ttk.Button(chars_frame, text="+ Добавить персонажа", 
                  command=self.add_character_field).pack(pady=5)
        
        # Привязываем обработчики изменений
        for var in [self.brief_title, self.brief_genre, self.brief_age]:
            var.trace_add('write', self.on_structured_data_changed)
        self.brief_description.bind("<KeyRelease>", self.on_structured_data_changed)
    
    def create_story_form(self):
        """Создание формы для story.json"""
        data = self.current_data
        
        # Страницы
        pages_frame = ttk.LabelFrame(self.form_frame, text="", padding=0)
        pages_frame.pack(fill="both", expand=True, pady=0)
        
        # Notebook для страниц
        self.pages_notebook = ttk.Notebook(pages_frame)
        self.pages_notebook.pack(fill="both", expand=True)
        
        # Создаем вкладки для каждой страницы
        pages = data.get("pages", [])
        self.page_widgets = []
        
        for page in pages:
            self.add_page_tab(page)
        
        # Кнопка добавления страницы
        ttk.Button(pages_frame, text="+ Добавить страницу", 
                  command=self.add_page).pack(pady=5)
    
    def add_page_tab(self, page_data: Dict[str, Any]):
        """Добавление вкладки страницы"""
        page_num = page_data.get("page", len(self.page_widgets) + 1)
        
        # Создаем фрейм для страницы
        page_frame = ttk.Frame(self.pages_notebook)
        self.pages_notebook.add(page_frame, text=f"Страница {page_num}")
        
        # Заголовок страницы
        ttk.Label(page_frame, text="Заголовок страницы:").pack(anchor="w", pady=(10, 2))
        page_title_var = tk.StringVar(value=page_data.get("title", ""))
        page_title_var.trace_add('write', self.on_structured_data_changed)
        ttk.Entry(page_frame, textvariable=page_title_var, width=50).pack(fill="x", padx=10)
        
        # Текст страницы
        ttk.Label(page_frame, text="Текст страницы:").pack(anchor="w", pady=(10, 2))
        page_text = tk.Text(page_frame, wrap=tk.WORD)
        page_text.insert("1.0", page_data.get("body", ""))
        page_text.bind("<KeyRelease>", self.on_structured_data_changed)
        page_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        
        # Счетчик слов
        word_count_label = ttk.Label(page_frame, text="Слов: 0")
        word_count_label.pack(anchor="w", padx=10)
        
        def update_word_count(event=None):
            text = page_text.get("1.0", tk.END)
            word_count = len(text.split())
            word_count_label.config(text=f"Слов: {word_count}")
        
        page_text.bind("<KeyRelease>", lambda e: [update_word_count(e), self.on_structured_data_changed(e)])
        update_word_count()
        
        # Сохраняем ссылки на виджеты
        self.page_widgets.append({
            "frame": page_frame,
            "title_var": page_title_var,
            "text_widget": page_text,
            "word_count_label": word_count_label
        })
    
    def create_characters_form(self):
        """Создание формы для characters.json"""
        # Упрощенная форма - список персонажей
        if not isinstance(self.current_data, list):
            self.current_data = []
        
        # Сохраняем ссылки на переменные для синхронизации
        self.character_vars = {}
        
        for i, char in enumerate(self.current_data):
            char_frame = ttk.LabelFrame(self.form_frame, text=f"Персонаж: {char.get('name', 'Без имени')}", padding=10)
            char_frame.pack(fill="x", pady=5)
            
            # Инициализируем словарь для этого персонажа
            self.character_vars[i] = {}
            
            # Основные поля
            ttk.Label(char_frame, text="Имя:").grid(row=0, column=0, sticky="w")
            name_var = tk.StringVar(value=char.get("name", ""))
            name_var.trace_add('write', self.on_structured_data_changed)
            self.character_vars[i]['name'] = name_var
            ttk.Entry(char_frame, textvariable=name_var, width=30).grid(row=0, column=1, sticky="ew", padx=5)
            
            ttk.Label(char_frame, text="Возраст:").grid(row=1, column=0, sticky="w")
            age_var = tk.StringVar(value=char.get("age", ""))
            age_var.trace_add('write', self.on_structured_data_changed)
            self.character_vars[i]['age'] = age_var
            age_values = self.get_unique_values_from_characters("age", ["ребёнок", "подросток", "взрослый", "пожилой", "взрослая", "пожилая"])
            age_combo = ttk.Combobox(char_frame, textvariable=age_var, width=25)
            age_combo['values'] = age_values
            age_combo.grid(row=1, column=1, sticky="ew", padx=5)
            
            # Роль персонажа
            ttk.Label(char_frame, text="Роль:").grid(row=2, column=0, sticky="w")
            role_var = tk.StringVar(value=char.get("role", ""))
            role_var.trace_add('write', self.on_structured_data_changed)
            self.character_vars[i]['role'] = role_var
            role_values = self.get_unique_values_from_characters("role", ["главный герой", "главная героиня", "вспомогательный персонаж", "антагонист", "второстепенный персонаж"])
            role_combo = ttk.Combobox(char_frame, textvariable=role_var, width=25)
            role_combo['values'] = role_values
            role_combo.grid(row=2, column=1, sticky="ew", padx=5)
            
            # Immutable Attributes
            immutable_frame = ttk.LabelFrame(char_frame, text="Неизменные атрибуты", padding=5)
            immutable_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=5)
            
            immutable_attrs = char.get("immutable_attributes", {})
            self.character_vars[i]['immutable_attributes'] = {}
            
            # Face shape
            ttk.Label(immutable_frame, text="Форма лица:").grid(row=0, column=0, sticky="w")
            face_shape_var = tk.StringVar(value=immutable_attrs.get("face_shape", ""))
            face_shape_var.trace_add('write', self.on_structured_data_changed)
            self.character_vars[i]['immutable_attributes']['face_shape'] = face_shape_var
            ttk.Entry(immutable_frame, textvariable=face_shape_var, width=20).grid(row=0, column=1, sticky="ew", padx=5)
            
            # Eye color
            ttk.Label(immutable_frame, text="Цвет глаз:").grid(row=0, column=2, sticky="w", padx=(10,0))
            eye_color_var = tk.StringVar(value=immutable_attrs.get("eye_color", ""))
            eye_color_var.trace_add('write', self.on_structured_data_changed)
            self.character_vars[i]['immutable_attributes']['eye_color'] = eye_color_var
            ttk.Entry(immutable_frame, textvariable=eye_color_var, width=20).grid(row=0, column=3, sticky="ew", padx=5)
            
            # Skin tone
            ttk.Label(immutable_frame, text="Тон кожи:").grid(row=1, column=0, sticky="w")
            skin_tone_var = tk.StringVar(value=immutable_attrs.get("skin_tone", ""))
            skin_tone_var.trace_add('write', self.on_structured_data_changed)
            self.character_vars[i]['immutable_attributes']['skin_tone'] = skin_tone_var
            ttk.Entry(immutable_frame, textvariable=skin_tone_var, width=20).grid(row=1, column=1, sticky="ew", padx=5)
            
            # Body proportions
            ttk.Label(immutable_frame, text="Телосложение:").grid(row=1, column=2, sticky="w", padx=(10,0))
            body_prop_var = tk.StringVar(value=immutable_attrs.get("body_proportions", ""))
            body_prop_var.trace_add('write', self.on_structured_data_changed)
            self.character_vars[i]['immutable_attributes']['body_proportions'] = body_prop_var
            ttk.Entry(immutable_frame, textvariable=body_prop_var, width=20).grid(row=1, column=3, sticky="ew", padx=5)
            
            # Unique features
            ttk.Label(immutable_frame, text="Особенности:").grid(row=2, column=0, sticky="nw")
            unique_features_text = tk.Text(immutable_frame, height=3, width=40)
            unique_features_text.insert("1.0", "\n".join(immutable_attrs.get("unique_features", [])))
            unique_features_text.bind("<KeyRelease>", self.on_structured_data_changed)
            self.character_vars[i]['immutable_attributes']['unique_features'] = unique_features_text
            unique_features_text.grid(row=2, column=1, columnspan=3, sticky="ew", padx=5)
            
            immutable_frame.grid_columnconfigure(1, weight=1)
            immutable_frame.grid_columnconfigure(3, weight=1)
            
            # Variable Attributes
            variable_frame = ttk.LabelFrame(char_frame, text="Изменяемые атрибуты", padding=5)
            variable_frame.grid(row=4, column=0, columnspan=2, sticky="ew", pady=5)
            
            variable_attrs = char.get("variable_attributes", {})
            self.character_vars[i]['variable_attributes'] = {}
            
            # Base clothing
            ttk.Label(variable_frame, text="Одежда:").grid(row=0, column=0, sticky="w")
            base_clothing_var = tk.StringVar(value=variable_attrs.get("base_clothing", ""))
            base_clothing_var.trace_add('write', self.on_structured_data_changed)
            self.character_vars[i]['variable_attributes']['base_clothing'] = base_clothing_var
            ttk.Entry(variable_frame, textvariable=base_clothing_var, width=25).grid(row=0, column=1, sticky="ew", padx=5)
            
            # Base hairstyle
            ttk.Label(variable_frame, text="Прическа:").grid(row=0, column=2, sticky="w", padx=(10,0))
            base_hairstyle_var = tk.StringVar(value=variable_attrs.get("base_hairstyle", ""))
            base_hairstyle_var.trace_add('write', self.on_structured_data_changed)
            self.character_vars[i]['variable_attributes']['base_hairstyle'] = base_hairstyle_var
            ttk.Entry(variable_frame, textvariable=base_hairstyle_var, width=25).grid(row=0, column=3, sticky="ew", padx=5)
            
            # Accessories
            ttk.Label(variable_frame, text="Аксессуары:").grid(row=1, column=0, sticky="nw")
            accessories_text = tk.Text(variable_frame, height=2, width=30)
            accessories_text.insert("1.0", "\n".join(variable_attrs.get("accessories", [])))
            accessories_text.bind("<KeyRelease>", self.on_structured_data_changed)
            self.character_vars[i]['variable_attributes']['accessories'] = accessories_text
            accessories_text.grid(row=1, column=1, columnspan=3, sticky="ew", padx=5)
            
            variable_frame.grid_columnconfigure(1, weight=1)
            variable_frame.grid_columnconfigure(3, weight=1)
            
            # Дополнительные поля
            extra_frame = ttk.LabelFrame(char_frame, text="Дополнительная информация", padding=5)
            extra_frame.grid(row=5, column=0, columnspan=2, sticky="ew", pady=5)
            
            # Reference image path
            ttk.Label(extra_frame, text="Путь к изображению:").grid(row=0, column=0, sticky="w")
            ref_image_var = tk.StringVar(value=char.get("reference_image_path", ""))
            ref_image_var.trace_add('write', self.on_structured_data_changed)
            self.character_vars[i]['reference_image_path'] = ref_image_var
            ttk.Entry(extra_frame, textvariable=ref_image_var, width=50).grid(row=0, column=1, sticky="ew", padx=5)
            
            # Gesture set
            ttk.Label(extra_frame, text="Жесты:").grid(row=1, column=0, sticky="nw")
            gesture_text = tk.Text(extra_frame, height=3, width=40)
            gesture_text.insert("1.0", "\n".join(char.get("gesture_set", [])))
            gesture_text.bind("<KeyRelease>", self.on_structured_data_changed)
            self.character_vars[i]['gesture_set'] = gesture_text
            gesture_text.grid(row=1, column=1, sticky="ew", padx=5)
            
            # Speech patterns
            ttk.Label(extra_frame, text="Речевые паттерны:").grid(row=2, column=0, sticky="nw")
            speech_text = tk.Text(extra_frame, height=3, width=40)
            speech_text.insert("1.0", "\n".join(char.get("speech_patterns", [])))
            speech_text.bind("<KeyRelease>", self.on_structured_data_changed)
            self.character_vars[i]['speech_patterns'] = speech_text
            speech_text.grid(row=2, column=1, sticky="ew", padx=5)
            
            # No-go rules
            ttk.Label(extra_frame, text="Запреты:").grid(row=3, column=0, sticky="nw")
            nogo_text = tk.Text(extra_frame, height=3, width=40)
            nogo_text.insert("1.0", "\n".join(char.get("no_go_rules", [])))
            nogo_text.bind("<KeyRelease>", self.on_structured_data_changed)
            self.character_vars[i]['no_go_rules'] = nogo_text
            nogo_text.grid(row=3, column=1, sticky="ew", padx=5)
            
            extra_frame.grid_columnconfigure(1, weight=1)
            
            char_frame.grid_columnconfigure(1, weight=1)
    
    def create_locations_form(self):
        """Создание формы для locations.json"""
        # Полноценная форма для локаций
        if not isinstance(self.current_data, list):
            self.current_data = []
        
        # Сохраняем ссылки на переменные для синхронизации
        self.location_vars = {}
        
        for i, loc in enumerate(self.current_data):
            loc_frame = ttk.LabelFrame(self.form_frame, text=f"Локация: {loc.get('name', 'Без названия')}", padding=10)
            loc_frame.pack(fill="x", pady=5)
            
            # Инициализируем словарь для этой локации
            self.location_vars[i] = {}
            
            # Название
            ttk.Label(loc_frame, text="Название:").grid(row=0, column=0, sticky="w")
            name_var = tk.StringVar(value=loc.get("name", ""))
            name_var.trace_add('write', self.on_structured_data_changed)
            self.location_vars[i]['name'] = name_var
            ttk.Entry(loc_frame, textvariable=name_var, width=40).grid(row=0, column=1, sticky="ew", padx=5)
            
            # Описание
            ttk.Label(loc_frame, text="Описание:").grid(row=1, column=0, sticky="nw")
            desc_text = tk.Text(loc_frame, height=3, width=40)
            desc_text.insert("1.0", loc.get("description", ""))
            desc_text.bind("<KeyRelease>", self.on_structured_data_changed)
            self.location_vars[i]['description'] = desc_text
            desc_text.grid(row=1, column=1, sticky="ew", padx=5)
            
            # Ключевые объекты
            ttk.Label(loc_frame, text="Ключевые объекты:").grid(row=2, column=0, sticky="nw")
            key_objects_text = tk.Text(loc_frame, height=3, width=40)
            key_objects_text.insert("1.0", "\n".join(loc.get("key_objects", [])))
            key_objects_text.bind("<KeyRelease>", self.on_structured_data_changed)
            self.location_vars[i]['key_objects'] = key_objects_text
            key_objects_text.grid(row=2, column=1, sticky="ew", padx=5)
            
            # Атмосфера
            ttk.Label(loc_frame, text="Атмосфера:").grid(row=3, column=0, sticky="w")
            atmosphere_var = tk.StringVar(value=loc.get("atmosphere", ""))
            atmosphere_var.trace_add('write', self.on_structured_data_changed)
            self.location_vars[i]['atmosphere'] = atmosphere_var
            ttk.Entry(loc_frame, textvariable=atmosphere_var, width=40).grid(row=3, column=1, sticky="ew", padx=5)
            
            # Освещение
            ttk.Label(loc_frame, text="Освещение:").grid(row=4, column=0, sticky="w")
            lighting_var = tk.StringVar(value=loc.get("lighting", ""))
            lighting_var.trace_add('write', self.on_structured_data_changed)
            self.location_vars[i]['lighting'] = lighting_var
            lighting_values = self.get_unique_values_from_locations("lighting", ["дневной свет", "вечерний свет", "утренний свет", "закат", "рассвет", "лунный свет", "искусственное освещение"])
            lighting_combo = ttk.Combobox(loc_frame, textvariable=lighting_var, width=37)
            lighting_combo['values'] = lighting_values
            lighting_combo.grid(row=4, column=1, sticky="ew", padx=5)
            
            # Цветовая палитра
            ttk.Label(loc_frame, text="Цветовая палитра:").grid(row=5, column=0, sticky="nw")
            color_palette_text = tk.Text(loc_frame, height=2, width=40)
            color_palette_text.insert("1.0", "\n".join(loc.get("color_palette", [])))
            color_palette_text.bind("<KeyRelease>", self.on_structured_data_changed)
            self.location_vars[i]['color_palette'] = color_palette_text
            color_palette_text.grid(row=5, column=1, sticky="ew", padx=5)
            
            # Путь к изображению
            ttk.Label(loc_frame, text="Путь к изображению:").grid(row=6, column=0, sticky="w")
            ref_image_var = tk.StringVar(value=loc.get("reference_image_path", ""))
            ref_image_var.trace_add('write', self.on_structured_data_changed)
            self.location_vars[i]['reference_image_path'] = ref_image_var
            ttk.Entry(loc_frame, textvariable=ref_image_var, width=40).grid(row=6, column=1, sticky="ew", padx=5)
            
            loc_frame.grid_columnconfigure(1, weight=1)
    
    def create_shots_form(self):
        """Создание формы для shots.json"""
        data = self.current_data
        
        # Заголовок
        header_frame = ttk.Frame(self.form_frame)
        header_frame.pack(fill="x", pady=(0, 10))
        
        ttk.Label(header_frame, text="🎬 Редактор видеокадров", style="Heading.TLabel").pack(side="left")
        
        # Кнопки управления
        btn_frame = ttk.Frame(header_frame)
        btn_frame.pack(side="right")
        
        ttk.Button(btn_frame, text="➕ Добавить кадр", 
                  command=self.add_shot).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="🔄 Обновить", 
                  command=self.on_structured_data_changed).pack(side="left", padx=5)
        
        # Информация о количестве кадров
        shots_count = len(data.get("items", []))
        ttk.Label(self.form_frame, text=f"Всего кадров: {shots_count}").pack(anchor="w", pady=5)
        
        # Основной фрейм со списком и редактором
        main_shots_frame = ttk.Frame(self.form_frame)
        main_shots_frame.pack(fill="both", expand=True, pady=10)
        
        # Левая панель - список кадров (фиксированная ширина)
        left_frame = ttk.Frame(main_shots_frame, width=280)
        left_frame.pack(side="left", fill="y", padx=(0, 10))
        left_frame.pack_propagate(False)  # Сохраняем фиксированную ширину
        
        ttk.Label(left_frame, text="Список кадров:", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(0, 5))
        
        # Listbox для кадров
        list_frame = ttk.Frame(left_frame)
        list_frame.pack(fill="both", expand=True)
        
        self.shots_listbox = tk.Listbox(list_frame, width=30)
        shots_scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.shots_listbox.yview)
        self.shots_listbox.configure(yscrollcommand=shots_scrollbar.set)
        
        self.shots_listbox.pack(side="left", fill="y")
        shots_scrollbar.pack(side="right", fill="y")
        
        # Добавляем поддержку прокрутки колесом мыши для списка кадров
        bind_mousewheel_to_widget(self.shots_listbox)
        
        # Заполняем список кадров
        items = data.get("items", [])
        for i, shot in enumerate(items):
            page_num = shot.get("page_number", "?")
            scene_num = shot.get("scene_number", "?")
            shot_num = shot.get("shot_number", "?")
            shot_type = shot.get("shot_type", "?")
            list_text = f"Стр.{page_num} Сц.{scene_num} Кад.{shot_num} ({shot_type})"
            self.shots_listbox.insert(tk.END, list_text)
        
        # Привязываем выбор кадра
        self.shots_listbox.bind('<<ListboxSelect>>', self.on_shot_select)
        
        # Кнопки управления списком
        buttons_frame = ttk.Frame(left_frame)
        buttons_frame.pack(fill="x", pady=(10, 0))
        
        ttk.Button(buttons_frame, text="➕ Добавить", command=self.add_shot, width=12).pack(fill="x", pady=2)
        ttk.Button(buttons_frame, text="🗑 Удалить", command=self.delete_shot, width=12).pack(fill="x", pady=2)
        ttk.Button(buttons_frame, text="⬆ Вверх", command=self.move_shot_up, width=12).pack(fill="x", pady=2)
        ttk.Button(buttons_frame, text="⬇ Вниз", command=self.move_shot_down, width=12).pack(fill="x", pady=2)
        
        # Правая панель - редактор кадра
        self.right_frame = ttk.Frame(main_shots_frame)
        self.right_frame.pack(side="right", fill="both", expand=True)
        
        # Инициализируем данные кадров
        self.shots_data = items
        self.current_shot_index = 0
        
        # Выбираем первый кадр
        if items:
            self.shots_listbox.selection_set(0)
            self.create_shot_editor()
        
    def on_shot_select(self, event):
        """Обработчик выбора кадра из списка"""
        selection = self.shots_listbox.curselection()
        if selection:
            self.current_shot_index = selection[0]
            self.create_shot_editor()
    
    def create_shot_editor(self):
        """Создание редактора для выбранного кадра"""
        # Очищаем правую панель
        for widget in self.right_frame.winfo_children():
            widget.destroy()
        
        if not self.shots_data or self.current_shot_index >= len(self.shots_data):
            ttk.Label(self.right_frame, text="Выберите кадр для редактирования").pack(pady=50)
            return
        
        shot_data = self.shots_data[self.current_shot_index]
        index = self.current_shot_index
        
        # Заголовок редактора
        header_frame = ttk.Frame(self.right_frame)
        header_frame.pack(fill="x", pady=(0, 10))
        
        page_num = shot_data.get("page_number", "?")
        scene_num = shot_data.get("scene_number", "?")
        shot_num = shot_data.get("shot_number", "?")
        shot_type = shot_data.get("shot_type", "?")
        title = f"Кадр: Стр.{page_num} Сц.{scene_num} Кад.{shot_num} ({shot_type})"
        
        ttk.Label(header_frame, text=title, font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        
        # Scrollable содержимое
        canvas = tk.Canvas(self.right_frame, height=600)  # Принудительно задаем минимальную высоту
        scrollbar = ttk.Scrollbar(self.right_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # Обновляем ширину при изменении размера canvas
        def configure_shot_canvas(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            # Устанавливаем ширину формы равной ширине canvas
            canvas_width = event.width
            if canvas.find_all():
                canvas.itemconfig(canvas.find_all()[0], width=canvas_width)
        
        canvas.bind('<Configure>', configure_shot_canvas)
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # Добавляем поддержку прокрутки колесом мыши для редактора кадра (окончательная версия)
        bind_mousewheel_to_canvas_frame_ultimate(canvas, scrollable_frame)
                
        # Тип кадра и план камеры
        camera_frame = ttk.LabelFrame(scrollable_frame, text="Камера и кадрирование", padding=10)
        camera_frame.pack(fill="x", pady=5, padx=10)
        
        # Тип кадра
        ttk.Label(camera_frame, text="Тип кадра:").grid(row=0, column=0, sticky="w", pady=2)
        shot_type_var = tk.StringVar(value=shot_data.get("shot_type", "start"))
        shot_type_combo = ttk.Combobox(camera_frame, textvariable=shot_type_var, 
                                      values=["start", "end"], state="readonly", width=15)
        shot_type_combo.grid(row=0, column=1, sticky="ew", padx=5)
        shot_type_var.trace_add('write', self.on_structured_data_changed)
        setattr(self, f"shot_{index}_shot_type", shot_type_var)
        
        # План камеры
        ttk.Label(camera_frame, text="План камеры:").grid(row=1, column=0, sticky="w", pady=2)
        camera_plan_var = tk.StringVar(value=shot_data.get("camera_plan", "Общий план"))
        
        # Динамически получаем все уникальные значения camera_plan из данных
        camera_plan_values = self.get_unique_values_from_shots("camera_plan")
        
        camera_plan_combo = ttk.Combobox(camera_frame, textvariable=camera_plan_var, width=25,
                                        values=camera_plan_values)
        camera_plan_combo.grid(row=1, column=1, sticky="ew", padx=5)
        camera_plan_var.trace_add('write', self.on_structured_data_changed)
        setattr(self, f"shot_{index}_camera_plan", camera_plan_var)
        
        # Тайминг
        ttk.Label(camera_frame, text="Тайминг:").grid(row=2, column=0, sticky="w", pady=2)
        timing_var = tk.StringVar(value=shot_data.get("timing", "00:00-00:03"))
        timing_entry = ttk.Entry(camera_frame, textvariable=timing_var, width=25)
        timing_entry.grid(row=2, column=1, sticky="ew", padx=5)
        timing_var.trace_add('write', self.on_structured_data_changed)
        setattr(self, f"shot_{index}_timing", timing_var)
        
        camera_frame.grid_columnconfigure(1, weight=1)
        
        # Промпты
        prompts_frame = ttk.LabelFrame(scrollable_frame, text="Промпты", padding=10)
        prompts_frame.pack(fill="x", pady=5, padx=10)
        
        # English prompt
        ttk.Label(prompts_frame, text="English prompt:").pack(anchor="w")
        english_prompt_text = tk.Text(prompts_frame, height=4, wrap=tk.WORD)
        english_prompt_text.pack(fill="x", pady=(2, 10))
        english_prompt_text.insert("1.0", shot_data.get("english_prompt", ""))
        english_prompt_text.bind('<KeyRelease>', self.on_structured_data_changed)
        setattr(self, f"shot_{index}_english_prompt", english_prompt_text)
        
        # Video prompt
        ttk.Label(prompts_frame, text="Video prompt:").pack(anchor="w")
        video_prompt_text = tk.Text(prompts_frame, height=2, wrap=tk.WORD)
        video_prompt_text.pack(fill="x", pady=(2, 10))
        video_prompt_text.insert("1.0", shot_data.get("video_prompt", ""))
        video_prompt_text.bind('<KeyRelease>', self.on_structured_data_changed)
        setattr(self, f"shot_{index}_video_prompt", video_prompt_text)
        
        # Negative prompt
        ttk.Label(prompts_frame, text="Negative prompt:").pack(anchor="w")
        negative_prompt_text = tk.Text(prompts_frame, height=2, wrap=tk.WORD)
        negative_prompt_text.pack(fill="x", pady=(2, 10))
        negative_prompt_text.insert("1.0", shot_data.get("negative_prompt", ""))
        negative_prompt_text.bind('<KeyRelease>', self.on_structured_data_changed)
        setattr(self, f"shot_{index}_negative_prompt", negative_prompt_text)
    
    def add_shot(self):
        """Добавление нового кадра"""
        if not hasattr(self, 'current_data') or self.current_data is None:
            return
        
        # Создаем новый кадр с базовыми параметрами
        new_shot = {
            "project_id": "ryaba",
            "page_number": 1,
            "scene_number": 1,
            "shot_number": 1,
            "shot_type": "start",
            "camera_plan": "Общий план",
            "timing": "00:00-00:03",
            "english_prompt": "",
            "video_prompt": "",
            "negative_prompt": "",
            "width": 1024,
            "height": 1024,
            "true_cfg_scale": 7.5,
            "num_inference_steps": 30
        }
        
        # Добавляем к данным
        if "items" not in self.current_data:
            self.current_data["items"] = []
        
        self.current_data["items"].append(new_shot)
        self.shots_data.append(new_shot)
        
        # Обновляем список кадров
        self.refresh_shots_list()
        
        # Выбираем новый кадр
        new_index = len(self.shots_data) - 1
        self.shots_listbox.selection_clear(0, tk.END)
        self.shots_listbox.selection_set(new_index)
        self.current_shot_index = new_index
        self.create_shot_editor()
        
        self.on_structured_data_changed()
    
    def delete_shot(self):
        """Удаление выбранного кадра"""
        if not self.shots_data or self.current_shot_index >= len(self.shots_data):
            return
        
        # Подтверждение удаления
        result = messagebox.askyesno("Подтверждение", 
                                   f"Удалить кадр {self.current_shot_index + 1}?")
        if result:
            # Удаляем из данных
            del self.shots_data[self.current_shot_index]
            del self.current_data["items"][self.current_shot_index]
            
            # Обновляем список
            self.refresh_shots_list()
            
            # Выбираем соседний кадр
            if self.shots_data:
                if self.current_shot_index >= len(self.shots_data):
                    self.current_shot_index = len(self.shots_data) - 1
                self.shots_listbox.selection_set(self.current_shot_index)
                self.create_shot_editor()
            else:
                self.current_shot_index = 0
                # Очищаем редактор
                for widget in self.right_frame.winfo_children():
                    widget.destroy()
                ttk.Label(self.right_frame, text="Нет кадров для редактирования").pack(pady=50)
            
            self.on_structured_data_changed()
    
    def move_shot_up(self):
        """Перемещение кадра вверх"""
        if self.current_shot_index > 0:
            # Меняем местами в данных
            items = self.current_data["items"]
            items[self.current_shot_index], items[self.current_shot_index - 1] = \
                items[self.current_shot_index - 1], items[self.current_shot_index]
            
            self.shots_data[self.current_shot_index], self.shots_data[self.current_shot_index - 1] = \
                self.shots_data[self.current_shot_index - 1], self.shots_data[self.current_shot_index]
            
            # Обновляем индекс и интерфейс
            self.current_shot_index -= 1
            self.refresh_shots_list()
            self.shots_listbox.selection_set(self.current_shot_index)
            self.create_shot_editor()
            self.on_structured_data_changed()
    
    def move_shot_down(self):
        """Перемещение кадра вниз"""
        if self.current_shot_index < len(self.shots_data) - 1:
            # Меняем местами в данных
            items = self.current_data["items"]
            items[self.current_shot_index], items[self.current_shot_index + 1] = \
                items[self.current_shot_index + 1], items[self.current_shot_index]
            
            self.shots_data[self.current_shot_index], self.shots_data[self.current_shot_index + 1] = \
                self.shots_data[self.current_shot_index + 1], self.shots_data[self.current_shot_index]
            
            # Обновляем индекс и интерфейс
            self.current_shot_index += 1
            self.refresh_shots_list()
            self.shots_listbox.selection_set(self.current_shot_index)
            self.create_shot_editor()
            self.on_structured_data_changed()
    
    def refresh_shots_list(self):
        """Обновление списка кадров"""
        self.shots_listbox.delete(0, tk.END)
        for i, shot in enumerate(self.shots_data):
            page_num = shot.get("page_number", "?")
            scene_num = shot.get("scene_number", "?")
            shot_num = shot.get("shot_number", "?")
            shot_type = shot.get("shot_type", "?")
            list_text = f"Стр.{page_num} Сц.{scene_num} Кад.{shot_num} ({shot_type})"
            self.shots_listbox.insert(tk.END, list_text)
    
    def get_unique_values_from_shots(self, field_name: str) -> list:
        """Получение уникальных значений поля из всех кадров"""
        if not hasattr(self, 'shots_data') or not self.shots_data:
            # Базовые значения по умолчанию
            defaults = {
                "camera_plan": ["Общий план", "Средний план", "Крупный план", "Экстрим крупный план", "POV"],
                "shot_type": ["start", "end"],
                "width": [512, 768, 1024, 1280, 1536],
                "height": [512, 768, 1024, 1280, 1536]
            }
            return defaults.get(field_name, [])
        
        # Собираем все уникальные значения из данных
        unique_values = set()
        for shot in self.shots_data:
            value = shot.get(field_name)
            if value is not None and value != "":
                unique_values.add(str(value))
        
        # Сортируем для удобства
        sorted_values = sorted(list(unique_values))
        
        # Добавляем базовые значения, если их нет
        defaults = {
            "camera_plan": ["Общий план", "Средний план", "Крупный план"],
            "shot_type": ["start", "end"],
            "width": ["512", "768", "1024", "1280", "1536"],
            "height": ["512", "768", "1024", "1280", "1536"]
        }
        
        if field_name in defaults:
            for default_val in defaults[field_name]:
                if default_val not in sorted_values:
                    sorted_values.append(default_val)
            
            # Пересортируем после добавления базовых значений
            if field_name in ["width", "height"]:
                # Для числовых значений сортируем как числа
                sorted_values = sorted(sorted_values, key=lambda x: int(x) if x.isdigit() else 0)
            else:
                sorted_values.sort()
        
        return sorted_values
    
    def get_unique_values_from_project(self, field_name: str, defaults: list = None) -> list:
        """Получение уникальных значений поля из всех файлов проекта"""
        if defaults is None:
            defaults = []
        
        unique_values = set(defaults)
        
        # Проверяем текущие данные
        if self.current_data and isinstance(self.current_data, dict):
            value = self.current_data.get(field_name)
            if value and value != "":
                unique_values.add(str(value))
        
        # Если у нас есть доступ к проекту, можем проверить другие файлы
        if hasattr(self, 'current_project') and self.current_project:
            try:
                # Проверяем brief
                brief_path = self.current_project.project_path / "00_brief.json"
                if brief_path.exists():
                    with open(brief_path, 'r', encoding='utf-8') as f:
                        brief_data = json.load(f)
                        value = brief_data.get(field_name)
                        if value and value != "":
                            unique_values.add(str(value))
                
                # Проверяем другие JSON файлы при необходимости
                # TODO: Можно расширить для других типов файлов
                
            except Exception as e:
                logger.warning(f"Ошибка при получении значений {field_name}: {e}")
        
        return sorted(list(unique_values))
    
    def get_unique_values_from_characters(self, field_name: str, defaults: list = None) -> list:
        """Получение уникальных значений поля из всех персонажей"""
        if defaults is None:
            defaults = []
        
        unique_values = set(defaults)
        
        # Проверяем текущие данные персонажей
        if self.current_data and isinstance(self.current_data, list):
            for char in self.current_data:
                if isinstance(char, dict):
                    value = char.get(field_name)
                    if value and value != "":
                        unique_values.add(str(value))
        
        # Проверяем файлы персонажей в проекте
        if hasattr(self, 'current_project') and self.current_project:
            try:
                # Проверяем characters.json
                chars_path = self.current_project.project_path / "20_bible" / "characters.json"
                if chars_path.exists():
                    with open(chars_path, 'r', encoding='utf-8') as f:
                        chars_data = json.load(f)
                        if isinstance(chars_data, list):
                            for char in chars_data:
                                if isinstance(char, dict):
                                    value = char.get(field_name)
                                    if value and value != "":
                                        unique_values.add(str(value))
                
            except Exception as e:
                logger.warning(f"Ошибка при получении значений персонажей {field_name}: {e}")
        
        return sorted(list(unique_values))
    
    def get_unique_values_from_locations(self, field_name: str, defaults: list = None) -> list:
        """Получить уникальные значения для поля из данных локаций"""
        unique_values = set()
        
        # Проверяем текущие данные локаций
        if hasattr(self, 'current_data') and isinstance(self.current_data, list):
            for loc in self.current_data:
                if isinstance(loc, dict):
                    value = loc.get(field_name)
                    if value and value != "":
                        unique_values.add(str(value))
        
        # Проверяем файлы локаций в проекте
        if hasattr(self, 'current_project') and self.current_project:
            try:
                # Проверяем locations.json
                locations_path = self.current_project.project_path / "20_bible" / "locations.json"
                if locations_path.exists():
                    with open(locations_path, 'r', encoding='utf-8') as f:
                        locations_data = json.load(f)
                        if isinstance(locations_data, list):
                            for loc in locations_data:
                                if isinstance(loc, dict):
                                    value = loc.get(field_name)
                                    if value and value != "":
                                        unique_values.add(str(value))
            except Exception as e:
                logger.warning(f"Ошибка чтения файла локаций: {e}")
        
        # Возвращаем отсортированный список с базовыми значениями в начале
        result = (defaults or []) + sorted(list(unique_values))
        return result
    
    def create_screenplay_form(self):
        """Создание формы для screenplay.json"""
        data = self.current_data
        self.screenplay_vars = {}
        self.screenplay_data = data.get("screenplay", [])
        self.current_scene_index = 0
        
        # === БАЗОВАЯ ИНФОРМАЦИЯ СЦЕНАРИЯ (ВЕРХНЯЯ ЧАСТЬ) ===
        
        # Концепция
        concept_frame = ttk.LabelFrame(self.form_frame, text="Концепция", padding=10)
        concept_frame.pack(fill="x", pady=5)
        concept_data = data.get("concept", {})
        
        # Название и аудитория в одной строке
        ttk.Label(concept_frame, text="Название:").grid(row=0, column=0, sticky="w", pady=2)
        title_var = tk.StringVar(value=concept_data.get("title", ""))
        title_var.trace_add('write', self.on_structured_data_changed)
        self.screenplay_vars['concept_title'] = title_var
        ttk.Entry(concept_frame, textvariable=title_var, width=30).grid(row=0, column=1, sticky="ew", padx=5)
        
        ttk.Label(concept_frame, text="Аудитория:").grid(row=0, column=2, sticky="w", pady=2, padx=(20,0))
        audience_var = tk.StringVar(value=concept_data.get("target_audience", ""))
        audience_var.trace_add('write', self.on_structured_data_changed)
        self.screenplay_vars['concept_target_audience'] = audience_var
        ttk.Entry(concept_frame, textvariable=audience_var, width=30).grid(row=0, column=3, sticky="ew", padx=5)
        
        # Длительность
        ttk.Label(concept_frame, text="Длительность:").grid(row=0, column=4, sticky="w", pady=2, padx=(20,0))
        duration_var = tk.StringVar(value=concept_data.get("duration", ""))
        duration_var.trace_add('write', self.on_structured_data_changed)
        self.screenplay_vars['concept_duration'] = duration_var
        ttk.Entry(concept_frame, textvariable=duration_var, width=20).grid(row=0, column=5, sticky="ew", padx=5)
        
        # Логлайн
        ttk.Label(concept_frame, text="Логлайн:").grid(row=1, column=0, sticky="nw", pady=2)
        logline_text = tk.Text(concept_frame, height=2, wrap=tk.WORD)
        logline_text.insert("1.0", concept_data.get("logline", ""))
        logline_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.screenplay_vars['concept_logline'] = logline_text
        logline_text.grid(row=1, column=1, columnspan=5, sticky="ew", padx=5)
        
        # Жанр и настроение
        ttk.Label(concept_frame, text="Жанр и настроение:").grid(row=2, column=0, sticky="nw", pady=2)
        genre_text = tk.Text(concept_frame, height=2, wrap=tk.WORD)
        genre_text.insert("1.0", concept_data.get("genre_mood", ""))
        genre_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.screenplay_vars['concept_genre_mood'] = genre_text
        genre_text.grid(row=2, column=1, columnspan=5, sticky="ew", padx=5)
        
        # Визуальный стиль
        ttk.Label(concept_frame, text="Визуальный стиль:").grid(row=3, column=0, sticky="nw", pady=2)
        visual_text = tk.Text(concept_frame, height=2, wrap=tk.WORD)
        visual_text.insert("1.0", concept_data.get("visual_style", ""))
        visual_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.screenplay_vars['concept_visual_style'] = visual_text
        visual_text.grid(row=3, column=1, columnspan=5, sticky="ew", padx=5)
        
        # Стиль анимации
        ttk.Label(concept_frame, text="Стиль анимации:").grid(row=4, column=0, sticky="nw", pady=2)
        animation_text = tk.Text(concept_frame, height=2, wrap=tk.WORD)
        animation_text.insert("1.0", concept_data.get("animation_style", ""))
        animation_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.screenplay_vars['concept_animation_style'] = animation_text
        animation_text.grid(row=4, column=1, columnspan=5, sticky="ew", padx=5)
        
        # Темы
        ttk.Label(concept_frame, text="Темы:").grid(row=5, column=0, sticky="nw", pady=2)
        themes_text = tk.Text(concept_frame, height=2, wrap=tk.WORD)
        themes_text.insert("1.0", concept_data.get("themes", ""))
        themes_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.screenplay_vars['concept_themes'] = themes_text
        themes_text.grid(row=5, column=1, columnspan=5, sticky="ew", padx=5)
        
        # Музыкальная концепция
        ttk.Label(concept_frame, text="Музыкальная концепция:").grid(row=6, column=0, sticky="nw", pady=2)
        music_text = tk.Text(concept_frame, height=2, wrap=tk.WORD)
        music_text.insert("1.0", concept_data.get("music_concept", ""))
        music_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.screenplay_vars['concept_music_concept'] = music_text
        music_text.grid(row=6, column=1, columnspan=5, sticky="ew", padx=5)
        
        concept_frame.grid_columnconfigure(1, weight=1)
        concept_frame.grid_columnconfigure(3, weight=1)
        concept_frame.grid_columnconfigure(5, weight=1)
        
        # Описание мира (компактно)
        world_frame = ttk.LabelFrame(self.form_frame, text="Описание мира", padding=10)
        world_frame.pack(fill="x", pady=5)
        
        world_text = tk.Text(world_frame, height=3, wrap=tk.WORD)
        world_text.insert("1.0", data.get("world_description", ""))
        world_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.screenplay_vars['world_description'] = world_text
        world_text.pack(fill="x", padx=5, pady=5)
        
        # Персонажи сценария (краткая информация)
        chars_frame = ttk.LabelFrame(self.form_frame, text="Персонажи сценария", padding=10)
        chars_frame.pack(fill="x", pady=5)
        
        characters_data = data.get("characters", [])
        self.screenplay_vars['characters'] = []
        
        for i, char in enumerate(characters_data):
            char_subframe = ttk.Frame(chars_frame)
            char_subframe.pack(fill="x", pady=2)
            
            char_vars = {}
            
            # Имя персонажа
            ttk.Label(char_subframe, text=f"Персонаж {i+1}:").pack(side="left")
            name_var = tk.StringVar(value=char.get("name", ""))
            name_var.trace_add('write', self.on_structured_data_changed)
            char_vars['name'] = name_var
            ttk.Entry(char_subframe, textvariable=name_var, width=25).pack(side="left", padx=5)
            
            # Краткое описание внешности (одна строка)
            ttk.Label(char_subframe, text="Внешность:").pack(side="left", padx=(20,0))
            appearance_var = tk.StringVar(value=char.get("appearance", ""))
            appearance_var.trace_add('write', self.on_structured_data_changed)
            char_vars['appearance'] = appearance_var
            ttk.Entry(char_subframe, textvariable=appearance_var).pack(side="left", padx=5, fill="x", expand=True)
            
            self.screenplay_vars['characters'].append(char_vars)
        
        # === РЕДАКТОР СЦЕН (НИЖНЯЯ ЧАСТЬ) ===
        
        # Основной контейнер для сцен
        scenes_container = ttk.LabelFrame(self.form_frame, text="Сцены сценария", padding=10)
        scenes_container.pack(fill="both", expand=True, pady=5)
        
        # Панель кнопок управления сценами
        scene_controls = ttk.Frame(scenes_container)
        scene_controls.pack(fill="x", pady=(0, 10))
        
        ttk.Button(scene_controls, text="➕ Добавить сцену", command=self.add_scene).pack(side="left", padx=5)
        ttk.Button(scene_controls, text="🗑️ Удалить сцену", command=self.delete_scene).pack(side="left", padx=5)
        ttk.Button(scene_controls, text="↑ Вверх", command=self.move_scene_up).pack(side="left", padx=5)
        ttk.Button(scene_controls, text="↓ Вниз", command=self.move_scene_down).pack(side="left", padx=5)
        
        # Выбор сцены
        scene_select_frame = ttk.Frame(scenes_container)
        scene_select_frame.pack(fill="x", pady=(0, 10))
        
        ttk.Label(scene_select_frame, text="Выбрать сцену:").pack(side="left")
        self.scene_selector = ttk.Combobox(scene_select_frame, state="readonly", width=50)
        self.scene_selector.pack(side="left", padx=5, fill="x", expand=True)
        self.scene_selector.bind("<<ComboboxSelected>>", self.on_scene_combobox_selected)
        
        # Инициализация списка сцен
        self.refresh_scene_selector()
        
        # Редактор сцены на всю ширину
        scene_editor_container = ttk.Frame(scenes_container)
        scene_editor_container.pack(fill="both", expand=True, pady=5)
        
        # Скроллируемый фрейм для редактора сцены
        canvas = tk.Canvas(scene_editor_container)
        scene_scrollbar = ttk.Scrollbar(scene_editor_container, orient="vertical", command=canvas.yview)
        self.scene_editor_frame = ttk.Frame(canvas)
        
        self.scene_editor_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas_window = canvas.create_window((0, 0), window=self.scene_editor_frame, anchor="nw")
        canvas.configure(yscrollcommand=scene_scrollbar.set)
        
        canvas.pack(side="left", fill="both", expand=True)
        scene_scrollbar.pack(side="right", fill="y")
        
        # Добавляем поддержку прокрутки колесом мыши для редактора сцены (окончательная версия)
        bind_mousewheel_to_canvas_frame_ultimate(canvas, self.scene_editor_frame)
        
        # Настройка адаптивности размера фрейма редактора для заполнения всей ширины
        def configure_canvas(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas_width = event.width - scene_scrollbar.winfo_reqwidth()
            canvas.itemconfig(canvas_window, width=canvas_width)
        
        canvas.bind('<Configure>', configure_canvas)
        
        # Выбираем первую сцену
        if self.screenplay_data:
            self.current_scene_index = 0
            self.scene_selector.current(0)
            self.create_scene_editor()
    
    def sync_screenplay_data(self):
        """Синхронизация данных сценария"""
        if not hasattr(self, 'screenplay_vars') or not self.current_data:
            return
        
        # Синхронизация данных текущей сцены перед общей синхронизацией
        self.sync_current_scene_data()
        
        # Концепция
        if 'concept' not in self.current_data:
            self.current_data['concept'] = {}
        
        concept = self.current_data['concept']
        concept['title'] = self.screenplay_vars['concept_title'].get()
        concept['target_audience'] = self.screenplay_vars['concept_target_audience'].get()
        concept['logline'] = self.screenplay_vars['concept_logline'].get("1.0", "end-1c")
        concept['genre_mood'] = self.screenplay_vars['concept_genre_mood'].get("1.0", "end-1c")
        concept['visual_style'] = self.screenplay_vars['concept_visual_style'].get("1.0", "end-1c")
        concept['animation_style'] = self.screenplay_vars['concept_animation_style'].get("1.0", "end-1c")
        concept['themes'] = self.screenplay_vars['concept_themes'].get("1.0", "end-1c")
        concept['music_concept'] = self.screenplay_vars['concept_music_concept'].get("1.0", "end-1c")
        concept['duration'] = self.screenplay_vars['concept_duration'].get()
        
        # Описание мира
        self.current_data['world_description'] = self.screenplay_vars['world_description'].get("1.0", "end-1c")
        
        # Персонажи сценария
        if 'characters' not in self.current_data:
            self.current_data['characters'] = []
        
        self.current_data['characters'] = []
        for char_vars in self.screenplay_vars.get('characters', []):
            char_data = {
                'name': char_vars['name'].get(),
                'appearance': char_vars['appearance'].get()
            }
            self.current_data['characters'].append(char_data)
        
        # Сценарий (уже синхронизирован в sync_current_scene_data)
        self.current_data['screenplay'] = self.screenplay_data
    
    def sync_current_scene_data(self):
        """Синхронизация данных текущей редактируемой сцены"""
        if not hasattr(self, 'current_scene_vars') or self.current_scene_index < 0 or self.current_scene_index >= len(self.screenplay_data):
            return
        
        # Обновляем данные текущей сцены
        scene = self.screenplay_data[self.current_scene_index]
        scene['scene_number'] = self.current_scene_vars['scene_number'].get()
        scene['location_time'] = self.current_scene_vars['location_time'].get()
        scene['action'] = self.current_scene_vars['action'].get("1.0", "end-1c")
        
        # Персонажи сцены
        characters_text = self.current_scene_vars['characters'].get("1.0", "end-1c").strip()
        if characters_text:
            scene['characters'] = [char.strip() for char in characters_text.split('\n') if char.strip()]
        else:
            scene['characters'] = []
        
        # Диалоги (если есть)
        if 'dialogue' in self.current_scene_vars:
            dialogue_text = self.current_scene_vars['dialogue'].get("1.0", "end-1c").strip()
            scene['dialogue'] = dialogue_text
        
        # Визуальное описание (если есть)
        if 'visual' in self.current_scene_vars:
            scene['visual'] = self.current_scene_vars['visual'].get("1.0", "end-1c")
        
        # Камера (если есть)
        if 'camera' in self.current_scene_vars:
            scene['camera'] = self.current_scene_vars['camera'].get("1.0", "end-1c")
        
        # Диалоги
        if 'dialogue' in self.current_scene_vars:
            scene['dialogue'] = self.current_scene_vars['dialogue'].get("1.0", "end-1c")
        
        # Раскадровка
        if 'storyboard' in self.current_scene_vars:
            scene['storyboard'] = self.current_scene_vars['storyboard'].get("1.0", "end-1c")
        
        # Визуальное описание
        if 'visual' in self.current_scene_vars:
            scene['visual'] = self.current_scene_vars['visual'].get("1.0", "end-1c")
        
        # Камера
        if 'camera' in self.current_scene_vars:
            scene['camera'] = self.current_scene_vars['camera'].get("1.0", "end-1c")
        
        # Звук
        if 'sound' in self.current_scene_vars:
            scene['sound'] = self.current_scene_vars['sound'].get("1.0", "end-1c")
        
        # Переходы
        if 'transition' in self.current_scene_vars:
            scene['transition'] = self.current_scene_vars['transition'].get("1.0", "end-1c")
        
        # Заметки
        if 'notes' in self.current_scene_vars:
            scene['notes'] = self.current_scene_vars['notes'].get("1.0", "end-1c")
    
    def refresh_scene_selector(self):
        """Обновление селектора сцен"""
        if not hasattr(self, 'scene_selector'):
            return
            
        scene_names = []
        for i, scene in enumerate(self.screenplay_data):
            scene_num = scene.get('scene_number', i + 1)
            location = scene.get('location_time', 'Без локации')[:30]
            item_text = f"Сцена {scene_num}: {location}"
            scene_names.append(item_text)
        
        self.scene_selector['values'] = scene_names
    
    def on_scene_combobox_selected(self, event):
        """Обработчик выбора сцены в combobox"""
        selection = self.scene_selector.current()
        if selection >= 0:
            # Сохраняем данные текущей сцены перед переключением
            if hasattr(self, 'current_scene_index') and hasattr(self, 'current_scene_vars'):
                self.sync_current_scene_data()
            
            # Переключаемся на новую сцену
            self.current_scene_index = selection
            self.create_scene_editor()
    
    def create_scene_editor(self):
        """Создание редактора для выбранной сцены"""
        # Очищаем текущий редактор
        for widget in self.scene_editor_frame.winfo_children():
            widget.destroy()
        
        if self.current_scene_index < 0 or self.current_scene_index >= len(self.screenplay_data):
            return
        
        scene = self.screenplay_data[self.current_scene_index]
        self.current_scene_vars = {}
        
        # Заголовок сцены
        title_frame = ttk.Frame(self.scene_editor_frame)
        title_frame.pack(fill="x", pady=(0, 10))
        
        ttk.Label(title_frame, text=f"Редактирование сцены {scene.get('scene_number', self.current_scene_index + 1)}", 
                 font=("Arial", 12, "bold")).pack(anchor="w")
        
        # Номер сцены
        scene_info_frame = ttk.LabelFrame(self.scene_editor_frame, text="Информация о сцене", padding=10)
        scene_info_frame.pack(fill="x", pady=5)
        
        ttk.Label(scene_info_frame, text="Номер сцены:").grid(row=0, column=0, sticky="w", pady=2)
        scene_number_var = tk.IntVar(value=scene.get('scene_number', self.current_scene_index + 1))
        scene_number_var.trace_add('write', self.on_structured_data_changed)
        self.current_scene_vars['scene_number'] = scene_number_var
        ttk.Entry(scene_info_frame, textvariable=scene_number_var, width=10).grid(row=0, column=1, sticky="w", padx=5)
        
        # Локация/Время
        ttk.Label(scene_info_frame, text="Локация/Время:").grid(row=1, column=0, sticky="w", pady=2)
        location_var = tk.StringVar(value=scene.get('location_time', ''))
        location_var.trace_add('write', self.on_structured_data_changed)
        self.current_scene_vars['location_time'] = location_var
        ttk.Entry(scene_info_frame, textvariable=location_var, width=50).grid(row=1, column=1, sticky="ew", padx=5)
        
        scene_info_frame.grid_columnconfigure(1, weight=1)
        
        # Персонажи сцены
        chars_frame = ttk.LabelFrame(self.scene_editor_frame, text="Персонажи сцены", padding=10)
        chars_frame.pack(fill="x", pady=5)
        
        ttk.Label(chars_frame, text="Персонажи (по одному на строке):").pack(anchor="w")
        characters_text = tk.Text(chars_frame, height=3, wrap=tk.WORD)
        if 'characters' in scene and scene['characters']:
            characters_text.insert("1.0", '\n'.join(scene['characters']))
        characters_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.current_scene_vars['characters'] = characters_text
        characters_text.pack(fill="x", padx=5, pady=5)
        
        # Действие
        action_frame = ttk.LabelFrame(self.scene_editor_frame, text="Действие", padding=10)
        action_frame.pack(fill="x", pady=5)
        
        action_text = tk.Text(action_frame, height=5, wrap=tk.WORD)
        action_text.insert("1.0", scene.get('action', ''))
        action_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.current_scene_vars['action'] = action_text
        action_text.pack(fill="x", padx=5, pady=5)
        
        # === ДИАЛОГИ ===
        dialogue_frame = ttk.LabelFrame(self.scene_editor_frame, text="Диалоги", padding=10)
        dialogue_frame.pack(fill="x", pady=5)
        
        dialogue_text = tk.Text(dialogue_frame, height=5, wrap=tk.WORD)
        dialogue_text.insert("1.0", scene.get('dialogue', ''))
        dialogue_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.current_scene_vars['dialogue'] = dialogue_text
        dialogue_text.pack(fill="x", padx=5, pady=5)
        
        # === ВИЗУАЛЬНОЕ ОПИСАНИЕ И РАСКАДРОВКА ===
        visual_frame = ttk.LabelFrame(self.scene_editor_frame, text="Визуальное описание / Раскадровка", padding=10)
        visual_frame.pack(fill="x", pady=5)
        
        # Раскадровка
        ttk.Label(visual_frame, text="Раскадровка:").pack(anchor="w")
        storyboard_text = tk.Text(visual_frame, height=4, wrap=tk.WORD)
        storyboard_text.insert("1.0", scene.get('storyboard', ''))
        storyboard_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.current_scene_vars['storyboard'] = storyboard_text
        storyboard_text.pack(fill="x", padx=5, pady=(0,10))
        
        # Визуальное описание
        ttk.Label(visual_frame, text="Визуальное описание:").pack(anchor="w")
        visual_text = tk.Text(visual_frame, height=4, wrap=tk.WORD)
        visual_text.insert("1.0", scene.get('visual', ''))
        visual_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.current_scene_vars['visual'] = visual_text
        visual_text.pack(fill="x", padx=5, pady=5)
        
        # === РАБОТА С КАМЕРОЙ ===
        camera_frame = ttk.LabelFrame(self.scene_editor_frame, text="Работа с камерой", padding=10)
        camera_frame.pack(fill="x", pady=5)
        
        camera_text = tk.Text(camera_frame, height=4, wrap=tk.WORD)
        camera_text.insert("1.0", scene.get('camera', ''))
        camera_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.current_scene_vars['camera'] = camera_text
        camera_text.pack(fill="x", padx=5, pady=5)
        
        # === ЗВУК И МУЗЫКА ===
        sound_frame = ttk.LabelFrame(self.scene_editor_frame, text="Звук и музыка", padding=10)
        sound_frame.pack(fill="x", pady=5)
        
        sound_text = tk.Text(sound_frame, height=3, wrap=tk.WORD)
        sound_text.insert("1.0", scene.get('sound', ''))
        sound_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.current_scene_vars['sound'] = sound_text
        sound_text.pack(fill="x", padx=5, pady=5)
        
        # === ПЕРЕХОДЫ ===
        transition_frame = ttk.LabelFrame(self.scene_editor_frame, text="Переходы", padding=10)
        transition_frame.pack(fill="x", pady=5)
        
        transition_text = tk.Text(transition_frame, height=2, wrap=tk.WORD)
        transition_text.insert("1.0", scene.get('transition', ''))
        transition_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.current_scene_vars['transition'] = transition_text
        transition_text.pack(fill="x", padx=5, pady=5)
        
        # === ЗАМЕТКИ И КОММЕНТАРИИ ===
        notes_frame = ttk.LabelFrame(self.scene_editor_frame, text="Заметки и комментарии", padding=10)
        notes_frame.pack(fill="x", pady=5)
        
        notes_text = tk.Text(notes_frame, height=3, wrap=tk.WORD)
        notes_text.insert("1.0", scene.get('notes', ''))
        notes_text.bind('<KeyRelease>', self.on_structured_data_changed)
        self.current_scene_vars['notes'] = notes_text
        notes_text.pack(fill="x", padx=5, pady=5)
    
    def add_scene(self):
        """Добавление новой сцены"""
        # Сохраняем текущую сцену
        self.sync_current_scene_data()
        
        # Создаем новую сцену
        new_scene = {
            "scene_number": len(self.screenplay_data) + 1,
            "location_time": "НОВАЯ ЛОКАЦИЯ - ВРЕМЯ",
            "action": "Описание действия новой сцены...",
            "characters": []
        }
        
        self.screenplay_data.append(new_scene)
        self.refresh_scene_selector()
        
        # Выбираем новую сцену
        self.current_scene_index = len(self.screenplay_data) - 1
        self.scene_selector.current(self.current_scene_index)
        self.create_scene_editor()
        
        self.on_structured_data_changed()
    
    def delete_scene(self):
        """Удаление выбранной сцены"""
        if self.current_scene_index < 0 or self.current_scene_index >= len(self.screenplay_data):
            return
        
        if len(self.screenplay_data) <= 1:
            messagebox.showwarning("Предупреждение", "Нельзя удалить последнюю сцену")
            return
        
        # Подтверждение удаления
        scene = self.screenplay_data[self.current_scene_index]
        scene_name = f"Сцена {scene.get('scene_number', self.current_scene_index + 1)}"
        
        if messagebox.askyesno("Подтверждение", f"Удалить {scene_name}?"):
            del self.screenplay_data[self.current_scene_index]
            
            # Корректируем индекс
            if self.current_scene_index >= len(self.screenplay_data):
                self.current_scene_index = len(self.screenplay_data) - 1
            
            self.refresh_scene_selector()
            
            # Выбираем сцену после удаления
            if self.screenplay_data:
                self.scene_selector.current(self.current_scene_index)
                self.create_scene_editor()
            
            self.on_structured_data_changed()
    
    def move_scene_up(self):
        """Перемещение сцены вверх"""
        if self.current_scene_index <= 0:
            return
        
        # Сохраняем текущую сцену
        self.sync_current_scene_data()
        
        # Меняем местами
        self.screenplay_data[self.current_scene_index], self.screenplay_data[self.current_scene_index - 1] = \
            self.screenplay_data[self.current_scene_index - 1], self.screenplay_data[self.current_scene_index]
        
        self.current_scene_index -= 1
        self.refresh_scene_selector()
        self.scene_selector.current(self.current_scene_index)
        
        self.on_structured_data_changed()
    
    def move_scene_down(self):
        """Перемещение сцены вниз"""
        if self.current_scene_index >= len(self.screenplay_data) - 1:
            return
        
        # Сохраняем текущую сцену
        self.sync_current_scene_data()
        
        # Меняем местами
        self.screenplay_data[self.current_scene_index], self.screenplay_data[self.current_scene_index + 1] = \
            self.screenplay_data[self.current_scene_index + 1], self.screenplay_data[self.current_scene_index]
        
        self.current_scene_index += 1
        self.refresh_scene_selector()
        self.scene_selector.current(self.current_scene_index)
        
        self.on_structured_data_changed()
    
    def create_generic_form(self):
        """Создание общей формы для других файлов"""
        ttk.Label(self.form_frame, text="Структурированное редактирование недоступно для этого типа файла.\nИспользуйте Raw JSON режим.", 
                 style="Subtitle.TLabel").pack(pady=50)
    
    def update_raw_editor(self):
        """Обновление raw редактора"""
        if self.current_data is not None:
            json_text = json.dumps(self.current_data, indent=2, ensure_ascii=False)
            self.raw_text.delete("1.0", tk.END)
            self.raw_text.insert("1.0", json_text)
            self.highlight_json_syntax()
        else:
            self.raw_text.delete("1.0", tk.END)
    
    def highlight_json_syntax(self):
        """Подсветка JSON синтаксиса"""
        # Простая подсветка (можно улучшить)
        
        # Очищаем существующие теги
        for tag in ["string", "number", "keyword", "bracket"]:
            self.raw_text.tag_delete(tag)
        
        # TODO: Реализовать подсветку синтаксиса
    
    def add_character_field(self):
        """Добавление поля персонажа"""
        char_var = tk.StringVar()
        char_var.trace_add('write', self.on_structured_data_changed)
        self.brief_characters.append(char_var)
        
        # Находим фрейм персонажей и добавляем поле
        for child in self.form_frame.winfo_children():
            if isinstance(child, ttk.LabelFrame) and "персонажи" in child.cget("text").lower():
                # Удаляем кнопку
                button = None
                for widget in child.winfo_children():
                    if isinstance(widget, ttk.Button):
                        button = widget
                        break
                if button:
                    button.destroy()
                
                # Добавляем новое поле
                ttk.Entry(child, textvariable=char_var, width=30).pack(pady=2)
                
                # Возвращаем кнопку
                ttk.Button(child, text="+ Добавить персонажа", 
                          command=self.add_character_field).pack(pady=5)
                break
    
    def add_page(self):
        """Добавление новой страницы"""
        new_page = {
            "page": len(self.page_widgets) + 1,
            "title": f"Страница {len(self.page_widgets) + 1}",
            "body": ""
        }
        self.add_page_tab(new_page)
        self.on_structured_data_changed()
    
    def on_structured_data_changed(self, *args):
        """Обработчик изменения структурированных данных"""
        self.has_changes = True
        self.status_label.config(text="Файл изменен *")
        
        # Обновляем raw редактор
        self.sync_structured_to_raw()
    
    def on_raw_text_changed(self, event):
        """Обработчик изменения raw текста"""
        self.has_changes = True
        self.status_label.config(text="Файл изменен *")
        
        # Проверяем валидность JSON
        try:
            json_text = self.raw_text.get("1.0", tk.END)
            json.loads(json_text)
            self.raw_text.config(background="white")
        except json.JSONDecodeError:
            self.raw_text.config(background="#FFE6E6")  # Светло-красный
    
    def sync_structured_to_raw(self):
        """Синхронизация структурированных данных в raw редактор"""
        try:
            # Всегда используется гибридный редактор
            if self.universal_form_generator:
                # Получаем данные из гибридной формы
                form_data = self.universal_form_generator.get_form_data()
                
                # Если мы работали с извлеченным массивом, восстанавливаем структуру
                array_types = ["shots", "beats", "characters", "locations"]
                if (self.current_file_type in array_types and 
                    isinstance(self.current_data, dict) and 
                    "items" in self.current_data and 
                    isinstance(form_data, list)):
                    
                    # Обновляем только items, сохраняя остальные поля
                    self.current_data["items"] = form_data
                    logger.info(f"🔄 Обновлен массив items для {self.current_file_type}: {len(form_data)} элементов")
                else:
                    # Обычная синхронизация
                    self.current_data = form_data
            else:
                # Fallback на legacy формы (для редких случаев)
                logger.warning("Fallback к legacy синхронизации")
                if self.current_file_type == "brief":
                    self.sync_brief_data()
                elif self.current_file_type == "story":
                    self.sync_story_data()
                elif self.current_file_type == "shots":
                    self.sync_shots_data()
                elif self.current_file_type == "characters":
                    self.sync_characters_data()
                elif self.current_file_type == "locations":
                    self.sync_locations_data()
                elif self.current_file_type == "screenplay":
                    self.sync_screenplay_data()
            
            # Обновляем raw редактор
            self.update_raw_editor()
            
        except Exception as e:
            logger.error(f"Ошибка синхронизации данных: {e}")
    
    def sync_brief_data(self):
        """Синхронизация данных brief"""
        if not hasattr(self, 'brief_title'):
            return
        
        self.current_data["title"] = self.brief_title.get()
        self.current_data["genre"] = self.brief_genre.get()
        self.current_data["target_age"] = self.brief_age.get()
        self.current_data["description"] = self.brief_description.get("1.0", tk.END).strip()
        
        # Персонажи
        characters = []
        for char_var in self.brief_characters:
            char_name = char_var.get().strip()
            if char_name:
                characters.append(char_name)
        self.current_data["main_characters"] = characters
    
    def sync_story_data(self):
        """Синхронизация данных story"""
        if not hasattr(self, 'story_title'):
            return
        
        self.current_data["title"] = self.story_title.get()
        
        # Страницы
        pages = []
        for i, page_widget in enumerate(self.page_widgets):
            page = {
                "page": i + 1,
                "title": page_widget["title_var"].get(),
                "body": page_widget["text_widget"].get("1.0", tk.END).strip()
            }
            pages.append(page)
        
        self.current_data["pages"] = pages
    
    def sync_shots_data(self):
        """Синхронизация данных shots"""
        if not hasattr(self, 'current_data') or self.current_data is None:
            return
        
        # Инициализируем список items если его нет
        if "items" not in self.current_data:
            self.current_data["items"] = []
        
        # Обновляем данные каждого кадра
        items = self.current_data["items"]
        for i in range(len(items)):
            shot = items[i]
            
            # Собираем данные из полей формы
            try:
                # Основные параметры - защищаем все get() вызовы
                def safe_get(widget_name, is_text=False):
                    """Безопасное получение значения из виджета или переменной"""
                    if hasattr(self, widget_name):
                        obj = getattr(self, widget_name)
                        try:
                            # Проверяем, это Tkinter переменная (StringVar, IntVar, etc.) или виджет
                            if hasattr(obj, 'winfo_exists'):
                                # Это виджет
                                if obj.winfo_exists():
                                    if is_text:
                                        return obj.get("1.0", tk.END).strip()
                                    else:
                                        return obj.get()
                            else:
                                # Это переменная Tkinter (StringVar, IntVar, etc.)
                                return obj.get()
                        except tk.TclError:
                            pass  # Виджет/переменная уже уничтожены
                    return None
                
                # Основные параметры
                page_number = safe_get(f"shot_{i}_page_number")
                if page_number is not None:
                    shot["page_number"] = page_number
                    
                scene_number = safe_get(f"shot_{i}_scene_number")
                if scene_number is not None:
                    shot["scene_number"] = scene_number
                    
                shot_number = safe_get(f"shot_{i}_shot_number")
                if shot_number is not None:
                    shot["shot_number"] = shot_number
                
                # Параметры камеры
                shot_type = safe_get(f"shot_{i}_shot_type")
                if shot_type is not None:
                    shot["shot_type"] = shot_type
                    
                camera_plan = safe_get(f"shot_{i}_camera_plan")
                if camera_plan is not None:
                    shot["camera_plan"] = camera_plan
                    
                timing = safe_get(f"shot_{i}_timing")
                if timing is not None:
                    shot["timing"] = timing
                
                # Промпты
                english_prompt = safe_get(f"shot_{i}_english_prompt", is_text=True)
                if english_prompt is not None:
                    shot["english_prompt"] = english_prompt
                    
                video_prompt = safe_get(f"shot_{i}_video_prompt", is_text=True)
                if video_prompt is not None:
                    shot["video_prompt"] = video_prompt
                    
                negative_prompt = safe_get(f"shot_{i}_negative_prompt", is_text=True)
                if negative_prompt is not None:
                    shot["negative_prompt"] = negative_prompt
                
                # Технические параметры
                width = safe_get(f"shot_{i}_width")
                if width is not None:
                    shot["width"] = width
                    
                height = safe_get(f"shot_{i}_height")
                if height is not None:
                    shot["height"] = height
                    
                cfg_scale = safe_get(f"shot_{i}_true_cfg_scale")
                if cfg_scale is not None:
                    shot["true_cfg_scale"] = cfg_scale
                    
                inference_steps = safe_get(f"shot_{i}_num_inference_steps")
                if inference_steps is not None:
                    shot["num_inference_steps"] = inference_steps
                    
            except Exception as e:
                logger.warning(f"Ошибка синхронизации кадра {i}: {e}")
                continue
    
    def sync_characters_data(self):
        """Синхронизация данных персонажей из структурированной формы"""
        if not isinstance(self.current_data, list) or not hasattr(self, 'character_vars'):
            return
            
        try:
            # Собираем данные из сохраненных переменных
            for i, char_vars in self.character_vars.items():
                if i < len(self.current_data):
                    char_data = self.current_data[i]
                    
                    # Обновляем простые поля
                    for field_name, var in char_vars.items():
                        if isinstance(var, dict):  # Вложенные атрибуты
                            if field_name == 'immutable_attributes':
                                if 'immutable_attributes' not in char_data:
                                    char_data['immutable_attributes'] = {}
                                for attr_name, attr_var in var.items():
                                    if attr_name == 'unique_features' and hasattr(attr_var, 'get'):
                                        # Обработка Text widget для списка
                                        try:
                                            if hasattr(attr_var, 'winfo_exists'):
                                                if attr_var.winfo_exists():
                                                    text_content = attr_var.get("1.0", tk.END).strip()
                                                    char_data['immutable_attributes'][attr_name] = [line.strip() for line in text_content.split('\n') if line.strip()]
                                            else:
                                                # Это переменная Tkinter
                                                text_content = attr_var.get("1.0", tk.END).strip()
                                                char_data['immutable_attributes'][attr_name] = [line.strip() for line in text_content.split('\n') if line.strip()]
                                        except tk.TclError:
                                            pass  # Виджет/переменная уже уничтожены
                                    elif hasattr(attr_var, 'get'):
                                        try:
                                            char_data['immutable_attributes'][attr_name] = attr_var.get()
                                        except tk.TclError:
                                            pass  # Переменная уже уничтожена
                            elif field_name == 'variable_attributes':
                                if 'variable_attributes' not in char_data:
                                    char_data['variable_attributes'] = {}
                                for attr_name, attr_var in var.items():
                                    if attr_name == 'accessories' and hasattr(attr_var, 'get'):
                                        # Обработка Text widget для списка
                                        try:
                                            if hasattr(attr_var, 'winfo_exists'):
                                                if attr_var.winfo_exists():
                                                    text_content = attr_var.get("1.0", tk.END).strip()
                                                    char_data['variable_attributes'][attr_name] = [line.strip() for line in text_content.split('\n') if line.strip()]
                                            else:
                                                # Это переменная Tkinter
                                                text_content = attr_var.get("1.0", tk.END).strip()
                                                char_data['variable_attributes'][attr_name] = [line.strip() for line in text_content.split('\n') if line.strip()]
                                        except tk.TclError:
                                            pass  # Виджет/переменная уже уничтожены
                                    elif hasattr(attr_var, 'get'):
                                        try:
                                            char_data['variable_attributes'][attr_name] = attr_var.get()
                                        except tk.TclError:
                                            pass  # Переменная уже уничтожена
                        elif hasattr(var, 'get') and callable(getattr(var, 'get')):
                            if field_name in ['gesture_set', 'speech_patterns', 'no_go_rules']:
                                # Обработка Text widget для списков
                                try:
                                    if hasattr(var, 'winfo_exists'):
                                        if var.winfo_exists():
                                            text_content = var.get("1.0", tk.END).strip()
                                            char_data[field_name] = [line.strip() for line in text_content.split('\n') if line.strip()]
                                    else:
                                        # Это переменная Tkinter
                                        text_content = var.get("1.0", tk.END).strip()
                                        char_data[field_name] = [line.strip() for line in text_content.split('\n') if line.strip()]
                                except tk.TclError:
                                    pass  # Виджет/переменная уже уничтожены
                            else:
                                # Обработка StringVar и других простых полей
                                try:
                                    char_data[field_name] = var.get()
                                except tk.TclError:
                                    pass  # Переменная уже уничтожена
                            
        except Exception as e:
            logger.warning(f"Ошибка синхронизации персонажей: {e}")
    
    def sync_locations_data(self):
        """Синхронизация данных локаций из структурированной формы"""
        if not isinstance(self.current_data, list) or not hasattr(self, 'location_vars'):
            return
            
        try:
            # Собираем данные из сохраненных переменных
            for i, loc_vars in self.location_vars.items():
                if i < len(self.current_data):
                    loc_data = self.current_data[i]
                    
                    # Обновляем данные из переменных
                    for field_name, var in loc_vars.items():
                        if hasattr(var, 'get') and callable(getattr(var, 'get')):
                            if field_name in ['key_objects', 'color_palette']:
                                # Обработка Text widget для списков
                                text_content = var.get("1.0", tk.END).strip()
                                loc_data[field_name] = [line.strip() for line in text_content.split('\n') if line.strip()]
                            elif field_name == 'description':
                                # Обработка Text widget для описания
                                loc_data[field_name] = var.get("1.0", tk.END).strip()
                            else:
                                # Обработка StringVar и других простых полей
                                loc_data[field_name] = var.get()
                            
        except Exception as e:
            logger.warning(f"Ошибка синхронизации локаций: {e}")
    
    def save_current_file(self):
        """Сохранение текущего файла"""
        if not self.file_manager or not self.current_file_type:
            messagebox.showwarning("Предупреждение", "Нет файла для сохранения")
            return
        
        try:
            # Получаем данные из активного редактора
            current_tab = self.editor_notebook.index(self.editor_notebook.select())
            
            if current_tab == 1:  # Raw JSON редактор
                # Парсим JSON из текстового поля
                json_text = self.raw_text.get("1.0", tk.END)
                self.current_data = json.loads(json_text)
            else:
                # Данные уже синхронизированы из структурированной формы
                pass
            
            # Сохраняем файл
            success = self.file_manager.save_json_file(
                self.current_data, 
                self.current_file_type,
                create_backup=True
            )
            
            if success:
                self.has_changes = False
                self.status_label.config(text="Файл сохранен")
                self.validate_current_file()
                
                # Уведомляем о сохранении
                if self.on_file_changed:
                    file_path = self.file_manager._get_default_file_path(self.current_file_type)
                    self.on_file_changed(self.current_file_type, file_path)
                
                messagebox.showinfo("Успех", "Файл сохранен")
            else:
                messagebox.showerror("Ошибка", "Не удалось сохранить файл")
            
        except json.JSONDecodeError as e:
            messagebox.showerror("Ошибка JSON", f"Некорректный JSON:\n{e}")
        except Exception as e:
            logger.error(f"Ошибка сохранения файла: {e}")
            messagebox.showerror("Ошибка", f"Ошибка сохранения:\n{e}")
    
    def reload_current_file(self):
        """Перезагрузка текущего файла"""
        if self.current_file_type:
            if self.has_changes:
                result = messagebox.askyesnocancel(
                    "Несохраненные изменения",
                    "У вас есть несохраненные изменения. Перезагрузить файл?"
                )
                if result is not True:
                    return
            
            self.load_file(self.current_file_type)
    
    def validate_current_file(self):
        """Валидация текущего файла"""
        if not self.current_data or not self.current_file_type:
            return
        
        try:
            # Очищаем область валидации
            self.validation_text.delete("1.0", tk.END)
            
            # Синхронизируем данные перед валидацией
            self.sync_structured_to_raw()
            
            # Если используется универсальный редактор, используем его валидацию
            if self.universal_form_generator:
                errors = self.universal_form_generator.validate_form()
            else:
                # Используем стандартную валидацию
                errors = json_validator.validate_data(self.current_data, self.current_file_type)
            
            if not errors:
                self.validation_text.insert(tk.END, "✓ Валидация прошла успешно\n", "success")
            else:
                self.validation_text.insert(tk.END, "❌ Ошибки валидации:\n", "error")
                for error in errors:
                    self.validation_text.insert(tk.END, f"  • {error}\n", "error")
            
            # Дополнительные проверки через json_validator
            if hasattr(json_validator, 'validate_file'):
                extended_result = json_validator.validate_file(
                    self.file_manager._get_default_file_path(self.current_file_type) if self.file_manager else "",
                    self.current_file_type
                )
                
                # Показываем предупреждения
                if extended_result.get("warnings"):
                    self.validation_text.insert(tk.END, "\n⚠️ Предупреждения:\n", "warning")
                    for warning in extended_result["warnings"]:
                        self.validation_text.insert(tk.END, f"  • {warning}\n", "warning")
                
                # Показываем предложения
                if extended_result.get("suggestions"):
                    self.validation_text.insert(tk.END, "\n💡 Предложения:\n", "info")
                    for suggestion in extended_result["suggestions"]:
                        self.validation_text.insert(tk.END, f"  • {suggestion}\n", "info")
            
        except Exception as e:
            logger.error(f"Ошибка валидации: {e}")
            self.validation_text.insert(tk.END, f"Ошибка валидации: {e}\n", "error")
    
    def reload_ui_config(self):
        """Перечитывает UI конфигурацию и пересоздает форму"""
        try:
            # Сохраняем текущие данные
            current_data = {}
            if self.universal_form_generator:
                try:
                    current_data = self.universal_form_generator.get_form_data()
                except Exception as e:
                    logger.warning(f"Не удалось получить данные формы: {e}")
                    current_data = self.current_data
            
            # Сохраняем текущие настройки
            current_file_type = self.current_file_type
            
            # Пересоздаем универсальный редактор с новой конфигурацией
            if current_data and current_file_type:
                from gui.universal_json_editor import UniversalFormGenerator
                
                # Создаем новый генератор (это перечитает ui_config.json)
                self.universal_form_generator = UniversalFormGenerator(
                    current_data,
                    current_file_type,
                    self.on_structured_data_changed
                )
                
                # Пересоздаем форму
                self.universal_form_generator.create_form(self.structured_frame)
                
                # Показываем успех в области валидации
                self.validation_text.delete("1.0", tk.END)
                self.validation_text.insert(tk.END, "✅ UI конфигурация перечитана и форма обновлена\n", "success")
                
                logger.info("UI конфигурация перечитана успешно")
            else:
                self.validation_text.delete("1.0", tk.END)
                self.validation_text.insert(tk.END, "⚠️ Нет данных для обновления UI конфигурации\n", "warning")
                
        except Exception as e:
            logger.error(f"Ошибка перечитывания UI конфигурации: {e}")
            self.validation_text.delete("1.0", tk.END)
            self.validation_text.insert(tk.END, f"❌ Ошибка перечитывания UI конфигурации: {e}\n", "error")
    
    def validate_current_project(self):
        """Валидация всего проекта"""
        if not self.current_project:
            return
        
        try:
            # Получаем пути к файлам проекта
            project_files = self.project_manager.get_project_files(self.current_project.project_id)
            
            # Проверяем согласованность
            result = json_validator.validate_project_consistency(project_files)
            
            # Показываем результаты
            self.validation_text.delete("1.0", tk.END)
            
            if result["consistent"]:
                self.validation_text.insert(tk.END, "✓ Проект согласован\n", "success")
            else:
                self.validation_text.insert(tk.END, "❌ Проект содержит ошибки согласованности:\n", "error")
                for error in result["errors"]:
                    self.validation_text.insert(tk.END, f"  • {error}\n", "error")
            
            if result["warnings"]:
                self.validation_text.insert(tk.END, "\n⚠️ Предупреждения:\n", "warning")
                for warning in result["warnings"]:
                    self.validation_text.insert(tk.END, f"  • {warning}\n", "warning")
            
        except Exception as e:
            logger.error(f"Ошибка валидации проекта: {e}")
            messagebox.showerror("Ошибка", f"Ошибка валидации проекта:\n{e}")
    
    def has_unsaved_changes(self) -> bool:
        """Проверка наличия несохраненных изменений"""
        return self.has_changes
    
    def save_all_files(self):
        """Сохранение всех файлов"""
        if self.has_changes:
            self.save_current_file()
    
    def undo(self):
        """Отмена последнего действия"""
        # TODO: Реализовать undo/redo функциональность
        try:
            widget = self.focus_get()
            if hasattr(widget, 'edit_undo'):
                widget.edit_undo()
        except Exception:
            pass
    
    def redo(self):
        """Повтор последнего отмененного действия"""
        # TODO: Реализовать undo/redo функциональность
        try:
            widget = self.focus_get()
            if hasattr(widget, 'edit_redo'):
                widget.edit_redo()
        except Exception:
            pass
