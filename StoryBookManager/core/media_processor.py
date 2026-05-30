"""
Обработка медиа файлов
=====================

Модуль для работы с изображениями и видео файлами проекта.
Включает создание превью, кэширование и базовую обработку.
"""

import os
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import logging

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("PIL (Pillow) не доступен. Функции работы с изображениями будут ограничены.")

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("OpenCV не доступен. Функции работы с видео будут ограничены.")

import tkinter as tk

from config.settings import app_settings

logger = logging.getLogger(__name__)


class MediaProcessor:
    """Обработка медиа файлов проекта"""
    
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.projects_dir = app_settings.get_projects_directory()
        self.project_path = self.projects_dir / project_id
        self.cache_dir = app_settings.get_backup_directory() / "media_cache" / project_id
        
        # Создаем директорию кэша
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Поддерживаемые форматы
        self.image_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff'}
        self.video_extensions = {'.mp4', '.avi', '.mov', '.mkv', '.webm'}
    
    def get_project_images(self) -> List[Dict[str, Any]]:
        """Возвращает список всех изображений в проекте"""
        images = []
        
        try:
            # Основные изображения (иллюстрации книги)
            images_dir = self.project_path / "50_images"
            if images_dir.exists():
                for page_dir in images_dir.iterdir():
                    if page_dir.is_dir():
                        for img_file in page_dir.iterdir():
                            if img_file.suffix.lower() in self.image_extensions:
                                images.append({
                                    "type": "illustration",
                                    "category": "book_page",
                                    "page": page_dir.name,
                                    "path": str(img_file),
                                    "name": img_file.name,
                                    "size": img_file.stat().st_size,
                                    "modified": img_file.stat().st_mtime
                                })
            
            # Референсы персонажей и локаций
            references_dir = self.project_path / "20_bible" / "references"
            if references_dir.exists():
                for ref_file in references_dir.rglob("*"):
                    if ref_file.suffix.lower() in self.image_extensions:
                        category = "character" if "character" in str(ref_file) else "location"
                        images.append({
                            "type": "reference",
                            "category": category,
                            "path": str(ref_file),
                            "name": ref_file.name,
                            "size": ref_file.stat().st_size,
                            "modified": ref_file.stat().st_mtime
                        })
            
            # Кадры для видео
            shots_dir = self.project_path / "97_shots"
            if shots_dir.exists():
                for shot_dir in shots_dir.iterdir():
                    if shot_dir.is_dir() and shot_dir.name.startswith("scene_"):
                        for img_file in shot_dir.iterdir():
                            if img_file.suffix.lower() in self.image_extensions:
                                images.append({
                                    "type": "shot",
                                    "category": "video_frame",
                                    "scene": shot_dir.name,
                                    "path": str(img_file),
                                    "name": img_file.name,
                                    "size": img_file.stat().st_size,
                                    "modified": img_file.stat().st_mtime
                                })
            
            # Сортируем по типу и дате изменения
            images.sort(key=lambda x: (x["type"], -x["modified"]))
            
        except Exception as e:
            logger.error(f"Ошибка получения списка изображений проекта {self.project_id}: {e}")
        
        return images
    
    def get_project_videos(self) -> List[Dict[str, Any]]:
        """Возвращает список всех видео в проекте"""
        videos = []
        
        try:
            shots_dir = self.project_path / "97_shots"
            if shots_dir.exists():
                for shot_dir in shots_dir.iterdir():
                    if shot_dir.is_dir() and shot_dir.name.startswith("scene_"):
                        for video_file in shot_dir.iterdir():
                            if video_file.suffix.lower() in self.video_extensions:
                                videos.append({
                                    "scene": shot_dir.name,
                                    "path": str(video_file),
                                    "name": video_file.name,
                                    "size": video_file.stat().st_size,
                                    "modified": video_file.stat().st_mtime,
                                    "duration": self._get_video_duration(video_file)
                                })
            
            # Сортируем по имени сцены
            videos.sort(key=lambda x: x["scene"])
            
        except Exception as e:
            logger.error(f"Ошибка получения списка видео проекта {self.project_id}: {e}")
        
        return videos
    
    def create_image_thumbnail(self, image_path: str, size: Tuple[int, int] = (150, 150)) -> Optional[str]:
        """
        Создает превью изображения
        
        Args:
            image_path: Путь к изображению
            size: Размер превью (ширина, высота)
        
        Returns:
            Путь к файлу превью или None в случае ошибки
        """
        if not PIL_AVAILABLE:
            logger.warning("PIL не доступен для создания превью")
            return None
        
        try:
            image_path = Path(image_path)
            if not image_path.exists():
                return None
            
            # Путь к файлу превью в кэше
            cache_name = f"{image_path.stem}_{size[0]}x{size[1]}.png"
            cache_path = self.cache_dir / "thumbnails" / cache_name
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Проверяем, существует ли уже превью и актуально ли оно
            if cache_path.exists():
                if cache_path.stat().st_mtime >= image_path.stat().st_mtime:
                    return str(cache_path)
            
            # Создаем превью
            with Image.open(image_path) as img:
                # Конвертируем в RGB если нужно
                if img.mode in ('RGBA', 'LA', 'P'):
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                    img = background
                
                # Создаем превью с сохранением пропорций
                img.thumbnail(size, Image.Resampling.LANCZOS)
                
                # Сохраняем превью
                img.save(cache_path, 'PNG')
            
            logger.debug(f"Создано превью: {cache_path}")
            return str(cache_path)
            
        except Exception as e:
            logger.error(f"Ошибка создания превью для {image_path}: {e}")
            return None
    
    def load_image_for_display(self, image_path: str, max_size: Tuple[int, int] = (800, 600)) -> Optional[tk.PhotoImage]:
        """
        Загружает изображение для отображения в tkinter
        
        Args:
            image_path: Путь к изображению
            max_size: Максимальный размер для отображения
        
        Returns:
            PhotoImage объект для tkinter или None в случае ошибки
        """
        if not PIL_AVAILABLE:
            logger.warning("PIL не доступен для загрузки изображений")
            return None
        
        try:
            image_path = Path(image_path)
            if not image_path.exists():
                return None
            
            with Image.open(image_path) as img:
                # Конвертируем в RGB если нужно
                if img.mode in ('RGBA', 'LA', 'P'):
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                    img = background
                
                # Изменяем размер если изображение слишком большое
                if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
                    img.thumbnail(max_size, Image.Resampling.LANCZOS)
                
                # Конвертируем для tkinter
                photo = ImageTk.PhotoImage(img)
                return photo
            
        except Exception as e:
            logger.error(f"Ошибка загрузки изображения {image_path}: {e}")
            return None
    
    def get_image_info(self, image_path: str) -> Dict[str, Any]:
        """Получает информацию об изображении"""
        info = {
            "path": image_path,
            "exists": False,
            "size": 0,
            "dimensions": None,
            "format": None,
            "mode": None
        }
        
        try:
            image_path = Path(image_path)
            if not image_path.exists():
                return info
            
            info["exists"] = True
            info["size"] = image_path.stat().st_size
            
            if PIL_AVAILABLE:
                try:
                    with Image.open(image_path) as img:
                        info["dimensions"] = img.size
                        info["format"] = img.format
                        info["mode"] = img.mode
                except Exception:
                    pass
            
        except Exception as e:
            logger.error(f"Ошибка получения информации об изображении {image_path}: {e}")
        
        return info
    
    def _get_video_duration(self, video_path: Path) -> Optional[float]:
        """Получает длительность видео в секундах"""
        if not CV2_AVAILABLE:
            return None
        
        try:
            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                return None
            try:
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            finally:
                cap.release()

            if fps > 0:
                duration = frame_count / fps
                return round(duration, 2)

        except Exception as e:
            logger.error(f"Ошибка получения длительности видео {video_path}: {e}")

        return None
    
    def get_video_thumbnail(self, video_path: str, time_offset: float = 1.0) -> Optional[str]:
        """
        Создает превью кадр из видео
        
        Args:
            video_path: Путь к видео
            time_offset: Время в секундах для извлечения кадра
        
        Returns:
            Путь к файлу превью или None в случае ошибки
        """
        if not CV2_AVAILABLE:
            logger.warning("OpenCV не доступен для создания превью видео")
            return None
        
        try:
            video_path = Path(video_path)
            if not video_path.exists():
                return None
            
            # Путь к файлу превью в кэше
            cache_name = f"{video_path.stem}_thumb.png"
            cache_path = self.cache_dir / "video_thumbnails" / cache_name
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Проверяем, существует ли уже превью и актуально ли оно
            if cache_path.exists():
                if cache_path.stat().st_mtime >= video_path.stat().st_mtime:
                    return str(cache_path)
            
            # Извлекаем кадр из видео
            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                return None

            ret, frame = False, None
            try:
                # Переходим к нужному времени
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_number = int(fps * time_offset)
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)

                # Читаем кадр
                ret, frame = cap.read()
            finally:
                cap.release()

            if not ret:
                return None

            # Сохраняем кадр как изображение
            cv2.imwrite(str(cache_path), frame)
            
            logger.debug(f"Создано превью видео: {cache_path}")
            return str(cache_path)
            
        except Exception as e:
            logger.error(f"Ошибка создания превью видео {video_path}: {e}")
            return None
    
    def cleanup_cache(self):
        """Очищает кэш медиа файлов"""
        try:
            import shutil
            if self.cache_dir.exists():
                shutil.rmtree(self.cache_dir)
                self.cache_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Кэш медиа файлов очищен для проекта {self.project_id}")
        except Exception as e:
            logger.error(f"Ошибка очистки кэша медиа файлов: {e}")
