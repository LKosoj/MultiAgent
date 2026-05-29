"""
Панель просмотра медиа файлов
===========================

Просмотр изображений и видео файлов проекта.
"""

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional, List, Dict, Any
import logging
from pathlib import Path
import sys
import threading
import time
import uuid
import shutil
from datetime import datetime
import concurrent.futures
try:
    from utils import log_smolagents_panel
except ImportError:
    _fallback_logger = logging.getLogger(__name__)

    def log_smolagents_panel(content, title="", **kwargs):
        _fallback_logger.info(f"[{title}] {content}")

try:
    import cv2
    HAS_OPENCV = True
except ImportError:
    HAS_OPENCV = False
    cv2 = None

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    Image = None
    ImageTk = None

from StoryBookManager.core.project_manager import Project
from StoryBookManager.core.media_processor import MediaProcessor
from StoryBookManager.utils.scroll_utils import (
    bind_mousewheel_to_treeview,
    bind_mousewheel_to_text_with_scrollbar,
    bind_mousewheel_to_canvas_frame,
    bind_mousewheel_to_canvas_frame_advanced,
    bind_mousewheel_to_canvas_frame_ultimate
)

logger = logging.getLogger(__name__)


class MediaPanel(ttk.Frame):
    """Панель для просмотра медиа файлов"""
    
    def __init__(self, parent):
        super().__init__(parent)
        
        self.current_project: Optional[Project] = None
        self.media_processor: Optional[MediaProcessor] = None
        self.current_image_path: Optional[str] = None
        self.current_video_path: Optional[str] = None
        
        # Переменные для встроенного видеоплеера
        self.video_capture = None
        self.is_playing = False
        self.is_paused = False
        self.video_thread = None
        self.fps = 30  # По умолчанию
        
        self.create_ui()
    
    def create_ui(self):
        """Создание пользовательского интерфейса"""
        # Заголовок
        header_frame = ttk.Frame(self)
        header_frame.pack(fill="x", padx=10, pady=(10, 5))
        
        ttk.Label(header_frame, text="Медиа файлы проекта", style="Title.TLabel").pack(side="left")
        
        # Кнопки управления
        button_frame = ttk.Frame(header_frame)
        button_frame.pack(side="right")
        
        ttk.Button(button_frame, text="🔄 Обновить", command=self.refresh_media).pack(side="left", padx=2)
        ttk.Button(button_frame, text="📁 Открыть папку", command=self.open_media_folder).pack(side="left", padx=2)
        ttk.Button(button_frame, text="🗑️ Очистить кэш", command=self.clear_cache).pack(side="left", padx=2)
        
        # Разделитель
        ttk.Separator(self, orient="horizontal").pack(fill="x", pady=5)
        
        # Основная область
        main_frame = ttk.Frame(self)
        main_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        # Левая панель - список файлов
        left_frame = ttk.LabelFrame(main_frame, text="Медиа файлы", padding=5)
        left_frame.pack(side="left", fill="both", expand=True, padx=(0, 5))
        
        self.create_media_tree(left_frame)
        
        # Правая панель - просмотр
        right_frame = ttk.LabelFrame(main_frame, text="Просмотр", padding=5)
        right_frame.pack(side="right", fill="both", padx=(5, 0))
        right_frame.config(width=400)
        
        self.create_media_viewer(right_frame)
    
    def create_media_tree(self, parent):
        """Создание дерева медиа файлов"""
        # Фильтры
        filter_frame = ttk.Frame(parent)
        filter_frame.pack(fill="x", pady=(0, 5))
        
        ttk.Label(filter_frame, text="Тип:").pack(side="left")
        self.media_filter = tk.StringVar(value="all")
        self.media_filter.trace_add('write', self.on_filter_changed)
        filter_combo = ttk.Combobox(filter_frame, textvariable=self.media_filter, width=15, state="readonly")
        filter_combo['values'] = ("all", "illustrations", "references", "shots", "videos")
        filter_combo.pack(side="left", padx=5)
        
        # Дерево файлов
        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill="both", expand=True)
        
        columns = ("type", "category", "size", "modified")
        self.media_tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings")
        
        # Настройка колонок
        self.media_tree.heading("#0", text="Файл", anchor="w")
        self.media_tree.column("#0", width=200, minwidth=150)
        
        self.media_tree.heading("type", text="Тип", anchor="w")
        self.media_tree.column("type", width=100, minwidth=80)
        
        self.media_tree.heading("category", text="Категория", anchor="w")
        self.media_tree.column("category", width=120, minwidth=100)
        
        self.media_tree.heading("size", text="Размер", anchor="center")
        self.media_tree.column("size", width=80, minwidth=60)
        
        self.media_tree.heading("modified", text="Изменен", anchor="center")
        self.media_tree.column("modified", width=100, minwidth=80)
        
        # Скроллбары
        v_scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.media_tree.yview)
        self.media_tree.configure(yscrollcommand=v_scrollbar.set)
        
        h_scrollbar = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.media_tree.xview)
        self.media_tree.configure(xscrollcommand=h_scrollbar.set)
        
        # Размещение
        self.media_tree.grid(row=0, column=0, sticky="nsew")
        v_scrollbar.grid(row=0, column=1, sticky="ns")
        h_scrollbar.grid(row=1, column=0, sticky="ew")
        
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        
        # Обработчики событий
        self.media_tree.bind("<<TreeviewSelect>>", self.on_media_select)
        self.media_tree.bind("<Double-1>", self.on_media_double_click)
        self.media_tree.bind("<Button-3>", self.show_media_context_menu)
        
        # Добавляем поддержку прокрутки колесом мыши для дерева медиа файлов
        bind_mousewheel_to_treeview(self.media_tree)
    
    def create_media_viewer(self, parent):
        """Создание области просмотра медиа"""
        # Информация о файле
        info_frame = ttk.LabelFrame(parent, text="Информация о файле", padding=5)
        info_frame.pack(fill="x", pady=(0, 10))
        
        self.file_info_text = tk.Text(info_frame, height=4, wrap=tk.WORD, font=("Courier", 9))
        info_scrollbar = ttk.Scrollbar(info_frame, command=self.file_info_text.yview)
        self.file_info_text.configure(yscrollcommand=info_scrollbar.set)
        
        self.file_info_text.pack(side="left", fill="both", expand=True)
        info_scrollbar.pack(side="right", fill="y")
        
        # Добавляем поддержку прокрутки колесом мыши для информации о файле
        bind_mousewheel_to_text_with_scrollbar(self.file_info_text, info_scrollbar)
        
        # Область просмотра
        viewer_frame = ttk.LabelFrame(parent, text="Просмотр (кликните для полного размера)", padding=5)
        viewer_frame.pack(fill="both", expand=True)
        
        # Canvas для изображений
        self.image_canvas = tk.Canvas(viewer_frame, bg="white", relief="sunken", borderwidth=1, cursor="hand2")
        self.image_canvas.pack(fill="both", expand=True)
        
        # Привязываем клик для полноразмерного просмотра изображений
        self.image_canvas.bind("<Button-1>", self.on_image_click)
        
        # Скроллбары для canvas
        canvas_v_scroll = ttk.Scrollbar(viewer_frame, orient="vertical", command=self.image_canvas.yview)
        canvas_h_scroll = ttk.Scrollbar(viewer_frame, orient="horizontal", command=self.image_canvas.xview)
        self.image_canvas.configure(yscrollcommand=canvas_v_scroll.set, xscrollcommand=canvas_h_scroll.set)
        
        # Панель управления просмотром
        control_frame = ttk.Frame(viewer_frame)
        control_frame.pack(fill="x", pady=(5, 0))
        
        ttk.Button(control_frame, text="🔍 Увеличить", command=self.zoom_in).pack(side="left", padx=2)
        ttk.Button(control_frame, text="🔍 Уменьшить", command=self.zoom_out).pack(side="left", padx=2)
        ttk.Button(control_frame, text="📐 По размеру", command=self.fit_to_window).pack(side="left", padx=2)
        
        # Кнопка редактирования изображений
        self.edit_image_btn = ttk.Button(control_frame, text="🖌️ Редактировать", command=self.edit_image)
        self.edit_image_btn.pack(side="left", padx=5)
        self.edit_image_btn.pack_forget()  # Скрываем по умолчанию
        
        # Кнопки управления видео
        self.play_video_btn = ttk.Button(control_frame, text="▶️ Воспроизвести", command=self.play_video_embedded)
        self.play_video_btn.pack(side="left", padx=5)
        self.play_video_btn.pack_forget()  # Скрываем по умолчанию
        
        self.pause_video_btn = ttk.Button(control_frame, text="⏸️ Пауза", command=self.pause_video)
        self.pause_video_btn.pack(side="left", padx=2)
        self.pause_video_btn.pack_forget()  # Скрываем по умолчанию
        
        self.stop_video_btn = ttk.Button(control_frame, text="⏹️ Стоп", command=self.stop_video)
        self.stop_video_btn.pack(side="left", padx=2)
        self.stop_video_btn.pack_forget()  # Скрываем по умолчанию
        
        # Разделитель
        ttk.Separator(control_frame, orient="vertical").pack(side="left", padx=10, fill="y")
        
        # Кнопка внешнего плеера
        ttk.Button(control_frame, text="🎬 Внешний плеер", command=self.play_video_external).pack(side="left", padx=2)
        
        ttk.Button(control_frame, text="📁 Открыть в системе", command=self.open_in_system).pack(side="right", padx=2)
        
        # Метка для пустого состояния
        self.empty_label = ttk.Label(self.image_canvas, text="Выберите медиа файл для просмотра", 
                                    anchor="center")
        self.image_canvas.create_window(200, 150, window=self.empty_label)
        
        # Переменные для зума
        self.zoom_factor = 1.0
        self.current_photo = None
    
    def load_project(self, project: Project):
        """Загрузка проекта"""
        try:
            self.current_project = project
            self.media_processor = MediaProcessor(project.project_id)
            
            # Очистка
            self.clear_viewer()
            
            # Загрузка медиа файлов
            self.refresh_media()
            
            logger.info(f"Проект {project.project_id} загружен в медиа панель")
            
        except Exception as e:
            logger.error(f"Ошибка загрузки проекта в медиа панель: {e}")
            messagebox.showerror("Ошибка", f"Не удалось загрузить проект:\n{e}")
    
    def refresh_media(self):
        """Обновление списка медиа файлов"""
        if not self.media_processor:
            return
        
        try:
            # Очищаем дерево
            for item in self.media_tree.get_children():
                self.media_tree.delete(item)
            
            # Загружаем изображения
            images = self.media_processor.get_project_images()
            for image in images:
                self.add_media_item(image, "image")
            
            # Загружаем видео
            videos = self.media_processor.get_project_videos()
            for video in videos:
                self.add_media_item(video, "video")
            
            logger.info(f"Загружено {len(images)} изображений и {len(videos)} видео")
            
        except Exception as e:
            logger.error(f"Ошибка обновления медиа файлов: {e}")
            messagebox.showerror("Ошибка", f"Не удалось обновить медиа файлы:\n{e}")
    
    def add_media_item(self, media_info: Dict[str, Any], media_type: str):
        """Добавление медиа элемента в дерево"""
        try:
            file_path = Path(media_info["path"])
            file_name = file_path.name
            
            # Форматируем размер файла
            size = media_info.get("size", 0)
            size_str = self.format_file_size(size)
            
            # Форматируем дату изменения
            import datetime
            modified = media_info.get("modified", 0)
            modified_str = datetime.datetime.fromtimestamp(modified).strftime("%d.%m.%Y")
            
            # Определяем тип и категорию
            item_type = media_info.get("type", media_type)
            category = media_info.get("category", "unknown")
            
            # Добавляем в дерево
            self.media_tree.insert(
                "", "end",
                text=file_name,
                values=(item_type, category, size_str, modified_str),
                tags=(media_info["path"],)
            )
            
        except Exception as e:
            logger.error(f"Ошибка добавления медиа элемента: {e}")
    
    def format_file_size(self, size_bytes: int) -> str:
        """Форматирование размера файла"""
        if size_bytes == 0:
            return "0 B"
        
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        
        return f"{size_bytes:.1f} TB"
    
    def on_filter_changed(self, *args):
        """Обработчик изменения фильтра"""
        # TODO: Реализовать фильтрацию медиа файлов
        pass
    
    def on_media_select(self, event):
        """Обработчик выбора медиа файла"""
        selection = self.media_tree.selection()
        if selection:
            item_id = selection[0]
            file_path = self.media_tree.item(item_id, "tags")[0]
            self.display_media(file_path)
    
    def display_media(self, file_path: str):
        """Отображение медиа файла"""
        try:
            self.current_image_path = file_path
            file_path_obj = Path(file_path)
            
            # Обновляем информацию о файле
            self.update_file_info(file_path)
            
            # Определяем тип файла и отображаем
            if file_path_obj.suffix.lower() in ['.png', '.jpg', '.jpeg', '.gif', '.bmp']:
                self.display_image(file_path)
            elif file_path_obj.suffix.lower() in ['.mp4', '.avi', '.mov', '.mkv']:
                self.display_video_thumbnail(file_path)
            else:
                self.show_unsupported_file()
            
        except Exception as e:
            logger.error(f"Ошибка отображения медиа файла {file_path}: {e}")
            self.show_error_message(f"Ошибка отображения файла:\n{e}")
    
    def update_file_info(self, file_path: str):
        """Обновление информации о файле"""
        try:
            file_path_obj = Path(file_path)
            
            # Очищаем текущую информацию
            self.file_info_text.delete("1.0", tk.END)
            
            if not file_path_obj.exists():
                self.file_info_text.insert("1.0", "Файл не найден")
                return
            
            # Базовая информация
            stat = file_path_obj.stat()
            info_lines = [
                f"Файл: {file_path_obj.name}",
                f"Путь: {file_path_obj.parent}",
                f"Размер: {self.format_file_size(stat.st_size)}",
                f"Изменен: {self.format_timestamp(stat.st_mtime)}"
            ]
            
            # Дополнительная информация для изображений
            if self.media_processor and file_path_obj.suffix.lower() in ['.png', '.jpg', '.jpeg', '.gif', '.bmp']:
                image_info = self.media_processor.get_image_info(file_path)
                if image_info.get("dimensions"):
                    width, height = image_info["dimensions"]
                    info_lines.append(f"Размеры: {width} × {height}")
                if image_info.get("format"):
                    info_lines.append(f"Формат: {image_info['format']}")
            
            # Выводим информацию
            self.file_info_text.insert("1.0", "\n".join(info_lines))
            
        except Exception as e:
            logger.error(f"Ошибка получения информации о файле: {e}")
            self.file_info_text.insert("1.0", f"Ошибка: {e}")
    
    def format_timestamp(self, timestamp: float) -> str:
        """Форматирование временной метки"""
        import datetime
        return datetime.datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y %H:%M:%S")
    
    def display_image(self, image_path: str):
        """Отображение изображения"""
        if not self.media_processor:
            return
        
        try:
            # Скрываем метку пустого состояния
            self.empty_label.pack_forget()
            
            # Показываем кнопку редактирования изображений
            self.edit_image_btn.pack(side="left", padx=5)
            
            # Скрываем кнопки управления видео (если показаны)
            self.play_video_btn.pack_forget()
            self.pause_video_btn.pack_forget()
            self.stop_video_btn.pack_forget()
            
            # Останавливаем видео если воспроизводится
            self.stop_video()
            
            # Загружаем изображение для отображения
            photo = self.media_processor.load_image_for_display(image_path)
            
            if photo:
                self.current_photo = photo
                
                # Очищаем canvas
                self.image_canvas.delete("all")
                
                # Центрируем изображение
                canvas_width = self.image_canvas.winfo_width()
                canvas_height = self.image_canvas.winfo_height()
                
                x = canvas_width // 2
                y = canvas_height // 2
                
                # Отображаем изображение
                self.image_canvas.create_image(x, y, image=photo, anchor="center", tags="image")
                
                # Настраиваем область прокрутки
                self.image_canvas.configure(scrollregion=self.image_canvas.bbox("all"))
                
            else:
                self.show_error_message("Не удалось загрузить изображение")
            
        except Exception as e:
            logger.error(f"Ошибка отображения изображения {image_path}: {e}")
            self.show_error_message(f"Ошибка отображения изображения:\n{e}")
    
    def display_video_thumbnail(self, video_path: str):
        """Отображение превью видео"""
        if not self.media_processor:
            return
        
        try:
            self.current_video_path = video_path
            
            # Создаем превью кадр
            thumbnail_path = self.media_processor.get_video_thumbnail(video_path)
            
            if thumbnail_path:
                self.display_image(thumbnail_path)
                
                # Добавляем индикатор видео
                self.image_canvas.create_text(
                    50, 50, 
                    text="🎬 ВИДЕО", 
                    font=("Arial", 14, "bold"), 
                    fill="white",
                    tags="video_indicator"
                )
                
                # Скрываем кнопку редактирования изображений для видео
                self.edit_image_btn.pack_forget()
                
                # Показываем кнопки управления видео
                self.play_video_btn.pack(side="left", padx=5)
                self.pause_video_btn.pack(side="left", padx=2)
                self.stop_video_btn.pack(side="left", padx=2)
            else:
                self.show_error_message("Не удалось создать превью видео")
            
        except Exception as e:
            logger.error(f"Ошибка отображения превью видео {video_path}: {e}")
            self.show_error_message(f"Ошибка отображения видео:\n{e}")
    
    def show_unsupported_file(self):
        """Показ сообщения о неподдерживаемом файле"""
        self.image_canvas.delete("all")
        self.image_canvas.create_text(
            200, 150,
            text="Предварительный просмотр\nне поддерживается\nдля этого типа файлов",
            font=("Arial", 12),
            justify="center",
            tags="message"
        )
    
    def show_error_message(self, message: str):
        """Показ сообщения об ошибке"""
        self.image_canvas.delete("all")
        self.image_canvas.create_text(
            200, 150,
            text=f"Ошибка:\n{message}",
            font=("Arial", 10),
            justify="center",
            fill="red",
            tags="error"
        )
    
    def clear_viewer(self):
        """Очистка области просмотра"""
        self.image_canvas.delete("all")
        self.empty_label = ttk.Label(self.image_canvas, text="Выберите медиа файл для просмотра", 
                                    anchor="center")
        self.image_canvas.create_window(200, 150, window=self.empty_label)
        
        self.file_info_text.delete("1.0", tk.END)
        self.current_photo = None
        self.current_image_path = None
        self.current_video_path = None
        self.zoom_factor = 1.0
    
    def zoom_in(self):
        """Увеличение изображения"""
        # TODO: Реализовать зум
        messagebox.showinfo("Информация", "Функция зума будет реализована позже")
    
    def zoom_out(self):
        """Уменьшение изображения"""
        # TODO: Реализовать зум
        messagebox.showinfo("Информация", "Функция зума будет реализована позже")
    
    def fit_to_window(self):
        """Подгонка изображения под размер окна"""
        # TODO: Реализовать подгонку размера
        messagebox.showinfo("Информация", "Функция подгонки размера будет реализована позже")
    
    def play_video_external(self):
        """Воспроизведение видео в системном плеере"""
        if not self.current_video_path:
            messagebox.showwarning("Предупреждение", "Видео файл не выбран")
            return
        
        try:
            video_path = Path(self.current_video_path)
            if not video_path.exists():
                messagebox.showerror("Ошибка", "Видео файл не найден")
                return
            
            import subprocess
            import sys
            
            if sys.platform == "win32":
                subprocess.run(["start", video_path], shell=True)
            elif sys.platform == "darwin":  # macOS
                subprocess.run(["open", video_path])
            else:  # Linux
                subprocess.run(["xdg-open", video_path])
                
            logger.info(f"Открыто видео для воспроизведения: {video_path}")
                
        except Exception as e:
            logger.error(f"Ошибка воспроизведения видео: {e}")
            messagebox.showerror("Ошибка", f"Не удалось воспроизвести видео:\n{e}")
    
    def play_video_embedded(self):
        """Встроенное воспроизведение видео"""
        if not HAS_OPENCV or not HAS_PIL:
            missing = []
            if not HAS_OPENCV:
                missing.append("OpenCV")
            if not HAS_PIL:
                missing.append("Pillow")
            messagebox.showerror("Ошибка", f"Не установлены библиотеки: {', '.join(missing)}.\nИспользуйте внешний плеер.")
            return
            
        if not self.current_video_path:
            messagebox.showwarning("Предупреждение", "Видео файл не выбран")
            return
        
        try:
            # Останавливаем текущее воспроизведение если есть
            self.stop_video()
            
            # Открываем видео
            self.video_capture = cv2.VideoCapture(self.current_video_path)
            
            if not self.video_capture.isOpened():
                messagebox.showerror("Ошибка", "Не удалось открыть видео файл")
                return
            
            # Получаем FPS видео
            self.fps = self.video_capture.get(cv2.CAP_PROP_FPS)
            if self.fps <= 0:
                self.fps = 30  # По умолчанию
            
            # Запускаем воспроизведение
            self.is_playing = True
            self.is_paused = False
            
            # Запускаем поток воспроизведения
            self.video_thread = threading.Thread(target=self._video_playback_loop, daemon=True)
            self.video_thread.start()
            
            logger.info(f"Начато встроенное воспроизведение видео: {self.current_video_path}")
            
        except Exception as e:
            logger.error(f"Ошибка запуска встроенного видеоплеера: {e}")
            messagebox.showerror("Ошибка", f"Не удалось запустить видео:\n{e}")
    
    def pause_video(self):
        """Пауза/возобновление воспроизведения"""
        if self.is_playing:
            self.is_paused = not self.is_paused
            if self.is_paused:
                self.pause_video_btn.config(text="▶️ Продолжить")
                logger.info("Видео поставлено на паузу")
            else:
                self.pause_video_btn.config(text="⏸️ Пауза")
                logger.info("Воспроизведение видео возобновлено")
    
    def stop_video(self):
        """Остановка воспроизведения"""
        if self.is_playing:
            self.is_playing = False
            self.is_paused = False
            
            if self.video_capture:
                self.video_capture.release()
                self.video_capture = None
            
            # Сбрасываем текст кнопки
            self.pause_video_btn.config(text="⏸️ Пауза")
            
            logger.info("Воспроизведение видео остановлено")
    
    def _video_playback_loop(self):
        """Основной цикл воспроизведения видео"""
        frame_delay = 1.0 / self.fps
        
        while self.is_playing and self.video_capture:
            if not self.is_paused:
                ret, frame = self.video_capture.read()
                
                if not ret:
                    # Конец видео
                    self.is_playing = False
                    self.is_paused = False
                    break
                
                # Конвертируем кадр для tkinter
                try:
                    # Изменяем размер кадра для отображения
                    canvas_width = self.image_canvas.winfo_width()
                    canvas_height = self.image_canvas.winfo_height()
                    
                    if canvas_width > 1 and canvas_height > 1:
                        height, width = frame.shape[:2]
                        
                        # Вычисляем масштаб для сохранения пропорций
                        scale_x = canvas_width / width
                        scale_y = canvas_height / height
                        scale = min(scale_x, scale_y)
                        
                        new_width = int(width * scale * 0.9)  # Небольшой отступ
                        new_height = int(height * scale * 0.9)
                        
                        # Изменяем размер кадра
                        frame = cv2.resize(frame, (new_width, new_height))
                        
                        # Конвертируем BGR в RGB
                        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        
                        # Конвертируем в PIL Image и затем в PhotoImage
                        pil_image = Image.fromarray(frame_rgb)
                        photo = ImageTk.PhotoImage(pil_image)
                        
                        # Обновляем canvas в главном потоке
                        self.image_canvas.after(0, self._update_video_frame, photo)
                        
                except Exception as e:
                    logger.error(f"Ошибка обработки кадра видео: {e}")
                    break
            
            time.sleep(frame_delay)
        
        # Освобождаем ресурсы
        if self.video_capture:
            self.video_capture.release()
            self.video_capture = None
    
    def _update_video_frame(self, photo):
        """Обновление кадра видео в canvas (вызывается в главном потоке)"""
        try:
            # Сохраняем ссылку на фото чтобы оно не удалилось сборщиком мусора
            self.current_photo = photo
            
            # Очищаем canvas и отображаем новый кадр
            self.image_canvas.delete("all")
            
            # Центрируем кадр
            canvas_width = self.image_canvas.winfo_width()
            canvas_height = self.image_canvas.winfo_height()
            
            x = canvas_width // 2
            y = canvas_height // 2
            
            self.image_canvas.create_image(x, y, image=photo, anchor="center", tags="video_frame")
            
        except Exception as e:
            logger.error(f"Ошибка обновления кадра видео: {e}")
    
    def open_in_system(self):
        """Открытие файла в системном приложении"""
        current_path = self.current_image_path or self.current_video_path
        if current_path:
            try:
                import subprocess
                import sys
                
                if sys.platform == "win32":
                    subprocess.run(["start", current_path], shell=True)
                elif sys.platform == "darwin":
                    subprocess.run(["open", current_path])
                else:
                    subprocess.run(["xdg-open", current_path])
                    
            except Exception as e:
                logger.error(f"Ошибка открытия файла в системе: {e}")
                messagebox.showerror("Ошибка", f"Не удалось открыть файл:\n{e}")
        else:
            messagebox.showwarning("Предупреждение", "Выберите файл для открытия")
    
    def on_media_double_click(self, event):
        """Обработчик двойного клика по медиа файлу"""
        self.open_in_system()
    
    def show_media_context_menu(self, event):
        """Показ контекстного меню для медиа файла"""
        item = self.media_tree.identify_row(event.y)
        if item:
            self.media_tree.selection_set(item)
            self.on_media_select(None)
            
            # Создаем контекстное меню
            context_menu = tk.Menu(self, tearoff=0)
            context_menu.add_command(label="Открыть в системе", command=self.open_in_system)
            context_menu.add_command(label="Показать в папке", command=self.show_in_folder)
            context_menu.add_separator()
            context_menu.add_command(label="Регенерировать", command=self.regenerate_media)
            context_menu.add_command(label="Копировать путь", command=self.copy_path)
            
            # Показываем меню
            try:
                context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                context_menu.grab_release()
    
    def show_in_folder(self):
        """Показать файл в папке"""
        current_path = self.current_image_path or self.current_video_path
        if current_path:
            try:
                import subprocess
                import sys
                
                folder_path = str(Path(current_path).parent)
                
                if sys.platform == "win32":
                    subprocess.run(["explorer", "/select,", current_path])
                elif sys.platform == "darwin":
                    subprocess.run(["open", "-R", current_path])
                else:
                    subprocess.run(["xdg-open", folder_path])
                    
            except Exception as e:
                logger.error(f"Ошибка показа файла в папке: {e}")
                messagebox.showerror("Ошибка", f"Не удалось показать файл в папке:\n{e}")
    
    def regenerate_media(self):
        """Регенерация медиа файла"""
        # TODO: Интеграция с pipeline для регенерации
        messagebox.showinfo("Информация", "Функция регенерации будет реализована позже")
    
    def copy_path(self):
        """Копирование пути файла в буфер обмена"""
        current_path = self.current_image_path or self.current_video_path
        if current_path:
            try:
                self.clipboard_clear()
                self.clipboard_append(current_path)
                messagebox.showinfo("Успех", "Путь скопирован в буфер обмена")
            except Exception as e:
                logger.error(f"Ошибка копирования пути: {e}")
    
    def open_media_folder(self):
        """Открытие папки с медиа файлами"""
        if self.current_project:
            try:
                import subprocess
                import sys
                
                media_path = self.current_project.project_path / "50_images"
                
                if sys.platform == "win32":
                    subprocess.run(["explorer", str(media_path)])
                elif sys.platform == "darwin":
                    subprocess.run(["open", str(media_path)])
                else:
                    subprocess.run(["xdg-open", str(media_path)])
                    
            except Exception as e:
                logger.error(f"Ошибка открытия папки медиа: {e}")
                messagebox.showerror("Ошибка", f"Не удалось открыть папку:\n{e}")
    
    def clear_cache(self):
        """Очистка кэша медиа файлов"""
        if self.media_processor:
            try:
                self.media_processor.cleanup_cache()
                messagebox.showinfo("Успех", "Кэш медиа файлов очищен")
            except Exception as e:
                logger.error(f"Ошибка очистки кэша: {e}")
                messagebox.showerror("Ошибка", f"Не удалось очистить кэш:\n{e}")
    
    def on_image_click(self, event):
        """Обработка клика по изображению для открытия полноразмерного просмотра"""
        if self.current_image_path and Path(self.current_image_path).exists():
            try:
                file_name = Path(self.current_image_path).name
                FullSizeImageViewer(self, self.current_image_path, file_name)
            except Exception as e:
                logger.error(f"Ошибка открытия полноразмерного просмотра: {e}")
                messagebox.showerror("Ошибка", f"Не удалось открыть изображение:\n{e}")
    
    def edit_image(self):
        """Редактирование текущего изображения через прямой вызов edit_image_vse_tool"""
        if not self.current_image_path:
            messagebox.showwarning("Предупреждение", "Не выбрано изображение для редактирования")
            return
        
        # Проверяем, что это изображение
        image_path = Path(self.current_image_path)
        if image_path.suffix.lower() not in ['.png', '.jpg', '.jpeg', '.webp']:
            messagebox.showerror("Ошибка", "Можно редактировать только изображения (PNG, JPG, JPEG, WEBP)")
            return
        
        # Получаем список всех изображений проекта для выбора референсов
        if not self.media_processor:
            messagebox.showerror("Ошибка", "Медиа процессор не инициализирован")
            return
        
        try:
            all_images = self.media_processor.get_project_images()
            available_references = []
            
            for img_info in all_images:
                img_path = Path(img_info["path"])
                if img_path.suffix.lower() in ['.png', '.jpg', '.jpeg', '.webp'] and str(img_path) != self.current_image_path:
                    available_references.append({
                        "path": str(img_path),
                        "name": img_path.name
                    })
            
            # Открываем диалог редактирования
            dialog = ImageEditDialog(self, self.current_image_path, available_references)
            if dialog.result:
                self.start_image_editing(
                    prompt=dialog.result["prompt"],
                    reference_paths=[self.current_image_path] + dialog.result["reference_paths"]
                )
        
        except Exception as e:
            logger.error(f"Ошибка при открытии диалога редактирования: {e}")
            messagebox.showerror("Ошибка", f"Не удалось открыть диалог редактирования:\n{e}")
    
    def start_image_editing(self, prompt: str, reference_paths: List[str]):
        """Запуск редактирования изображения в отдельном потоке"""
        if not reference_paths:
            return
        
        original_path = Path(reference_paths[0])  # Первый элемент - исходное изображение
        
        try:
            # Создаем резервную копию
            backup_path = self.create_backup(original_path)
            
            logger.info(f"🖌️ Начинаем редактирование изображения: {original_path}")
            logger.info(f"📋 Резервная копия создана: {backup_path}")
            logger.info(f"🎯 Промпт: {prompt[:100]}...")
            logger.info(f"📁 Референсы: {len(reference_paths)} изображений")
            
            # Запускаем редактирование в отдельном потоке
            def edit_worker():
                try:
                    success = self.edit_image_with_artist_agent(
                        prompt=prompt,
                        reference_paths=reference_paths,
                        output_path=str(original_path)
                    )
                    
                    # Обновляем UI в главном потоке
                    self.after(0, lambda: self.on_edit_completed(success, original_path, backup_path))
                    
                except Exception as e:
                    logger.error(f"Ошибка редактирования изображения: {e}")
                    self.after(0, lambda: self.on_edit_error(str(e), backup_path))
            
            # Показываем прогресс
            self.show_edit_progress(True)
            
            # Запускаем в отдельном потоке
            threading.Thread(target=edit_worker, daemon=True).start()
        
        except Exception as e:
            logger.error(f"Ошибка подготовки к редактированию: {e}")
            messagebox.showerror("Ошибка", f"Не удалось подготовить редактирование:\n{e}")
    
    def create_backup(self, original_path: Path) -> Path:
        """Создание резервной копии изображения"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{original_path.stem}__backup_{timestamp}{original_path.suffix}"
        backup_path = original_path.parent / backup_name
        
        # Если файл с таким именем уже существует, добавляем счетчик
        counter = 1
        while backup_path.exists():
            backup_name = f"{original_path.stem}__backup_{timestamp}__{counter}{original_path.suffix}"
            backup_path = original_path.parent / backup_name
            counter += 1
        
        shutil.copy2(original_path, backup_path)
        return backup_path
    
    def edit_image_with_artist_agent(self, prompt: str, reference_paths: List[str], output_path: str) -> bool:
        """Редактирование изображения через прямой вызов edit_image_vse_tool (без агента)"""
        try:
            # Импортируем необходимые модули
            from custom_tools.image_tools import edit_image_vse_tool
            from utils import _translate_texts_batch
            import uuid
            
            session_id = str(uuid.uuid4())
            
            # Переводим промпт на английский язык
            logger.info(f"🌐 Переводим промпт на английский: {prompt}...")
            try:
                translated_prompts = _translate_texts_batch([prompt], 'en')
                english_prompt = translated_prompts[0] if translated_prompts else prompt
                logger.info(f"🌐 Переведенный промпт: {english_prompt}...")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка перевода, используем исходный промпт: {e}")
                english_prompt = prompt
            
            # Выполняем редактирование напрямую через edit_image_vse_tool
            logger.info(f"🎨 Запускаем редактирование изображения через edit_image_vse_tool...")
            logger.info(f"📁 Изображения для редактирования: {len(reference_paths)} файлов")
            for i, path in enumerate(reference_paths):
                logger.info(f"  {i+1}. {path}")
            
            log_data = {
                "📝 Промпт": english_prompt,
                "🖼️  Изображений": len(reference_paths),
                "📁 Файлы": [reference_paths],
                "💾 Выходный путь": output_path
            }
            
            log_smolagents_panel(
                content=log_data,
                title="🎨 Artist Generation Process (edit_image_vse_tool)",
                title_style="bold green",
                border_style="blue"
            )
            
            
            result = edit_image_vse_tool(
                prompt=english_prompt,
                image_paths=reference_paths,
                session_id=session_id,
                output_path=output_path
            )
            
            logger.info(f"🎨 Результат edit_image_vse_tool: {result}")

            log_data = {
                "🎨 Результат": result,
                "💾 Выходный путь": output_path
            }
            log_smolagents_panel(
                content=log_data,
                title="🎨 Artist Generation Process (edit_image_vse_tool)",
                title_style="bold green",
                border_style="blue"
            )
            
            # Проверяем результат
            output_path_obj = Path(output_path)
            if output_path_obj.exists():
                logger.info(f"✅ Изображение отредактировано: {output_path}")
                return True
            else:
                logger.warning(f"⚠️ Файл не был создан: {output_path}")
                logger.warning(f"⚠️ Результат инструмента: {result}")
                return False
                
        except Exception as e:
            logger.error(f"Ошибка редактирования через edit_image_vse_tool: {e}")
            return False
    
    def show_edit_progress(self, show: bool):
        """Показ/скрытие индикатора прогресса редактирования"""
        if show:
            # Создаем простой индикатор прогресса
            self.image_canvas.create_rectangle(
                50, 50, 350, 120,
                fill="white", outline="black", width=2,
                tags="progress_indicator"
            )
            self.image_canvas.create_text(
                200, 85,
                text="🖌️ Редактирование изображения...\nПожалуйста, подождите",
                font=("Arial", 12),
                justify="center",
                tags="progress_indicator"
            )
        else:
            # Удаляем индикатор прогресса
            self.image_canvas.delete("progress_indicator")
    
    def on_edit_completed(self, success: bool, original_path: Path, backup_path: Path):
        """Обработка завершения редактирования"""
        self.show_edit_progress(False)
        
        if success:
            # Перезагружаем изображение
            self.display_image(str(original_path))
            
            # Обновляем список медиа файлов
            self.refresh_media()
            
            # Показываем сообщение об успехе
            result = messagebox.showinfo(
                "Успех", 
                f"Изображение успешно отредактировано!\n\n"
                f"Исходная версия сохранена как:\n{backup_path.name}",
                icon="info"
            )
            
            logger.info(f"✅ Редактирование завершено успешно")
        else:
            messagebox.showerror(
                "Ошибка", 
                "Не удалось отредактировать изображение.\n"
                f"Исходная версия сохранена как:\n{backup_path.name}"
            )
    
    def on_edit_error(self, error_message: str, backup_path: Path):
        """Обработка ошибки редактирования"""
        self.show_edit_progress(False)
        
        messagebox.showerror(
            "Ошибка редактирования", 
            f"Произошла ошибка при редактировании:\n{error_message}\n\n"
            f"Исходная версия сохранена как:\n{backup_path.name}"
        )


class ImageEditDialog:
    """Диалог редактирования изображения"""
    
    def __init__(self, parent, image_path: str, available_references: List[Dict[str, str]]):
        self.result = None
        self.image_path = image_path
        self.available_references = available_references
        self.selected_references = []
        
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Редактирование изображения")
        self.dialog.geometry("1000x750")
        self.dialog.resizable(True, True)
        self.dialog.grab_set()  # Модальный режим
        
        # Центрируем диалог
        self.dialog.transient(parent)
        self.center_dialog(parent)
        
        self.create_ui()
        
        # Ожидаем закрытия диалога
        self.dialog.wait_window()
    
    def center_dialog(self, parent):
        """Центрирование диалога"""
        parent_x = parent.winfo_rootx()
        parent_y = parent.winfo_rooty()
        parent_width = parent.winfo_width()
        parent_height = parent.winfo_height()
        
        dialog_width = 1000
        dialog_height = 750
        
        x = parent_x + (parent_width - dialog_width) // 2
        y = parent_y + (parent_height - dialog_height) // 2
        
        self.dialog.geometry(f"{dialog_width}x{dialog_height}+{x}+{y}")
    
    def create_ui(self):
        """Создание пользовательского интерфейса диалога"""
        # Заголовок
        title_frame = ttk.Frame(self.dialog)
        title_frame.pack(fill="x", padx=20, pady=(20, 10))
        
        ttk.Label(
            title_frame,
            text=f"Редактирование: {Path(self.image_path).name}",
            font=("TkDefaultFont", 12, "bold")
        ).pack()
        
        # Кнопки сначала - в нижней части
        button_frame = ttk.Frame(self.dialog)
        button_frame.pack(side="bottom", fill="x", padx=20, pady=20)
        
        ttk.Button(button_frame, text="❌ Отмена", command=self.cancel).pack(side="right", padx=(10, 0))
        self.edit_button = ttk.Button(button_frame, text="🖌️ Запустить редактирование", command=self.start_edit, state="disabled")
        self.edit_button.pack(side="right")
        
        # Статус
        self.status_label = ttk.Label(button_frame, text="Введите описание изменений", foreground="orange")
        self.status_label.pack(side="left")
        
        # Основной контейнер для содержимого - разделим на левую и правую части
        main_container = ttk.Frame(self.dialog)
        main_container.pack(fill="both", expand=True, padx=20, pady=(10, 0))
        
        # Левая панель (настройки)
        left_panel = ttk.Frame(main_container)
        left_panel.pack(side="left", fill="both", expand=True, padx=(0, 10))
        
        # Правая панель (предпросмотр)
        right_panel = ttk.LabelFrame(main_container, text="Предпросмотр", padding=10)
        right_panel.pack(side="right", fill="both", padx=(10, 0))
        right_panel.config(width=320)
        
        # Поле промпта в левой панели
        prompt_frame = ttk.LabelFrame(left_panel, text="Что изменить в изображении", padding=15)
        prompt_frame.pack(fill="x", pady=(0, 15))
        
        self.prompt_text = tk.Text(prompt_frame, width=40, height=6, wrap="word")
        prompt_scrollbar = ttk.Scrollbar(prompt_frame, orient="vertical", command=self.prompt_text.yview)
        self.prompt_text.configure(yscrollcommand=prompt_scrollbar.set)
        
        self.prompt_text.pack(side="left", fill="both", expand=True)
        prompt_scrollbar.pack(side="right", fill="y")
        
        # Добавляем поддержку прокрутки колесом мыши для поля промпта
        bind_mousewheel_to_text_with_scrollbar(self.prompt_text, prompt_scrollbar)
        
        # Секция референсов в левой панели
        references_frame = ttk.LabelFrame(left_panel, text="Дополнительные референсы (выберите до 3)", padding=15)
        references_frame.pack(fill="both", expand=True, pady=(0, 15))
        
        # Информация об исходном изображении
        info_frame = ttk.Frame(references_frame)
        info_frame.pack(fill="x", pady=(0, 10))
        
        ttk.Label(info_frame, text="Редактируемое изображение:", font=("TkDefaultFont", 9, "bold")).pack(anchor="w")
        ttk.Label(info_frame, text=f"📷 {Path(self.image_path).name} (всегда первое в списке)", 
                 foreground="blue").pack(anchor="w")
        
        # Счетчик выбранных референсов
        self.counter_label = ttk.Label(info_frame, text="Выбрано: 0/3 дополнительных", foreground="gray")
        self.counter_label.pack(anchor="w", pady=(5, 0))
        
        # Список доступных референсов
        if self.available_references:
            # Создаем Treeview для выбора референсов
            tree_frame = ttk.Frame(references_frame)
            tree_frame.pack(fill="both", expand=True)
            
            self.refs_tree = ttk.Treeview(tree_frame, height=6, selectmode="extended")
            self.refs_tree.heading("#0", text="Доступные изображения для референса")
            
            refs_scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.refs_tree.yview)
            self.refs_tree.configure(yscrollcommand=refs_scrollbar.set)
            
            self.refs_tree.pack(side="left", fill="both", expand=True)
            refs_scrollbar.pack(side="right", fill="y")
            
            # Добавляем поддержку прокрутки колесом мыши для дерева референсов
            bind_mousewheel_to_treeview(self.refs_tree)
            
            # Заполняем список референсов - храним данные в самом объекте
            self.ref_item_data = {}  # Словарь для хранения путей по item_id
            for ref in self.available_references:
                item_id = self.refs_tree.insert("", "end", text=ref["name"])
                self.ref_item_data[item_id] = ref["path"]
            
            # Привязываем события
            self.refs_tree.bind("<<TreeviewSelect>>", self.on_reference_selected)
            
            # Кнопка очистки
            ttk.Button(references_frame, text="🗑️ Очистить выбор", command=self.clear_references).pack(pady=(10, 0))
        else:
            ttk.Label(references_frame, text="Нет доступных изображений для использования как референсы").pack()
        
        # Создаем панель предпросмотра
        self.create_preview_panel(right_panel)
        
        # Валидация в реальном времени
        self.prompt_text.bind('<KeyRelease>', self.validate_form)
        
        # Фокус на поле промпта
        self.prompt_text.focus_set()
        
        # Начальная валидация
        self.validate_form()
    
    def on_reference_selected(self, event):
        """Обработка выбора референсов"""
        selected_items = self.refs_tree.selection()
        
        # Ограничиваем выбор до 3 элементов
        if len(selected_items) > 3:
            # Снимаем выделение с лишних элементов
            for item in selected_items[3:]:
                self.refs_tree.selection_remove(item)
            selected_items = selected_items[:3]
        
        # Обновляем список выбранных путей
        self.selected_references = []
        for item in selected_items:
            # Получаем путь к файлу из нашего словаря
            if item in self.ref_item_data:
                self.selected_references.append(self.ref_item_data[item])
        
        # Обновляем счетчик
        count = len(self.selected_references)
        self.counter_label.config(text=f"Выбрано: {count}/3 дополнительных")
        
        # Обновляем предпросмотр
        if self.selected_references:
            # Показываем последний выбранный референс
            last_selected_path = self.selected_references[-1]
            last_selected_name = Path(last_selected_path).name
            self.show_preview_image(last_selected_path, f"Референс: {last_selected_name}")
        else:
            # Если никто не выбран, показываем исходное изображение
            self.show_preview_image(self.image_path, "Исходное изображение")
        
        # Обновляем мини-превью
        self.update_mini_previews()
        
        self.validate_form()
    
    def clear_references(self):
        """Очистка выбранных referencer"""
        if hasattr(self, 'refs_tree'):
            self.refs_tree.selection_remove(*self.refs_tree.selection())
        self.selected_references = []
        self.counter_label.config(text="Выбрано: 0/3 дополнительных")
        
        # Возвращаем предпросмотр к исходному изображению
        self.show_preview_image(self.image_path, "Исходное изображение")
        
        # Обновляем мини-превью
        self.update_mini_previews()
        
        self.validate_form()
    
    def validate_form(self, event=None):
        """Валидация формы"""
        prompt = self.prompt_text.get("1.0", tk.END).strip()
        
        if len(prompt) >= 5:
            self.edit_button.config(state="normal")
            self.status_label.config(text="Готово к редактированию ✅", foreground="green")
        else:
            self.edit_button.config(state="disabled")
            self.status_label.config(text="Введите описание изменений (минимум 5 символов)", foreground="orange")
    
    def start_edit(self):
        """Запуск редактирования"""
        prompt = self.prompt_text.get("1.0", tk.END).strip()
        
        if len(prompt) < 5:
            messagebox.showerror("Ошибка", "Описание изменений должно содержать минимум 5 символов")
            return
        
        self.result = {
            "prompt": prompt,
            "reference_paths": self.selected_references
        }
        
        self.dialog.destroy()
    
    def create_preview_panel(self, parent):
        """Создание панели предпросмотра изображений"""
        # Canvas для отображения основного изображения
        self.preview_canvas = tk.Canvas(parent, width=300, height=320, bg="white", cursor="hand2")
        self.preview_canvas.pack(pady=(0, 10))
        
        # Информация о текущем изображении
        self.preview_info_label = ttk.Label(parent, text="", wraplength=300, justify="center")
        self.preview_info_label.pack(pady=(0, 5))
        
        # Подсказка о клике
        hint_label = ttk.Label(parent, text="💡 Кликните для просмотра в полном размере", 
                              font=("TkDefaultFont", 8), foreground="gray")
        hint_label.pack(pady=(0, 10))
        
        # Секция мини-превью выбранных референсов
        self.mini_preview_frame = ttk.LabelFrame(parent, text="Выбранные референсы", padding=5)
        self.mini_preview_frame.pack(fill="x", pady=(0, 10))
        
        # Scrollable Frame для мини-превью
        self.mini_canvas = tk.Canvas(self.mini_preview_frame, height=100, bg="white")
        self.mini_scrollbar = ttk.Scrollbar(self.mini_preview_frame, orient="horizontal", command=self.mini_canvas.xview)
        self.mini_scrollable_frame = ttk.Frame(self.mini_canvas)
        
        self.mini_scrollable_frame.bind(
            "<Configure>",
            lambda e: self.mini_canvas.configure(scrollregion=self.mini_canvas.bbox("all"))
        )
        
        self.mini_canvas.create_window((0, 0), window=self.mini_scrollable_frame, anchor="nw")
        self.mini_canvas.configure(xscrollcommand=self.mini_scrollbar.set)
        
        self.mini_canvas.pack(side="top", fill="x", expand=True)
        self.mini_scrollbar.pack(side="bottom", fill="x")
        
        # Добавляем поддержку прокрутки колесом мыши для мини-превью (окончательная версия)
        bind_mousewheel_to_canvas_frame_ultimate(self.mini_canvas, self.mini_scrollable_frame)
        
        # Список для хранения мини-превью
        self.mini_previews = []
        
        # Привязываем клик для открытия полноразмерного просмотра
        self.preview_canvas.bind("<Button-1>", self.on_preview_click)
        self.current_preview_path = self.image_path  # Отслеживаем текущее изображение
        
        # Показываем исходное изображение по умолчанию
        self.show_preview_image(self.image_path, "Исходное изображение")
        self.update_mini_previews()
    
    def show_preview_image(self, image_path: str, description: str = ""):
        """Отображение изображения в панели предпросмотра"""
        try:
            if not HAS_PIL:
                self.preview_info_label.config(text="PIL не установлен")
                return
            
            # Очищаем canvas
            self.preview_canvas.delete("all")
            
            # Загружаем и масштабируем изображение
            with Image.open(image_path) as img:
                # Масштабируем изображение, сохраняя пропорции
                img.thumbnail((290, 310), Image.Resampling.LANCZOS)
                
                # Конвертируем для Tkinter
                self.preview_photo = ImageTk.PhotoImage(img)
                
                # Центрируем изображение на canvas
                canvas_width = self.preview_canvas.winfo_reqwidth()
                canvas_height = self.preview_canvas.winfo_reqheight()
                x = canvas_width // 2
                y = canvas_height // 2
                
                self.preview_canvas.create_image(x, y, image=self.preview_photo, anchor="center")
                
                # Обновляем информацию
                file_name = Path(image_path).name
                img_size = f"{img.width}×{img.height}"
                info_text = f"{description}\n{file_name}\n{img_size}"
                self.preview_info_label.config(text=info_text)
                
                # Обновляем путь к текущему изображению
                self.current_preview_path = image_path
                
        except Exception as e:
            self.preview_canvas.create_text(
                150, 160,
                text=f"Ошибка загрузки:\n{str(e)}",
                font=("Arial", 10),
                justify="center",
                fill="red"
            )
            self.preview_info_label.config(text=f"Ошибка: {Path(image_path).name}")
    
    def update_mini_previews(self):
        """Обновление мини-превью выбранных референсов"""
        # Очищаем существующие мини-превью
        for widget in self.mini_scrollable_frame.winfo_children():
            widget.destroy()
        self.mini_previews.clear()
        
        # Всегда показываем исходное изображение первым
        self.create_mini_preview(self.image_path, "Исходное", 0, is_original=True)
        
        # Добавляем выбранные референсы
        for i, ref_path in enumerate(self.selected_references):
            self.create_mini_preview(ref_path, f"Реф {i+1}", i+1, is_original=False)
        
        # Обновляем scrollregion
        self.mini_canvas.update_idletasks()
        self.mini_canvas.configure(scrollregion=self.mini_canvas.bbox("all"))
    
    def create_mini_preview(self, image_path: str, label: str, index: int, is_original: bool = False):
        """Создание одного мини-превью"""
        try:
            if not HAS_PIL:
                return
            
            # Контейнер для мини-превью
            mini_frame = ttk.Frame(self.mini_scrollable_frame)
            mini_frame.pack(side="left", padx=2, pady=2)
            
            # Загружаем и уменьшаем изображение
            with Image.open(image_path) as img:
                # Создаем миниатюру 70x70
                img.thumbnail((70, 70), Image.Resampling.LANCZOS)
                mini_photo = ImageTk.PhotoImage(img)
                
                # Canvas для мини-изображения
                mini_canvas = tk.Canvas(mini_frame, width=75, height=75, 
                                      bg="lightblue" if is_original else "lightgray",
                                      highlightthickness=2,
                                      highlightcolor="blue" if is_original else "gray")
                mini_canvas.pack()
                
                # Отображаем изображение
                mini_canvas.create_image(37, 37, image=mini_photo, anchor="center")
                
                # Подпись
                label_widget = ttk.Label(mini_frame, text=label, font=("TkDefaultFont", 8))
                label_widget.pack()
                
                # Сохраняем ссылки на объекты
                self.mini_previews.append({
                    'frame': mini_frame,
                    'canvas': mini_canvas,
                    'photo': mini_photo,
                    'label': label_widget,
                    'path': image_path
                })
                
                # Привязываем клик для увеличения в основном превью
                def on_click(event, path=image_path, desc=label):
                    if is_original:
                        self.show_preview_image(path, "Исходное изображение")
                    else:
                        self.show_preview_image(path, f"Референс: {Path(path).name}")
                
                mini_canvas.bind("<Button-1>", on_click)
                
        except Exception as e:
            # Создаем заглушку при ошибке
            mini_frame = ttk.Frame(self.mini_scrollable_frame)
            mini_frame.pack(side="left", padx=2, pady=2)
            
            error_canvas = tk.Canvas(mini_frame, width=75, height=75, bg="red")
            error_canvas.pack()
            error_canvas.create_text(37, 37, text="❌", font=("Arial", 20), fill="white")
            
            ttk.Label(mini_frame, text="Ошибка", font=("TkDefaultFont", 8)).pack()
    
    def on_preview_click(self, event):
        """Обработка клика по предпросмотру для открытия полноразмерного просмотра"""
        if hasattr(self, 'current_preview_path') and self.current_preview_path:
            file_name = Path(self.current_preview_path).name
            self.show_fullsize_preview(self.current_preview_path, file_name)
    
    def show_fullsize_preview(self, image_path: str, title: str = ""):
        """Показ изображения в полном размере в модальном окне"""
        try:
            FullSizeImageViewer(self.dialog, image_path, title)
        except Exception as e:
            logger.error(f"Ошибка открытия полноразмерного просмотра: {e}")
            messagebox.showerror("Ошибка", f"Не удалось открыть изображение:\n{e}")
    
    def cancel(self):
        """Отмена редактирования"""
        self.dialog.destroy()


class FullSizeImageViewer:
    """Модальное окно для просмотра изображения в полном размере"""
    
    def __init__(self, parent, image_path: str, title: str = ""):
        self.image_path = image_path
        self.title = title or Path(image_path).name
        
        # Создаем модальное окно
        self.viewer = tk.Toplevel(parent)
        self.viewer.title(f"Просмотр: {self.title}")
        self.viewer.grab_set()  # Модальный режим
        self.viewer.transient(parent)
        
        # Настройка окна
        self.viewer.configure(bg="black")
        
        # Переменные для масштабирования и перемещения
        self.scale_factor = 1.0
        self.pan_start_x = 0
        self.pan_start_y = 0
        self.image_x = 0
        self.image_y = 0
        
        # Создаем UI
        self.create_ui()
        
        # Загружаем изображение
        self.load_and_display_image()
        
        # Привязываем события
        self.bind_events()
        
        # Центрируем окно
        self.center_window()
    
    def create_ui(self):
        """Создание пользовательского интерфейса"""
        # Панель инструментов сверху
        toolbar = ttk.Frame(self.viewer)
        toolbar.pack(side="top", fill="x", padx=5, pady=5)
        
        # Информация об изображении
        self.info_label = ttk.Label(toolbar, text="", foreground="white", background="black")
        self.info_label.pack(side="left")
        
        # Кнопки управления
        button_frame = ttk.Frame(toolbar)
        button_frame.pack(side="right")
        
        ttk.Button(button_frame, text="🔍+", command=self.zoom_in, width=4).pack(side="left", padx=2)
        ttk.Button(button_frame, text="🔍-", command=self.zoom_out, width=4).pack(side="left", padx=2)
        ttk.Button(button_frame, text="📐", command=self.fit_to_window, width=4).pack(side="left", padx=2)
        ttk.Button(button_frame, text="1:1", command=self.actual_size, width=4).pack(side="left", padx=2)
        ttk.Button(button_frame, text="❌", command=self.close, width=4).pack(side="left", padx=(10, 0))
        
        # Canvas для изображения
        self.canvas = tk.Canvas(self.viewer, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        
        # Scrollbars
        self.h_scrollbar = ttk.Scrollbar(self.viewer, orient="horizontal", command=self.canvas.xview)
        self.v_scrollbar = ttk.Scrollbar(self.viewer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=self.h_scrollbar.set, yscrollcommand=self.v_scrollbar.set)
    
    def load_and_display_image(self):
        """Загрузка и отображение изображения"""
        try:
            if not HAS_PIL:
                self.info_label.config(text="PIL не установлен")
                return
            
            # Загружаем изображение
            self.original_image = Image.open(self.image_path)
            self.display_image()
            
            # Обновляем информацию
            file_size = Path(self.image_path).stat().st_size
            size_str = self.format_file_size(file_size)
            info_text = f"{self.title} | {self.original_image.width}×{self.original_image.height} | {size_str}"
            self.info_label.config(text=info_text)
            
        except Exception as e:
            self.canvas.create_text(
                200, 200,
                text=f"Ошибка загрузки изображения:\n{str(e)}",
                fill="red",
                font=("Arial", 14),
                justify="center"
            )
    
    def display_image(self):
        """Отображение изображения с текущим масштабом"""
        if not hasattr(self, 'original_image'):
            return
        
        # Вычисляем новый размер
        new_width = int(self.original_image.width * self.scale_factor)
        new_height = int(self.original_image.height * self.scale_factor)
        
        # Масштабируем изображение
        if self.scale_factor != 1.0:
            resized_image = self.original_image.resize((new_width, new_height), Image.Resampling.LANCZOS)
        else:
            resized_image = self.original_image
        
        # Конвертируем для Tkinter
        self.photo = ImageTk.PhotoImage(resized_image)
        
        # Очищаем canvas и отображаем изображение
        self.canvas.delete("all")
        self.image_id = self.canvas.create_image(
            self.image_x, self.image_y, 
            image=self.photo, 
            anchor="nw"
        )
        
        # Обновляем область прокрутки
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
    
    def zoom_in(self):
        """Увеличение масштаба"""
        self.scale_factor *= 1.25
        self.display_image()
    
    def zoom_out(self):
        """Уменьшение масштаба"""
        self.scale_factor /= 1.25
        self.display_image()
    
    def fit_to_window(self):
        """Подгонка изображения под размер окна"""
        if not hasattr(self, 'original_image'):
            return
        
        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()
        
        if canvas_width <= 1 or canvas_height <= 1:
            return
        
        # Вычисляем масштаб для подгонки
        scale_x = canvas_width / self.original_image.width
        scale_y = canvas_height / self.original_image.height
        self.scale_factor = min(scale_x, scale_y) * 0.95  # Небольшой отступ
        
        self.image_x = 0
        self.image_y = 0
        self.display_image()
    
    def actual_size(self):
        """Отображение в реальном размере"""
        self.scale_factor = 1.0
        self.image_x = 0
        self.image_y = 0
        self.display_image()
    
    def bind_events(self):
        """Привязка событий"""
        # Колесо мыши для масштабирования
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind("<Button-4>", self.on_mousewheel)
        self.canvas.bind("<Button-5>", self.on_mousewheel)
        
        # Перетаскивание для панорамирования
        self.canvas.bind("<Button-1>", self.on_pan_start)
        self.canvas.bind("<B1-Motion>", self.on_pan_motion)
        
        # Клавиши
        self.viewer.bind("<Key>", self.on_key_press)
        self.viewer.focus_set()
        
        # Изменение размера окна
        self.viewer.bind("<Configure>", self.on_window_resize)
    
    def on_mousewheel(self, event):
        """Обработка колеса мыши для масштабирования"""
        import sys
        
        if sys.platform == "win32":
            if event.delta > 0:
                self.zoom_in()
            else:
                self.zoom_out()
        elif sys.platform == "darwin":  # macOS
            if event.delta > 0:
                self.zoom_in()
            else:
                self.zoom_out()
        else:  # Linux
            if event.num == 4:
                self.zoom_in()
            elif event.num == 5:
                self.zoom_out()
    
    def on_pan_start(self, event):
        """Начало перетаскивания"""
        self.pan_start_x = event.x
        self.pan_start_y = event.y
    
    def on_pan_motion(self, event):
        """Перетаскивание изображения"""
        dx = event.x - self.pan_start_x
        dy = event.y - self.pan_start_y
        
        self.image_x += dx
        self.image_y += dy
        
        self.pan_start_x = event.x
        self.pan_start_y = event.y
        
        self.display_image()
    
    def on_key_press(self, event):
        """Обработка нажатий клавиш"""
        if event.keysym == "Escape":
            self.close()
        elif event.keysym == "plus" or event.keysym == "equal":
            self.zoom_in()
        elif event.keysym == "minus":
            self.zoom_out()
        elif event.keysym == "0":
            self.fit_to_window()
        elif event.keysym == "1":
            self.actual_size()
    
    def on_window_resize(self, event):
        """Обработка изменения размера окна"""
        if event.widget == self.viewer:
            # Небольшая задержка для избежания множественных вызовов
            self.viewer.after(100, self.fit_to_window)
    
    def center_window(self):
        """Центрирование окна"""
        # Получаем размер экрана
        screen_width = self.viewer.winfo_screenwidth()
        screen_height = self.viewer.winfo_screenheight()
        
        # Устанавливаем размер окна (80% от экрана)
        window_width = int(screen_width * 0.8)
        window_height = int(screen_height * 0.8)
        
        # Центрируем
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2
        
        self.viewer.geometry(f"{window_width}x{window_height}+{x}+{y}")
        
        # Подгоняем изображение после установки размера
        self.viewer.after(100, self.fit_to_window)
    
    def format_file_size(self, size_bytes: int) -> str:
        """Форматирование размера файла"""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
    
    def close(self):
        """Закрытие окна просмотра"""
        self.viewer.destroy()
