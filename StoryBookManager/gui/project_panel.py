"""
Панель управления проектами
==========================

Отображает список проектов, их превью и основные операции.
"""

import json
from pathlib import Path
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from typing import Any, Callable, Optional, List, Dict, Tuple
import logging
from datetime import datetime

from StoryBookManager.core.project_manager import ProjectManager, Project
from StoryBookManager.utils.scroll_utils import (
    bind_mousewheel_to_treeview, 
    bind_mousewheel_to_canvas_frame_ultimate
)

logger = logging.getLogger(__name__)


class ProjectPanel(ttk.Frame):
    """Панель для управления проектами"""
    
    def __init__(self, parent, project_manager: ProjectManager, on_project_selected: Callable,
                 on_projects_changed: Optional[Callable] = None):
        super().__init__(parent)

        self.project_manager = project_manager
        self.on_project_selected = on_project_selected
        self.on_projects_changed = on_projects_changed
        self.projects: List[Project] = []
        self.selected_project: Optional[Project] = None
        self.project_items: Dict[str, Project] = {}  # Словарь для связи item_id -> Project
        # Кэш размеров: project_path_str -> (dir_mtime, size_str)
        self._size_cache: Dict[str, Tuple[float, str]] = {}
        self._size_bytes_cache: Dict[str, int] = {}
        self._size_thread: Optional[threading.Thread] = None
        self._search_after_id: Optional[str] = None
        self._thumbnail_path: Optional[str] = None

        self.create_ui()
        self.refresh_projects()
    
    def create_ui(self):
        """Создание пользовательского интерфейса"""
        # Заголовок и кнопки управления
        header_frame = ttk.Frame(self)
        header_frame.pack(fill="x", padx=10, pady=(10, 5))
        
        ttk.Label(header_frame, text="Проекты сказок", style="Title.TLabel").pack(side="left")
        
        # Кнопки управления
        button_frame = ttk.Frame(header_frame)
        button_frame.pack(side="right")
        
        ttk.Button(button_frame, text="➕ Создать", command=self.create_new_project).pack(side="left", padx=2)
        ttk.Button(button_frame, text="🔄 Обновить", command=self.refresh_projects).pack(side="left", padx=2)
        ttk.Button(button_frame, text="📁 Открыть папку", command=self.open_projects_folder).pack(side="left", padx=2)
        ttk.Button(button_frame, text="🗑️ Удалить", command=self.delete_project).pack(side="left", padx=2)
        
        # Поиск и фильтры
        search_frame = ttk.Frame(self)
        search_frame.pack(fill="x", padx=10, pady=5)
        
        ttk.Label(search_frame, text="Поиск:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add('write', self.on_search_changed)
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=30)
        search_entry.pack(side="left", padx=(5, 10))
        
        ttk.Label(search_frame, text="Сортировка:").pack(side="left")
        self.sort_var = tk.StringVar(value="modified")
        self.sort_var.trace_add('write', self.on_sort_changed)
        sort_combo = ttk.Combobox(search_frame, textvariable=self.sort_var, width=15, state="readonly")
        sort_combo['values'] = ("modified", "created", "name", "size")
        sort_combo.pack(side="left", padx=5)
        
        # Разделитель
        ttk.Separator(self, orient="horizontal").pack(fill="x", pady=5)
        
        # Основная область с проектами
        main_frame = ttk.Frame(self)
        main_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        # Левая панель - список проектов
        left_frame = ttk.LabelFrame(main_frame, text="Список проектов", padding=5)
        left_frame.pack(side="left", fill="both", expand=True, padx=(0, 5))
        
        # Treeview для списка проектов
        self.create_projects_tree(left_frame)
        
        # Правая панель - превью проекта
        right_frame = ttk.LabelFrame(main_frame, text="Информация о проекте", padding=5)
        right_frame.pack(side="right", fill="y", padx=(5, 0))
        right_frame.config(width=300)
        
        self.create_project_preview(right_frame)
    
    def create_projects_tree(self, parent):
        """Создание дерева проектов"""
        # Фрейм для дерева и скроллбара
        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill="both", expand=True)
        
        # Определяем колонки
        columns = ("name", "pages", "modified", "size")
        self.projects_tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings")
        
        # Настройка колонок
        self.projects_tree.heading("#0", text="ID проекта", anchor="w")
        self.projects_tree.column("#0", width=120, minwidth=80)
        
        self.projects_tree.heading("name", text="Название", anchor="w")
        self.projects_tree.column("name", width=200, minwidth=150)
        
        self.projects_tree.heading("pages", text="Страниц", anchor="center")
        self.projects_tree.column("pages", width=70, minwidth=50)
        
        self.projects_tree.heading("modified", text="Изменен", anchor="center")
        self.projects_tree.column("modified", width=120, minwidth=100)
        
        self.projects_tree.heading("size", text="Размер", anchor="center")
        self.projects_tree.column("size", width=80, minwidth=60)
        
        # Скроллбары
        v_scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.projects_tree.yview)
        self.projects_tree.configure(yscrollcommand=v_scrollbar.set)
        
        h_scrollbar = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.projects_tree.xview)
        self.projects_tree.configure(xscrollcommand=h_scrollbar.set)
        
        # Размещение виджетов
        self.projects_tree.grid(row=0, column=0, sticky="nsew")
        v_scrollbar.grid(row=0, column=1, sticky="ns")
        h_scrollbar.grid(row=1, column=0, sticky="ew")
        
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        
        # Обработчики событий
        self.projects_tree.bind("<<TreeviewSelect>>", self.on_project_select)
        self.projects_tree.bind("<Double-1>", self.on_project_double_click)
        self.projects_tree.bind("<Button-3>", self.show_context_menu)  # Правый клик
        
        # Добавляем поддержку прокрутки колесом мыши
        bind_mousewheel_to_treeview(self.projects_tree)
    
    def create_project_preview(self, parent):
        """Создание панели превью проекта"""
        # Фрейм для превью с прокруткой
        canvas = tk.Canvas(parent, width=280)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        self.preview_frame = ttk.Frame(canvas)
        
        self.preview_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=self.preview_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # Добавляем поддержку прокрутки колесом мыши для панели превью (окончательная версия)
        bind_mousewheel_to_canvas_frame_ultimate(canvas, self.preview_frame)
        
        # Содержимое превью
        self.create_preview_content()
    
    def create_preview_content(self):
        """Создание содержимого панели превью"""
        # Название проекта
        self.preview_title = ttk.Label(self.preview_frame, text="Проект не выбран", 
                                      style="Subtitle.TLabel", wraplength=250)
        self.preview_title.pack(pady=(0, 10), anchor="w")
        
        # Thumbnail изображение
        self.thumbnail_frame = ttk.Frame(self.preview_frame, relief="solid", borderwidth=1)
        self.thumbnail_frame.pack(pady=5)
        self.thumbnail_frame.config(width=150, height=150)
        
        self.thumbnail_label = ttk.Label(self.thumbnail_frame, text="Нет изображения", 
                                        anchor="center")
        self.thumbnail_label.pack(expand=True, fill="both")
        
        # Информация о проекте
        info_frame = ttk.LabelFrame(self.preview_frame, text="Информация", padding=5)
        info_frame.pack(fill="x", pady=10)
        
        self.info_labels = {}
        info_fields = [
            ("ID проекта:", "project_id"),
            ("Описание:", "description"),
            ("Персонажи:", "characters_count"),
            ("Страниц:", "pages_count"),
            ("Создан:", "created_date"),
            ("Изменен:", "modified_date")
        ]
        
        for i, (label_text, field_name) in enumerate(info_fields):
            ttk.Label(info_frame, text=label_text, font=("Arial", 9, "bold")).grid(
                row=i, column=0, sticky="nw", padx=(0, 5), pady=2
            )
            self.info_labels[field_name] = ttk.Label(info_frame, text="-", 
                                                    wraplength=180, justify="left")
            self.info_labels[field_name].grid(row=i, column=1, sticky="nw", pady=2)
        
        info_frame.grid_columnconfigure(1, weight=1)
        
        # Статистика файлов
        stats_frame = ttk.LabelFrame(self.preview_frame, text="Файлы", padding=5)
        stats_frame.pack(fill="x", pady=5)
        
        self.stats_labels = {}
        stats_fields = [
            ("PDF:", "has_pdf"),
            ("Видео:", "has_video"),
            ("JSON файлы:", "json_count"),
            ("Изображения:", "images_count")
        ]
        
        for i, (label_text, field_name) in enumerate(stats_fields):
            ttk.Label(stats_frame, text=label_text, font=("Arial", 9, "bold")).grid(
                row=i, column=0, sticky="w", padx=(0, 5), pady=2
            )
            self.stats_labels[field_name] = ttk.Label(stats_frame, text="-")
            self.stats_labels[field_name].grid(row=i, column=1, sticky="w", pady=2)
        
        # Кнопки действий
        actions_frame = ttk.Frame(self.preview_frame)
        actions_frame.pack(fill="x", pady=10)
        
        ttk.Button(actions_frame, text="Открыть проект", 
                  command=self.open_selected_project).pack(fill="x", pady=2)
        ttk.Button(actions_frame, text="Создать backup",
                  command=self.backup_selected_project).pack(fill="x", pady=2)
        ttk.Button(actions_frame, text="Экспорт",
                  command=self.export_selected_project).pack(fill="x", pady=2)
        ttk.Button(actions_frame, text="Показать в папке",                  command=self.show_in_folder).pack(fill="x", pady=2)
    
    def refresh_projects(self):
        """Обновление списка проектов"""
        try:
            logger.info("Обновление списка проектов")
            
            # Очищаем текущий список
            for item in self.projects_tree.get_children():
                self.projects_tree.delete(item)
            
            # Загружаем проекты
            self.projects = self.project_manager.list_projects()
            
            # Заполняем дерево
            self.populate_projects_tree()
            
            # Обновляем превью
            self.update_preview(None)
            
            logger.info(f"Загружено {len(self.projects)} проектов")
            
        except Exception as e:
            logger.error(f"Ошибка обновления списка проектов: {e}")
            messagebox.showerror("Ошибка", f"Не удалось обновить список проектов:\n{e}")
    
    def populate_projects_tree(self):
        """Заполнение дерева проектов"""
        # Очищаем дерево и словарь
        for item in self.projects_tree.get_children():
            self.projects_tree.delete(item)
        self.project_items.clear()

        search_text = self.search_var.get().lower()

        for project in self.projects:
            # Фильтрация по поисковому запросу
            if search_text and search_text not in project.name.lower() and search_text not in project.project_id.lower():
                continue

            # Получаем информацию для отображения
            preview_info = project.get_preview_info()

            # Форматируем данные
            pages_count = preview_info.get("pages_count", 0)
            modified_date = preview_info.get("modified_date")
            modified_str = modified_date.strftime("%d.%m.%Y") if modified_date else "-"

            # Берём размер из кэша (если есть) — без блокирующего rglob
            cache_key = str(project.project_path)
            cached = self._size_cache.get(cache_key)
            size_str = cached[1] if cached is not None else "..."

            # Добавляем в дерево
            item_id = self.projects_tree.insert(
                "", "end",
                text=project.project_id,
                values=(project.name, pages_count, modified_str, size_str),
                tags=(project.project_id,)
            )

            self.project_items[item_id] = project

        # Запускаем фоновый расчёт размеров (обновит ячейки после завершения)
        self._load_sizes_in_background()
    
    def calculate_project_size(self, project: Project) -> str:
        """Вычисляет размер проекта с кэшированием по mtime директории"""
        path = project.project_path
        cache_key = str(path)
        try:
            current_mtime = path.stat().st_mtime if path.exists() else 0.0
        except OSError as e:
            logger.warning("Не удалось получить mtime для %s: %s", path, e)
            current_mtime = 0.0

        cached = self._size_cache.get(cache_key)
        if cached is not None and cached[0] == current_mtime:
            return cached[1]

        try:
            total_size = 0
            if path.exists():
                for file_path in path.rglob("*"):
                    if file_path.is_file():
                        total_size += file_path.stat().st_size

            if total_size < 1024:
                size_str = f"{total_size} B"
            elif total_size < 1024 * 1024:
                size_str = f"{total_size / 1024:.1f} KB"
            elif total_size < 1024 * 1024 * 1024:
                size_str = f"{total_size / (1024 * 1024):.1f} MB"
            else:
                size_str = f"{total_size / (1024 * 1024 * 1024):.1f} GB"

            self._size_cache[cache_key] = (current_mtime, size_str)
            self._size_bytes_cache[cache_key] = total_size
            return size_str
        except Exception as e:
            logger.warning("Ошибка вычисления размера проекта %s: %s", path, e)
            return "?"

    def _load_sizes_in_background(self):
        """Загружает размеры всех проектов в фоновом потоке и обновляет дерево.

        Если все проекты уже есть в _size_bytes_cache — поток не запускается
        и _apply_size_updates не планируется. Это разрывает цикл пересортировки:
        после первого прогона кэш заполнен, и повторный вызов populate_projects_tree
        (из on_sort_changed) не порождает нового потока.
        """
        if self._size_thread is not None and self._size_thread.is_alive():
            return

        projects_snapshot = list(self.projects)

        # Только проекты без кэшированного значения требуют фонового расчёта
        uncached = [p for p in projects_snapshot if str(p.project_path) not in self._size_bytes_cache]
        if not uncached:
            return

        def worker():
            updates: Dict[str, str] = {}
            for project in uncached:
                try:
                    size_str = self.calculate_project_size(project)
                    updates[project.project_id] = size_str
                except Exception as e:
                    logger.warning("Ошибка фонового расчёта размера %s: %s", project.project_id, e)

            self.after(0, lambda: self._apply_size_updates(updates))

        self._size_thread = threading.Thread(target=worker, daemon=True)
        self._size_thread.start()

    def _apply_size_updates(self, updates: Dict[str, str]):
        """Обновляет колонку размера в дереве после фоновой загрузки"""
        for item_id, project in list(self.project_items.items()):
            size_str = updates.get(project.project_id)
            if size_str is not None:
                try:
                    values = list(self.projects_tree.item(item_id, "values"))
                    if len(values) >= 4:
                        values[3] = size_str
                        self.projects_tree.item(item_id, values=values)
                except Exception as e:
                    logger.debug("Не удалось обновить размер в дереве для %s: %s", item_id, e)

        # Если текущая сортировка — по размеру, перестроить список с актуальными байтами
        if self.sort_var.get() == "size":
            self.on_sort_changed()
    
    def on_project_select(self, event):
        """Обработчик выбора проекта"""
        selection = self.projects_tree.selection()
        if selection:
            item_id = selection[0]
            project = self.get_project_from_item(item_id)
            if project:
                self.selected_project = project
                self.update_preview(project)
    
    def get_project_from_item(self, item_id: str) -> Optional[Project]:
        """Получает объект проекта из элемента дерева"""
        try:
            # Сначала пробуем получить из нашего словаря
            if item_id in self.project_items:
                return self.project_items[item_id]

            # Fallback: поиск по project_id
            project_id = self.projects_tree.item(item_id, "text")
            for project in self.projects:
                if project.project_id == project_id:
                    return project
        except Exception as e:
            logger.warning("Ошибка получения проекта из item_id=%s: %s", item_id, e)
        return None
    
    def update_preview(self, project: Optional[Project]):
        """Обновление панели превью"""
        if project is None:
            # Очищаем превью
            self.preview_title.config(text="Проект не выбран")
            self.thumbnail_label.config(text="Нет изображения", image="")
            
            for label in self.info_labels.values():
                label.config(text="-")
            for label in self.stats_labels.values():
                label.config(text="-")
            
            return
        
        try:
            # Получаем информацию о проекте
            preview_info = project.get_preview_info()
            structure = project.get_files_structure()
            
            # Обновляем заголовок
            self.preview_title.config(text=project.name)
            
            # Обновляем thumbnail
            self.update_thumbnail(preview_info.get("thumbnail"))
            
            # Обновляем информацию
            self.info_labels["project_id"].config(text=project.project_id)
            
            description = preview_info.get("description", "")
            if len(description) > 100:
                description = description[:97] + "..."
            self.info_labels["description"].config(text=description or "Нет описания")
            
            self.info_labels["characters_count"].config(text=str(preview_info.get("characters_count", 0)))
            self.info_labels["pages_count"].config(text=str(preview_info.get("pages_count", 0)))
            
            created_date = preview_info.get("created_date")
            created_str = created_date.strftime("%d.%m.%Y %H:%M") if created_date else "Неизвестно"
            self.info_labels["created_date"].config(text=created_str)
            
            modified_date = preview_info.get("modified_date")
            modified_str = modified_date.strftime("%d.%m.%Y %H:%M") if modified_date else "Неизвестно"
            self.info_labels["modified_date"].config(text=modified_str)
            
            # Обновляем статистику файлов
            self.stats_labels["has_pdf"].config(
                text="✓ Есть" if preview_info.get("has_pdf") else "✗ Нет"
            )
            self.stats_labels["has_video"].config(
                text="✓ Есть" if preview_info.get("has_video") else "✗ Нет"
            )
            
            # Подсчитываем JSON файлы
            json_files = structure.get("json_files", {})
            json_count = sum(1 for file_info in json_files.values() if file_info.get("exists"))
            self.stats_labels["json_count"].config(text=str(json_count))
            
            # Подсчитываем изображения
            media_dirs = structure.get("media_dirs", {})
            images_count = sum(dir_info.get("files_count", 0) for dir_info in media_dirs.values())
            self.stats_labels["images_count"].config(text=str(images_count))
            
        except Exception as e:
            logger.error(f"Ошибка обновления превью проекта: {e}")
    
    def update_thumbnail(self, thumbnail_path: Optional[str]):
        """Обновление thumbnail изображения (PIL-загрузка в фоновом потоке)"""
        self._thumbnail_path = thumbnail_path

        if not thumbnail_path or not thumbnail_path.endswith(('.png', '.jpg', '.jpeg')):
            self.thumbnail_label.config(text="Нет изображения", image="")
            return

        self.thumbnail_label.config(text="Загрузка...", image="")

        def _load():
            try:
                from PIL import Image, ImageTk  # type: ignore
                img = Image.open(thumbnail_path)
                img.thumbnail((150, 150))
                photo = ImageTk.PhotoImage(img)
            except ImportError:
                logger.debug("PIL не установлен — thumbnail не отображается")
                self.after(0, lambda: self.thumbnail_label.config(text="[Изображение]", image=""))
                return
            except Exception as e:
                logger.warning("Ошибка загрузки thumbnail %s: %s", thumbnail_path, e)
                self.after(0, lambda: self.thumbnail_label.config(text="Ошибка загрузки", image=""))
                return

            def _apply():
                # Guard: если пользователь переключился на другой проект — не применяем
                if self._thumbnail_path != thumbnail_path:
                    return
                self._thumbnail_photo = photo
                self.thumbnail_label.config(image=self._thumbnail_photo, text="")

            self.after(0, _apply)

        threading.Thread(target=_load, daemon=True).start()
    
    def on_project_double_click(self, event):
        """Обработчик двойного клика по проекту"""
        self.open_selected_project()
    
    def open_selected_project(self):
        """Открытие выбранного проекта"""
        if self.selected_project:
            self.on_project_selected(self.selected_project)
    
    def backup_selected_project(self):
        """Создание backup выбранного проекта (в фоновом потоке)"""
        if not self.selected_project:
            messagebox.showwarning("Предупреждение", "Выберите проект")
            return

        project_id = self.selected_project.project_id

        progress_dialog = tk.Toplevel(self)
        progress_dialog.title("Backup проекта")
        progress_dialog.geometry("300x100")
        progress_dialog.transient(self)
        progress_dialog.grab_set()
        ttk.Label(progress_dialog, text=f"Создание backup {project_id}...").pack(pady=20)
        progress_bar = ttk.Progressbar(progress_dialog, mode="indeterminate")
        progress_bar.pack(fill="x", padx=20, pady=5)
        progress_bar.start()

        def worker():
            try:
                backup_path = self.project_manager.backup_project(project_id)
            except Exception as e:
                logger.error("Ошибка создания backup %s: %s", project_id, e)
                self.after(0, lambda: (progress_dialog.destroy(),
                                       messagebox.showerror("Ошибка", f"Ошибка создания backup:\n{e}")))
                return

            def on_done():
                progress_dialog.destroy()
                if backup_path:
                    messagebox.showinfo("Успех", f"Backup создан:\n{backup_path}")
                else:
                    messagebox.showerror("Ошибка", "Не удалось создать backup")

            self.after(0, on_done)

        threading.Thread(target=worker, daemon=True).start()

    def export_selected_project(self):
        """Экспорт выбранного проекта в ZIP архив"""
        if not self.selected_project:
            messagebox.showwarning("Предупреждение", "Выберите проект")
            return
            
        project_id = self.selected_project.project_id
        date_str = datetime.now().strftime("%Y%m%d")
        default_filename = f"{project_id}_{date_str}.zip"
        
        file_path = filedialog.asksaveasfilename(
            title="Экспорт проекта",
            defaultextension=".zip",
            initialfile=default_filename,
            filetypes=[("ZIP archives", "*.zip"), ("All files", "*.*")]
        )
        
        if not file_path:
            return
            
        # Create progress dialog
        progress_dialog = tk.Toplevel(self)
        progress_dialog.title("Экспорт проекта")
        progress_dialog.geometry("300x150")
        progress_dialog.transient(self)
        progress_dialog.grab_set()
        
        ttk.Label(progress_dialog, text=f"Экспорт проекта {project_id}...").pack(pady=20)
        
        progress_var = tk.DoubleVar()
        progress_bar = ttk.Progressbar(progress_dialog, variable=progress_var, maximum=100)
        progress_bar.pack(fill="x", padx=20, pady=10)
        
        def update_progress(current, total):
            if total > 0:
                self.after(0, lambda: progress_var.set((current / total) * 100))

        def worker():
            try:
                success = self.project_manager.export_project(
                    project_id, file_path, progress_callback=update_progress
                )
            except Exception as e:
                logger.error("Ошибка экспорта %s: %s", project_id, e)
                self.after(0, lambda: (progress_dialog.destroy(),
                                       messagebox.showerror("Ошибка", f"Ошибка экспорта:\n{e}")))
                return

            def on_done():
                progress_dialog.destroy()
                if success:
                    messagebox.showinfo("Успех", f"Проект экспортирован:\n{file_path}")
                else:
                    messagebox.showerror("Ошибка", "Не удалось экспортировать проект")

            self.after(0, on_done)

        threading.Thread(target=worker, daemon=True).start()
    
    def show_in_folder(self):
        """Показать проект в папке"""
        if self.selected_project:
            try:
                import subprocess
                import sys
                
                path = str(self.selected_project.project_path)
                
                if sys.platform == "win32":
                    subprocess.run(["explorer", path])
                elif sys.platform == "darwin":
                    subprocess.run(["open", path])
                else:
                    subprocess.run(["xdg-open", path])
                    
            except Exception as e:
                logger.error(f"Ошибка открытия папки: {e}")
                messagebox.showerror("Ошибка", f"Не удалось открыть папку:\n{e}")
        else:
            messagebox.showwarning("Предупреждение", "Выберите проект")
    
    def open_projects_folder(self):
        """Открытие папки с проектами"""
        try:
            import subprocess
            import sys
            
            path = str(self.project_manager.projects_dir)
            
            if sys.platform == "win32":
                subprocess.run(["explorer", path])
            elif sys.platform == "darwin":
                subprocess.run(["open", path])
            else:
                subprocess.run(["xdg-open", path])
                
        except Exception as e:
            logger.error(f"Ошибка открытия папки проектов: {e}")
            messagebox.showerror("Ошибка", f"Не удалось открыть папку:\n{e}")
    
    def create_new_project(self):
        """Создание нового проекта"""
        logger.info("=" * 60)
        logger.info("🆕 Открытие диалога создания нового проекта")
        logger.info("=" * 60)
        dialog = NewProjectDialog(self, self.project_manager)
        if dialog.result:
            logger.info("✅ Диалог закрыт, получены данные проекта")
            title = dialog.result["title"]
            task_description = dialog.result["description"]
            genre = dialog.result["genre"]
            target_age = dialog.result["target_age"]
            pages_min = dialog.result["pages_min"]
            pages_max = dialog.result["pages_max"]
            words_per_page_min = dialog.result["words_per_page_min"]
            words_per_page_max = dialog.result["words_per_page_max"]
            language = dialog.result["language"]

            try:
                logger.info("🚀 Начало детерминированного создания проекта")
                logger.info(f"   Название: {title}")
                logger.info(f"   Жанр: {genre}; возраст: {target_age}")
                logger.info(f"   Описание: {task_description[:50]}...")
                logger.info(f"   Параметры: pages={pages_min}-{pages_max}, words={words_per_page_min}-{words_per_page_max}, lang={language}")
                project = self.project_manager.create_project(
                    title=title,
                    description=task_description,
                    genre=genre,
                    target_age=target_age,
                    language=language,
                    pages_min=pages_min,
                    pages_max=pages_max,
                    words_per_page_min=words_per_page_min,
                    words_per_page_max=words_per_page_max,
                    project_id_hint=dialog.result["project_id"],
                )
                project_id = project.project_id

                logger.info(f"✅ Проект создан: {project_id}")
                messagebox.showinfo("Успех", f"Проект '{title}' создан успешно!")
                
                # Обновляем список проектов
                logger.info("🔄 Обновление списка проектов...")
                self.refresh_projects()
                logger.info("✅ Список проектов обновлен")
                
                # Автоматически выбираем новый проект
                logger.info("🔍 Поиск созданного проекта в списке для автовыбора...")
                found = False
                for item in self.projects_tree.get_children():
                    item_project_id = self.projects_tree.item(item, "text")
                    if item_project_id == project_id:
                        logger.info("✅ Проект найден в списке, выбираем его")
                        self.projects_tree.selection_set(item)
                        self.projects_tree.focus(item)
                        self.on_project_select(None)
                        found = True
                        break
                
                if not found:
                    logger.warning(f"⚠️ Созданный проект '{project_id}' не найден в списке проектов")
                
                if self.on_projects_changed:
                    self.on_projects_changed()

                logger.info(f"✅✅✅ СОЗДАНИЕ ПРОЕКТА ПОЛНОСТЬЮ ЗАВЕРШЕНО: {project_id}")

            except Exception as e:
                logger.error(f"❌❌❌ КРИТИЧЕСКАЯ ОШИБКА при создании проекта: {e}")
                logger.exception("Полный traceback ошибки:")
                messagebox.showerror("Ошибка", f"Не удалось создать проект:\n{e}")
        else:
            logger.info("❌ Диалог создания проекта отменен пользователем")
    
    def delete_project(self):
        """Удаление выбранного проекта"""
        if not self.selected_project:
            messagebox.showwarning("Предупреждение", "Выберите проект для удаления")
            return

        project = self.selected_project
        # Кастомный диалог подтверждения — требует ввода имени проекта
        confirmed = _DeleteConfirmDialog(self, project.name).confirmed
        if not confirmed:
            return

        project_id = project.project_id

        progress_dialog = tk.Toplevel(self)
        progress_dialog.title("Удаление проекта")
        progress_dialog.geometry("300x100")
        progress_dialog.transient(self)
        progress_dialog.grab_set()
        ttk.Label(progress_dialog, text=f"Удаление {project_id}...").pack(pady=20)
        progress_bar = ttk.Progressbar(progress_dialog, mode="indeterminate")
        progress_bar.pack(fill="x", padx=20, pady=5)
        progress_bar.start()

        def worker():
            try:
                success = self.project_manager.delete_project(project_id, create_backup=True)
            except Exception as e:
                logger.error("Ошибка удаления проекта %s: %s", project_id, e)
                self.after(0, lambda: (progress_dialog.destroy(),
                                       messagebox.showerror("Ошибка", f"Ошибка удаления проекта:\n{e}")))
                return

            def on_done():
                progress_dialog.destroy()
                if success:
                    messagebox.showinfo("Успех", "Проект удален (backup создан)")
                    self.refresh_projects()
                    if self.on_projects_changed:
                        self.on_projects_changed()
                else:
                    messagebox.showerror("Ошибка", "Не удалось удалить проект")

            self.after(0, on_done)

        threading.Thread(target=worker, daemon=True).start()
    
    def show_context_menu(self, event):
        """Показ контекстного меню"""
        # Определяем элемент под курсором
        item = self.projects_tree.identify_row(event.y)
        if item:
            self.projects_tree.selection_set(item)
            self.on_project_select(None)
            
            # Создаем контекстное меню
            context_menu = tk.Menu(self, tearoff=0)
            context_menu.add_command(label="Открыть проект", command=self.open_selected_project)
            context_menu.add_separator()
            context_menu.add_command(label="Создать backup", command=self.backup_selected_project)
            context_menu.add_command(label="Экспорт", command=self.export_selected_project)
            context_menu.add_command(label="Показать в папке", command=self.show_in_folder)
            context_menu.add_separator()
            context_menu.add_command(label="Удалить проект", command=self.delete_project)
            
            # Показываем меню
            try:
                context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                context_menu.grab_release()
    
    def on_search_changed(self, *args):
        """Обработчик изменения поискового запроса — дебаунс 300 мс"""
        if self._search_after_id is not None:
            self.after_cancel(self._search_after_id)
        self._search_after_id = self.after(300, self._apply_search)

    def _apply_search(self):
        """Применяет текущий фильтр поиска — перерисовывает дерево без rglob"""
        self._search_after_id = None
        self.populate_projects_tree()

    def on_sort_changed(self, *args):
        """Обработчик изменения сортировки"""
        sort_by = self.sort_var.get()

        if sort_by == "name":
            self.projects.sort(key=lambda p: p.name.lower())
        elif sort_by == "created":
            self.projects.sort(key=lambda p: p.created_date or datetime.min, reverse=True)
        elif sort_by == "modified":
            self.projects.sort(key=lambda p: p.modified_date or datetime.min, reverse=True)
        elif sort_by == "size":
            # Сортировка по кэшированным байтам; проекты без кэша идут в конец (0)
            def _cached_size_bytes(p: Project) -> int:
                return self._size_bytes_cache.get(str(p.project_path), 0)

            self.projects.sort(key=_cached_size_bytes, reverse=True)

        self.populate_projects_tree()
    
class _DeleteConfirmDialog:
    """Диалог подтверждения удаления проекта — требует ввода имени проекта"""

    def __init__(self, parent, project_name: str):
        self.confirmed = False
        dialog = tk.Toplevel(parent)
        dialog.title("Подтверждение удаления")
        dialog.geometry("420x200")
        dialog.transient(parent)
        dialog.grab_set()
        dialog.resizable(False, False)

        ttk.Label(
            dialog,
            text=f"Вы собираетесь удалить проект:\n«{project_name}»\n\nПроект будет перемещён в backup.",
            wraplength=380, justify="center"
        ).pack(pady=(15, 10))

        ttk.Label(dialog, text="Введите название проекта для подтверждения:").pack()
        entry_var = tk.StringVar()
        entry = ttk.Entry(dialog, textvariable=entry_var, width=35)
        entry.pack(pady=(5, 10))
        entry.focus_set()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack()

        delete_btn = ttk.Button(
            btn_frame, text="Удалить", state="disabled",
            command=lambda: self._confirm(dialog)
        )
        delete_btn.pack(side="left", padx=(0, 10))
        ttk.Button(btn_frame, text="Отмена", command=dialog.destroy).pack(side="left")

        def on_entry_changed(*_):
            if entry_var.get().strip() == project_name:
                delete_btn.config(state="normal")
            else:
                delete_btn.config(state="disabled")

        entry_var.trace_add("write", on_entry_changed)
        dialog.bind("<Return>", lambda e: self._confirm(dialog) if delete_btn["state"] == "normal" else None)
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        dialog.wait_window()

    def _confirm(self, dialog: tk.Toplevel):
        self.confirmed = True
        dialog.destroy()


class NewProjectDialog:
    """Диалог создания нового проекта"""
    
    def __init__(self, parent, project_manager: ProjectManager):
        self.project_manager = project_manager
        self.result = None
        self.field_config = self._load_field_config()
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Создать новый проект")
        self.dialog.geometry("600x650")
        self.dialog.resizable(True, True)
        self.dialog.grab_set()  # Модальный режим
        
        # Центрируем диалог относительно родительского окна
        self.dialog.transient(parent)
        self.center_dialog(parent)
        
        self.create_ui()
        
        # Ожидаем закрытия диалога
        self.dialog.wait_window()

    def _load_field_config(self) -> Dict[str, Any]:
        """Загружает значения полей brief из ui_config.json."""
        config_path = Path(__file__).resolve().parents[1] / "config" / "ui_config.json"
        with open(config_path, "r", encoding="utf-8") as f:
            ui_config = json.load(f)

        field_config = ui_config["brief"]["field_config"]
        return {
            "genre_values": tuple(field_config["genre"]["values"]),
            "target_age_values": tuple(field_config["target_age"]["values"]),
        }
    
    def center_dialog(self, parent):
        """Центрирование диалога относительно родительского окна"""
        parent_x = parent.winfo_rootx()
        parent_y = parent.winfo_rooty()
        parent_width = parent.winfo_width()
        parent_height = parent.winfo_height()
        
        dialog_width = 600
        dialog_height = 650
        
        x = parent_x + (parent_width - dialog_width) // 2
        y = parent_y + (parent_height - dialog_height) // 2
        
        self.dialog.geometry(f"{dialog_width}x{dialog_height}+{x}+{y}")
    
    def create_ui(self):
        """Создание пользовательского интерфейса диалога"""
        # Создаем основной контейнер
        main_container = ttk.Frame(self.dialog)
        main_container.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Создаем Canvas с прокруткой для содержимого
        canvas = tk.Canvas(main_container)
        scrollbar = ttk.Scrollbar(main_container, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # Заголовок
        title_frame = ttk.Frame(scrollable_frame)
        title_frame.pack(fill="x", pady=(0, 15))
        
        title_label = ttk.Label(
            title_frame,
            text="Создание нового проекта",
            font=("TkDefaultFont", 12, "bold")
        )
        title_label.pack()
        
        # Основная форма
        form_frame = ttk.LabelFrame(scrollable_frame, text="Параметры проекта", padding=15)
        form_frame.pack(fill="x", pady=(0, 15))
        
        # Название проекта
        ttk.Label(form_frame, text="Название:").grid(row=0, column=0, sticky="w", pady=(0, 5))
        self.title_var = tk.StringVar()
        title_entry = ttk.Entry(form_frame, textvariable=self.title_var, width=30)
        title_entry.grid(row=0, column=1, sticky="ew", pady=(0, 5), padx=(10, 0))
        title_entry.focus_set()

        # ID проекта
        ttk.Label(form_frame, text="ID проекта (необязательно):").grid(row=1, column=0, sticky="w", pady=(0, 5))
        self.project_id_var = tk.StringVar()
        project_id_entry = ttk.Entry(form_frame, textvariable=self.project_id_var, width=30)
        project_id_entry.grid(row=1, column=1, sticky="ew", pady=(0, 5), padx=(10, 0))
        
        # Подсказка для ID
        hint_label = ttk.Label(
            form_frame,
            text="Оставьте пустым для автоматической генерации уникального ID",
            foreground="gray"
        )
        hint_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 15))

        # Жанр
        ttk.Label(form_frame, text="Жанр:").grid(row=3, column=0, sticky="w", pady=(0, 5))
        self.genre_var = tk.StringVar(value=self.field_config["genre_values"][0])
        genre_combo = ttk.Combobox(
            form_frame,
            textvariable=self.genre_var,
            values=self.field_config["genre_values"],
            state="readonly",
            width=27,
        )
        genre_combo.grid(row=3, column=1, sticky="w", pady=(0, 5), padx=(10, 0))

        # Возраст
        ttk.Label(form_frame, text="Возраст:").grid(row=4, column=0, sticky="w", pady=(0, 5))
        self.target_age_var = tk.StringVar(value=self.field_config["target_age_values"][0])
        target_age_combo = ttk.Combobox(
            form_frame,
            textvariable=self.target_age_var,
            values=self.field_config["target_age_values"],
            state="readonly",
            width=27,
        )
        target_age_combo.grid(row=4, column=1, sticky="w", pady=(0, 15), padx=(10, 0))
        
        # Описание задачи
        ttk.Label(form_frame, text="Описание:").grid(row=5, column=0, sticky="nw", pady=(0, 5))
        
        # Текстовое поле с прокруткой
        text_container = ttk.Frame(form_frame)
        text_container.grid(row=5, column=1, sticky="ew", pady=(0, 15), padx=(10, 0))
        
        self.description_text = tk.Text(text_container, width=40, height=5, wrap="word")
        text_scrollbar = ttk.Scrollbar(text_container, orient="vertical", command=self.description_text.yview)
        self.description_text.configure(yscrollcommand=text_scrollbar.set)
        
        self.description_text.pack(side="left", fill="both", expand=True)
        text_scrollbar.pack(side="right", fill="y")
        
        # Дополнительные параметры
        ttk.Label(form_frame, text="Параметры книги:", font=("TkDefaultFont", 9, "bold")).grid(row=6, column=0, columnspan=2, sticky="w", pady=(15, 5))
        
        # Строка с количеством страниц
        pages_frame = ttk.Frame(form_frame)
        pages_frame.grid(row=7, column=0, columnspan=2, sticky="ew", pady=2)
        
        ttk.Label(pages_frame, text="Страниц:").pack(side="left")
        self.pages_min_var = tk.StringVar(value="9")
        ttk.Entry(pages_frame, textvariable=self.pages_min_var, width=5).pack(side="left", padx=(5, 2))
        ttk.Label(pages_frame, text="—").pack(side="left", padx=2)
        self.pages_max_var = tk.StringVar(value="12")
        ttk.Entry(pages_frame, textvariable=self.pages_max_var, width=5).pack(side="left", padx=(2, 10))
        
        # Строка со словами на страницу
        words_frame = ttk.Frame(form_frame)
        words_frame.grid(row=8, column=0, columnspan=2, sticky="ew", pady=2)
        
        ttk.Label(words_frame, text="Слов на страницу:").pack(side="left")
        self.words_min_var = tk.StringVar(value="400")
        ttk.Entry(words_frame, textvariable=self.words_min_var, width=5).pack(side="left", padx=(5, 2))
        ttk.Label(words_frame, text="—").pack(side="left", padx=2)
        self.words_max_var = tk.StringVar(value="450")
        ttk.Entry(words_frame, textvariable=self.words_max_var, width=5).pack(side="left", padx=(2, 10))
        
        # Язык
        language_frame = ttk.Frame(form_frame)
        language_frame.grid(row=9, column=0, columnspan=2, sticky="ew", pady=2)
        
        ttk.Label(language_frame, text="Язык:").pack(side="left")
        self.language_var = tk.StringVar(value="ru")
        language_combo = ttk.Combobox(language_frame, textvariable=self.language_var, width=10, state="readonly")
        language_combo['values'] = ("ru", "en", "es", "fr", "de")
        language_combo.pack(side="left", padx=(5, 0))
        
        # Настройка колонок формы
        form_frame.grid_columnconfigure(1, weight=1)
        
        # Примеры в более компактном виде
        examples_frame = ttk.LabelFrame(scrollable_frame, text="Примеры (нажмите для выбора)", padding=10)
        examples_frame.pack(fill="x", pady=(0, 15))
        
        examples = [
            "Сказка про храброго котенка, который спасает деревню от злого дракона",
            "История о маленькой принцессе, которая учится дружить с лесными животными",
            "Приключения умного зайчика в волшебном саду"
        ]
        
        for i, example in enumerate(examples):
            btn = ttk.Button(
                examples_frame, 
                text=f"💡 {example[:45]}..." if len(example) > 45 else f"💡 {example}",
                command=lambda e=example: self.set_example(e)
            )
            btn.pack(fill="x", pady=1)
        
        # Упаковываем Canvas и Scrollbar
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # Кнопки внизу - ВАЖНО: фиксированное расположение ВНЕ прокручиваемой области
        button_container = ttk.Frame(self.dialog)
        button_container.pack(fill="x", side="bottom", padx=10, pady=(0, 10))
        
        # Разделитель
        separator = ttk.Separator(button_container, orient="horizontal")
        separator.pack(fill="x", pady=(0, 15))
        
        # Фрейм для кнопок
        button_frame = ttk.Frame(button_container)
        button_frame.pack(fill="x")
        
        # Кнопки
        cancel_btn = ttk.Button(button_frame, text="Отмена", command=self.cancel)
        cancel_btn.pack(side="right", padx=(10, 0))
        
        self.create_button = ttk.Button(button_frame, text="Создать", command=self.create, state="disabled")
        self.create_button.pack(side="right")
        
        # Статус валидации
        self.status_label = ttk.Label(button_frame, text="Заполните все поля", foreground="orange")
        self.status_label.pack(side="left")
        
        # Валидация в реальном времени
        self.title_var.trace_add('write', self.validate_form)
        self.project_id_var.trace_add('write', self.validate_project_id)
        self.genre_var.trace_add('write', self.validate_form)
        self.target_age_var.trace_add('write', self.validate_form)
        self.description_text.bind('<KeyRelease>', self.validate_description)
        
        # Привязываем горячие клавиши
        self.dialog.bind('<Return>', lambda e: self.create_if_valid())
        self.dialog.bind('<Escape>', lambda e: self.cancel())
        
        # Добавляем поддержку прокрутки колесом мыши (окончательная версия)
        bind_mousewheel_to_canvas_frame_ultimate(canvas, scrollable_frame)
        
        # Фокус на canvas для обработки событий клавиатуры
        canvas.focus_set()
        
        # Начальная валидация
        self.validate_form()
    
    def set_example(self, example_text):
        """Установка примера в поле описания"""
        self.description_text.delete(1.0, tk.END)
        self.description_text.insert(1.0, example_text)
        self.validate_form()
    
    def validate_project_id(self, *args):
        """Валидация ID проекта"""
        project_id = self.project_id_var.get()
        
        # Проверка на допустимые символы
        import re
        if not re.match(r'^[a-zA-Z0-9_]*$', project_id):
            self.project_id_var.set(re.sub(r'[^a-zA-Z0-9_]', '', project_id))
        
        self.validate_form()
    
    def validate_description(self, *args):
        """Валидация описания"""
        self.validate_form()
    
    def validate_form(self):
        """Общая валидация формы"""
        title = self.title_var.get().strip()
        description = self.description_text.get(1.0, tk.END).strip()
        
        # Проверяем валидность
        title_valid = len(title) >= 2
        desc_valid = len(description) >= 10
        
        # Обновляем кнопку и статус
        if hasattr(self, 'create_button') and hasattr(self, 'status_label'):
            if title_valid and desc_valid:
                self.create_button.config(state="normal")
                self.status_label.config(text="Готово к созданию ✅", foreground="green")
            elif not title_valid and not desc_valid:
                self.create_button.config(state="disabled")
                self.status_label.config(text="Введите название и описание", foreground="orange")
            elif not title_valid:
                self.create_button.config(state="disabled")
                self.status_label.config(text="Название должно содержать минимум 2 символа", foreground="orange")
            elif not desc_valid:
                self.create_button.config(state="disabled")
                self.status_label.config(text="Описание должно содержать минимум 10 символов", foreground="orange")
    
    def create_if_valid(self):
        """Создание проекта только если данные валидны"""
        if hasattr(self, 'create_button') and self.create_button['state'] == 'normal':
            self.create()
    
    def create(self):
        """Создание проекта"""
        title = self.title_var.get().strip()
        description = self.description_text.get(1.0, tk.END).strip()
        genre = self.genre_var.get().strip()
        target_age = self.target_age_var.get().strip()

        if len(title) < 2:
            messagebox.showerror("Ошибка", "Название проекта должно содержать минимум 2 символа")
            return
        
        if len(description) < 10:
            messagebox.showerror("Ошибка", "Описание должно содержать минимум 10 символов")
            return
        
        if not genre:
            messagebox.showerror("Ошибка", "Выберите жанр проекта")
            return

        if not target_age:
            messagebox.showerror("Ошибка", "Выберите возрастную категорию")
            return

        # Валидация числовых параметров
        try:
            pages_min = int(self.pages_min_var.get())
            pages_max = int(self.pages_max_var.get())
            words_min = int(self.words_min_var.get())
            words_max = int(self.words_max_var.get())
            
            if pages_min < 1 or pages_max < pages_min:
                messagebox.showerror("Ошибка", "Некорректное количество страниц")
                return
            
            if words_min < 50 or words_max < words_min:
                messagebox.showerror("Ошибка", "Некорректное количество слов на страницу")
                return
                
        except ValueError:
            messagebox.showerror("Ошибка", "Числовые параметры должны быть целыми числами")
            return

        project_id = self.project_manager.generate_project_id(self.project_id_var.get().strip() or title)
        
        self.result = {
            "title": title,
            "project_id": project_id,
            "description": description,
            "genre": genre,
            "target_age": target_age,
            "pages_min": pages_min,
            "pages_max": pages_max,
            "words_per_page_min": words_min,
            "words_per_page_max": words_max,
            "language": self.language_var.get()
        }
        
        self.dialog.destroy()
    
    def cancel(self):
        """Отмена создания проекта"""
        self.dialog.destroy()
