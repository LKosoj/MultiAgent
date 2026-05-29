"""
Главное окно приложения StoryBook Manager
========================================

Основной GUI интерфейс приложения с навигацией и рабочими панелями.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import logging
from typing import Optional

from StoryBookManager.config.settings import app_settings
from StoryBookManager.core.project_manager import ProjectManager
from StoryBookManager.gui.project_panel import ProjectPanel
from StoryBookManager.gui.editor_panel import EditorPanel
from StoryBookManager.gui.media_panel import MediaPanel
from StoryBookManager.gui.generation_panel import GenerationPanel
from StoryBookManager.gui.settings_dialog import SettingsDialog

logger = logging.getLogger(__name__)


class MainWindow:
    """Главное окно приложения"""
    
    def __init__(self, root: tk.Tk):
        self.root = root
        self.project_manager = ProjectManager()
        self.current_project = None
        
        # Настройка главного окна
        self.setup_window()
        
        # Сначала UI (панели), затем меню: пункты меню ссылаются на self.project_panel и др.
        self.create_ui()
        self.create_menu()
        
        # Загрузка начальных данных
        self.load_initial_data()
        
        logger.info("Главное окно инициализировано")
    
    def setup_window(self):
        """Настройка главного окна"""
        self.root.title("StoryBook Manager - Управление проектами сказок")
        
        # Размер и позиция окна
        geometry = app_settings.get("window_geometry", "1200x800")
        self.root.geometry(geometry)
        
        # Минимальный размер
        self.root.minsize(1000, 600)
        
        # Иконка приложения (если есть)
        try:
            # TODO: Добавить иконку приложения
            pass
        except Exception:
            pass
        
        # Обработчик закрытия окна
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # Настройка стилей ttk
        self.setup_styles()
    
    def setup_styles(self):
        """Настройка стилей ttk"""
        style = ttk.Style()
        
        # Используем стандартную тему
        available_themes = style.theme_names()
        if "clam" in available_themes:
            style.theme_use("clam")
        
        # Кастомные стили
        style.configure("Title.TLabel", font=("Arial", 12, "bold"))
        style.configure("Subtitle.TLabel", font=("Arial", 10, "bold"))
        style.configure("Header.TFrame", relief="raised", borderwidth=1)
    
    def create_menu(self):
        """Создание главного меню"""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)
        
        # Меню "Файл"
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Файл", menu=file_menu)
        file_menu.add_command(label="Новый проект...", command=self.new_project)
        file_menu.add_separator()
        file_menu.add_command(label="Открыть проект...", command=self.open_project)
        file_menu.add_command(label="Обновить список проектов", command=self.refresh_projects)
        file_menu.add_separator()
        file_menu.add_command(label="Настройки...", command=self.show_settings)
        file_menu.add_separator()
        file_menu.add_command(label="Выход", command=self.on_closing)
        
        # Меню "Правка"
        edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Правка", menu=edit_menu)
        edit_menu.add_command(label="Отменить", command=self.undo, accelerator="Ctrl+Z")
        edit_menu.add_command(label="Повторить", command=self.redo, accelerator="Ctrl+Y")
        edit_menu.add_separator()
        edit_menu.add_command(label="Копировать", command=self.copy, accelerator="Ctrl+C")
        edit_menu.add_command(label="Вставить", command=self.paste, accelerator="Ctrl+V")
        
        # Меню "Проект"
        project_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Проект", menu=project_menu)
        project_menu.add_command(label="Запустить полный pipeline", command=self.run_full_pipeline)
        project_menu.add_command(label="Запустить с шага...", command=self.run_from_step)
        project_menu.add_separator()
        project_menu.add_command(label="Создать backup", command=self.backup_project)
        project_menu.add_command(label="Экспорт проекта...", command=self.project_panel.export_selected_project)
        project_menu.add_separator()
        project_menu.add_command(label="Валидация проекта", command=self.validate_project)
        
        # Меню "Инструменты"
        tools_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Инструменты", menu=tools_menu)
        tools_menu.add_command(label="Очистить кэш медиа", command=self.clear_media_cache)
        tools_menu.add_command(label="Просмотр логов", command=self.view_logs)
        
        # Меню "Помощь"
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Помощь", menu=help_menu)
        help_menu.add_command(label="Руководство пользователя", command=self.show_user_guide)
        help_menu.add_command(label="Горячие клавиши", command=self.show_shortcuts)
        help_menu.add_separator()
        help_menu.add_command(label="О программе", command=self.show_about)
        
        # Горячие клавиши
        self.root.bind("<Control-n>", lambda e: self.new_project())
        self.root.bind("<Control-o>", lambda e: self.open_project())
        self.root.bind("<Control-s>", lambda e: self.save_current())
        self.root.bind("<F5>", lambda e: self.refresh_projects())
    
    def create_ui(self):
        """Создание пользовательского интерфейса"""
        # Главный контейнер
        main_container = ttk.Frame(self.root)
        main_container.pack(fill="both", expand=True, padx=5, pady=5)
        
        # Toolbar
        self.create_toolbar(main_container)
        
        # Notebook для вкладок
        self.notebook = ttk.Notebook(main_container)
        self.notebook.pack(fill="both", expand=True, pady=(5, 0))
        
        # Создание панелей
        self.create_panels()
        
        # Статусная строка
        self.create_status_bar(main_container)
    
    def create_toolbar(self, parent):
        """Создание панели инструментов"""
        toolbar_frame = ttk.Frame(parent, style="Header.TFrame")
        toolbar_frame.pack(fill="x", pady=(0, 5))
        
        # Кнопки быстрого доступа
        ttk.Button(toolbar_frame, text="📁 Открыть", command=self.open_project).pack(side="left", padx=(5, 2))
        ttk.Button(toolbar_frame, text="🔄 Обновить", command=self.refresh_projects).pack(side="left", padx=2)
        
        ttk.Separator(toolbar_frame, orient="vertical").pack(side="left", fill="y", padx=10)
        
        ttk.Button(toolbar_frame, text="💾 Сохранить", command=self.save_current).pack(side="left", padx=2)
        ttk.Button(toolbar_frame, text="🚀 Запустить", command=self.run_full_pipeline).pack(side="left", padx=2)
        
        ttk.Separator(toolbar_frame, orient="vertical").pack(side="left", fill="y", padx=10)
        
        ttk.Button(toolbar_frame, text="⚙️ Настройки", command=self.show_settings).pack(side="left", padx=2)
        
        # Информация о текущем проекте
        self.project_info_label = ttk.Label(toolbar_frame, text="Проект не выбран", style="Subtitle.TLabel")
        self.project_info_label.pack(side="right", padx=(2, 5))
    
    def create_panels(self):
        """Создание основных панелей"""
        # Панель проектов
        self.project_panel = ProjectPanel(self.notebook, self.project_manager, self.on_project_selected)
        self.notebook.add(self.project_panel, text="📁 Проекты")
        
        # Панель редактора
        self.editor_panel = EditorPanel(self.notebook, self.on_file_changed, self.project_manager)
        self.notebook.add(self.editor_panel, text="📝 Редактор", state="disabled")
        
        # Панель медиа
        self.media_panel = MediaPanel(self.notebook)
        self.notebook.add(self.media_panel, text="🖼️ Медиа", state="disabled")
        
        # Панель генерации
        self.generation_panel = GenerationPanel(self.notebook, self.on_generation_started)
        self.notebook.add(self.generation_panel, text="🚀 Генерация", state="disabled")
        
        # Обработчик смены вкладок
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)
    
    def create_status_bar(self, parent):
        """Создание статусной строки"""
        status_frame = ttk.Frame(parent)
        status_frame.pack(fill="x", side="bottom")
        
        # Статусное сообщение
        self.status_var = tk.StringVar(value="Готов")
        self.status_label = ttk.Label(status_frame, textvariable=self.status_var)
        self.status_label.pack(side="left", padx=(5, 0))
        
        # Прогресс-бар
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            status_frame, 
            variable=self.progress_var, 
            length=200,
            mode="determinate"
        )
        self.progress_bar.pack(side="right", padx=(0, 5))
        
        # Разделитель
        ttk.Separator(status_frame, orient="horizontal").pack(fill="x", pady=1)
    
    def load_initial_data(self):
        """Загрузка начальных данных"""
        try:
            self.set_status("Загрузка проектов...")
            self.project_panel.refresh_projects()
            self.set_status("Готов")
        except Exception as e:
            logger.error(f"Ошибка загрузки начальных данных: {e}")
            self.set_status("Ошибка загрузки")
            messagebox.showerror("Ошибка", f"Не удалось загрузить данные:\n{e}")
    
    def on_project_selected(self, project):
        """Обработчик выбора проекта"""
        try:
            self.current_project = project
            
            # Обновляем информацию о проекте
            self.project_info_label.config(text=f"Проект: {project.name}")
            
            # Активируем вкладки
            for tab_id in range(1, self.notebook.index("end")):
                self.notebook.tab(tab_id, state="normal")
            
            # Загружаем данные проекта в панели
            self.editor_panel.load_project(project)
            self.media_panel.load_project(project)
            self.generation_panel.load_project(project)
            
            self.set_status(f"Загружен проект: {project.name}")
            logger.info(f"Выбран проект: {project.project_id}")
            
        except Exception as e:
            logger.error(f"Ошибка при выборе проекта: {e}")
            messagebox.showerror("Ошибка", f"Не удалось загрузить проект:\n{e}")
    
    def on_file_changed(self, file_type: str, file_path: str):
        """Обработчик изменения файла"""
        logger.info(f"Изменен файл {file_type}: {file_path}")
        # TODO: Реализовать автосохранение и отслеживание изменений
    
    def on_generation_started(self, generation_type: str, params: dict):
        """Обработчик начала генерации"""
        logger.info(f"Запущена генерация {generation_type} с параметрами: {params}")
        self.set_status(f"Генерация {generation_type}...")
        # TODO: Реализовать отслеживание прогресса генерации
    
    def on_tab_changed(self, event):
        """Обработчик смены вкладок"""
        try:
            current_tab = event.widget.index("current")
            tab_text = event.widget.tab(current_tab, "text")
            logger.debug(f"Переключение на вкладку: {tab_text}")
        except Exception:
            pass
    
    def set_status(self, message: str, progress: Optional[float] = None):
        """Установка статусного сообщения и прогресса"""
        self.status_var.set(message)
        if progress is not None:
            self.progress_var.set(progress)
        self.root.update_idletasks()
    
    # Обработчики меню
    def new_project(self):
        """Создание нового проекта"""
        self.notebook.select(0)
        self.project_panel.create_new_project()
    
    def open_project(self):
        """Открытие проекта"""
        self.notebook.select(0)  # Переключаемся на вкладку проектов
    
    def refresh_projects(self):
        """Обновление списка проектов"""
        self.project_panel.refresh_projects()
    
    def save_current(self):
        """Сохранение текущего файла"""
        if self.current_project:
            self.editor_panel.save_current_file()
    
    def run_full_pipeline(self):
        """Запуск полного pipeline"""
        if self.current_project:
            self.generation_panel.run_full_pipeline()
        else:
            messagebox.showwarning("Предупреждение", "Выберите проект для запуска pipeline")
    
    def run_from_step(self):
        """Запуск pipeline с определенного шага"""
        # TODO: Реализовать диалог выбора шага
        messagebox.showinfo("Информация", "Функция запуска с определенного шага будет реализована позже")
    
    def backup_project(self):
        """Создание backup проекта"""
        if self.current_project:
            try:
                backup_path = self.project_manager.backup_project(self.current_project.project_id)
                if backup_path:
                    messagebox.showinfo("Успех", f"Backup создан:\n{backup_path}")
                else:
                    messagebox.showerror("Ошибка", "Не удалось создать backup")
            except Exception as e:
                messagebox.showerror("Ошибка", f"Ошибка создания backup:\n{e}")
        else:
            messagebox.showwarning("Предупреждение", "Выберите проект для создания backup")

    def validate_project(self):
        """Валидация JSON файлов проекта"""
        if self.current_project:
            self.editor_panel.validate_current_project()
        else:
            messagebox.showwarning("Предупреждение", "Выберите проект для валидации")
    
    def clear_media_cache(self):
        """Очистка кэша медиа"""
        if self.current_project:
            self.media_panel.clear_cache()
            messagebox.showinfo("Успех", "Кэш медиа файлов очищен")
    
    def view_logs(self):
        """Просмотр логов"""
        # TODO: Реализовать просмотрщик логов
        messagebox.showinfo("Информация", "Просмотрщик логов будет реализован позже")
    
    def show_settings(self):
        """Показ настроек"""
        dialog = SettingsDialog(self.root, on_save=self.apply_settings)
        self.root.wait_window(dialog)

    def apply_settings(self):
        """Применение настроек без перезапуска приложения."""
        self.project_manager.projects_dir = app_settings.get_projects_directory()
        self.project_manager.backup_dir = app_settings.get_backup_directory()
        logging.getLogger().setLevel(app_settings.get("log_level", "INFO"))
        self.refresh_projects()
        self.set_status("Настройки применены")
    
    def show_user_guide(self):
        """Показ руководства пользователя"""
        messagebox.showinfo("Руководство пользователя", 
            "StoryBook Manager - интерфейс для управления проектами сказок\n\n"
            "Основные функции:\n"
            "• Просмотр и редактирование JSON файлов проекта\n"
            "• Просмотр изображений и видео\n"
            "• Запуск генерации контента\n"
            "• Создание backup'ов проектов\n\n"
            "Для начала работы выберите проект на вкладке 'Проекты'")
    
    def show_shortcuts(self):
        """Показ горячих клавиш"""
        shortcuts_text = """Горячие клавиши:

Ctrl+N - Новый проект
Ctrl+O - Открыть проект  
Ctrl+S - Сохранить текущий файл
F5 - Обновить список проектов

Ctrl+Z - Отменить
Ctrl+Y - Повторить
Ctrl+C - Копировать
Ctrl+V - Вставить"""
        
        messagebox.showinfo("Горячие клавиши", shortcuts_text)
    
    def show_about(self):
        """Показ информации о программе"""
        about_text = """StoryBook Manager v1.0

Приложение для управления проектами сказок,
созданными с помощью storybook_pipeline.

Возможности:
• Структурированное редактирование JSON
• Просмотр медиа файлов  
• Интеграция с workflow engine
• Валидация данных
• Создание backup'ов

Разработано для проекта MultiAgent"""
        
        messagebox.showinfo("О программе", about_text)
    
    def undo(self):
        """Отмена последнего действия"""
        self.editor_panel.undo()
    
    def redo(self):
        """Повтор последнего отмененного действия"""
        self.editor_panel.redo()
    
    def copy(self):
        """Копирование"""
        try:
            self.root.focus_get().event_generate("<<Copy>>")
        except Exception:
            pass
    
    def paste(self):
        """Вставка"""
        try:
            self.root.focus_get().event_generate("<<Paste>>")
        except Exception:
            pass
    
    def on_closing(self):
        """Обработчик закрытия окна"""
        try:
            # Сохраняем настройки окна
            app_settings.set("window_geometry", self.root.geometry())
            app_settings.save_settings()
            
            # Проверяем несохраненные изменения
            if self.editor_panel.has_unsaved_changes():
                result = messagebox.askyesnocancel(
                    "Сохранение изменений",
                    "У вас есть несохраненные изменения. Сохранить перед выходом?"
                )
                if result is True:
                    self.editor_panel.save_all_files()
                elif result is None:  # Cancel
                    return
            
            self.root.destroy()
            
        except Exception as e:
            logger.error(f"Ошибка при закрытии приложения: {e}")
            self.root.destroy()
