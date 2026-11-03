"""
Управление проектами StoryBook
============================

Модуль для работы с проектами storybook: загрузка, сохранение,
создание бэкапов, получение информации о проектах.
"""

import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
import logging

from StoryBookManager.config.settings import app_settings
from custom_tools.storybook.studio import project_store
from custom_tools.storybook.studio.errors import StudioError

logger = logging.getLogger(__name__)


class Project:
    """Класс представляющий один проект storybook"""
    
    def __init__(self, project_id: str, project_path: Path):
        self.project_id = project_id
        self.project_path = project_path
        self.name = project_id
        self.created_date = None
        self.modified_date = None
        self.brief_data = None
        
        self._load_project_info()
    
    def _load_project_info(self):
        """Загружает основную информацию о проекте"""
        try:
            # Получаем даты создания и изменения
            if self.project_path.exists():
                stat = self.project_path.stat()
                self.created_date = datetime.fromtimestamp(stat.st_ctime)
                self.modified_date = datetime.fromtimestamp(stat.st_mtime)
            
            # Загружаем brief для получения названия и описания
            brief_file = self.project_path / "00_brief.json"
            if brief_file.exists():
                with open(brief_file, 'r', encoding='utf-8') as f:
                    self.brief_data = json.load(f)
                    self.name = self.brief_data.get('title', self.project_id)
        except Exception as e:
            logger.warning(f"Не удалось загрузить информацию о проекте {self.project_id}: {e}")
    
    def get_files_structure(self) -> Dict[str, Any]:
        """Возвращает структуру файлов проекта"""
        structure = {}
        
        if not self.project_path.exists():
            return structure
            
        try:
            # Основные JSON файлы
            json_files = {
                "brief": "00_brief.json",
                "synopsis": "10_synopsis/synopsis.json",
                "beats": "10_synopsis/beats.json",
                "characters": "20_bible/characters.json",
                "locations": "20_bible/locations.json",
                "consistency_rules": "20_bible/consistency_rules.json",
                "story": "20_story/story.json",
                "style_text": "30_style/style_text.json",
                "style_images": "30_style/style_images.json",
                "screenplay": "91_screenplay/screenplay.json",
                "shots": "97_shots/shots.json",
                "asset_map": "93_blockout/asset_map.json",
                "scene_spec": "93_blockout/scene_spec.json",
                "chains": "93_blockout/chains.json"
            }

            structure["json_files"] = {}
            for key, relative_path in json_files.items():
                file_path = self.project_path / relative_path
                structure["json_files"][key] = {
                    "path": str(file_path),
                    "exists": file_path.exists(),
                    "size": file_path.stat().st_size if file_path.exists() else 0,
                    "modified": datetime.fromtimestamp(file_path.stat().st_mtime) if file_path.exists() else None
                }
            
            # Директории с медиа файлами
            media_dirs = {
                "images": "50_images",
                "references": "20_bible/references",
                "shots": "97_shots"
            }
            
            structure["media_dirs"] = {}
            for key, relative_path in media_dirs.items():
                dir_path = self.project_path / relative_path
                if dir_path.exists():
                    files = []
                    for ext in ['.png', '.jpg', '.jpeg', '.mp4', '.avi']:
                        files.extend(f for f in dir_path.rglob(f"*{ext}")
                                     if "_versions" not in f.relative_to(dir_path).parts)
                    structure["media_dirs"][key] = {
                        "path": str(dir_path),
                        "files_count": len(files),
                        "files": [str(f.relative_to(dir_path)) for f in files[:10]]  # Первые 10 файлов
                    }
                else:
                    structure["media_dirs"][key] = {
                        "path": str(dir_path),
                        "files_count": 0,
                        "files": []
                    }
            
            # Результирующие файлы
            result_files = {
                "pdf": "95_pdf/book.pdf",
                "markdown": "90_md/book.md"
            }
            
            structure["result_files"] = {}
            for key, relative_path in result_files.items():
                file_path = self.project_path / relative_path
                structure["result_files"][key] = {
                    "path": str(file_path),
                    "exists": file_path.exists(),
                    "size": file_path.stat().st_size if file_path.exists() else 0
                }
                
        except Exception as e:
            logger.error(f"Ошибка получения структуры файлов проекта {self.project_id}: {e}")
            
        return structure
    
    def get_preview_info(self) -> Dict[str, Any]:
        """Возвращает информацию для превью проекта"""
        info = {
            "project_id": self.project_id,
            "name": self.name,
            "created_date": self.created_date,
            "modified_date": self.modified_date,
            "description": "",
            "characters_count": 0,
            "pages_count": 0,
            "has_pdf": False,
            "has_video": False,
            "thumbnail": None
        }
        
        try:
            # Информация из brief
            if self.brief_data:
                info["description"] = self.brief_data.get("description", "")[:100] + "..."
                info["characters_count"] = len(self.brief_data.get("main_characters", []))
            
            # Количество страниц из story
            story_file = self.project_path / "20_story/story.json"
            if story_file.exists():
                with open(story_file, 'r', encoding='utf-8') as f:
                    story_data = json.load(f)
                    info["pages_count"] = len(story_data.get("pages", []))
            
            # Наличие результирующих файлов
            pdf_file = self.project_path / "95_pdf/book.pdf"
            info["has_pdf"] = pdf_file.exists()
            
            shots_dir = self.project_path / "97_shots"
            if shots_dir.exists():
                # версии шотов (_versions) не считаются готовым видео
                video_files = [f for f in shots_dir.rglob("*.mp4") if "_versions" not in f.relative_to(shots_dir).parts]
                info["has_video"] = len(video_files) > 0
            
            # Thumbnail - обложка проекта (общее правило studio)
            cover = project_store.pick_cover(self.project_path)
            if cover:
                info["thumbnail"] = str(self.project_path / cover)
                    
        except Exception as e:
            logger.warning(f"Ошибка получения превью для проекта {self.project_id}: {e}")
            
        return info


class ProjectManager:
    """Управление проектами storybook"""

    PROJECT_STRUCTURE = project_store.PROJECT_STRUCTURE

    def __init__(self):
        from custom_tools.storybook.project_paths import storybook_projects_root

        self.projects_dir = storybook_projects_root()
        self.backup_dir = app_settings.get_backup_directory()
        
        # Создаем необходимые директории
        app_settings.ensure_directories()

    def generate_project_id(self, base_name: str = "") -> str:
        """Генерирует уникальный project_id на основе названия или подсказки."""
        return project_store.suggest_project_id(base_name, self.projects_dir)

    def create_project(
        self,
        title: str,
        description: str,
        genre: str,
        target_age: str,
        language: str = "ru",
        pages_min: int = 9,
        pages_max: int = 12,
        words_per_page_min: int = 400,
        words_per_page_max: int = 450,
        project_id_hint: str = "",
    ) -> Project:
        """Создает новый проект с директорией и заполненным 00_brief.json."""
        project_id = project_store.create_project(
            self.projects_dir,
            title=title,
            description=description,
            genre=genre,
            target_age=target_age,
            language=language,
            pages_min=pages_min,
            pages_max=pages_max,
            words_per_page_min=words_per_page_min,
            words_per_page_max=words_per_page_max,
            id_hint=project_id_hint,
        )
        return Project(project_id, self.projects_dir / project_id)
    
    def list_projects(self) -> List[Project]:
        """Возвращает список всех проектов"""
        projects = []
        
        try:
            if not self.projects_dir.exists():
                logger.warning(f"Директория проектов не найдена: {self.projects_dir}")
                return projects
            
            for item in project_store.list_project_dirs(self.projects_dir):
                try:
                    projects.append(Project(item.name, item))
                except Exception as e:
                    logger.warning(f"Ошибка загрузки проекта {item.name}: {e}")
            
            # Сортируем по дате изменения (новые сверху)
            projects.sort(key=lambda p: p.modified_date or datetime.min, reverse=True)
            
        except Exception as e:
            logger.error(f"Ошибка получения списка проектов: {e}")
            
        return projects
    
    def load_project(self, project_id: str) -> Optional[Project]:
        """Загружает конкретный проект"""
        try:
            project_path = self.projects_dir / project_id
            # Защита от path traversal через project_id
            try:
                project_path.resolve().relative_to(self.projects_dir.resolve())
            except ValueError:
                logger.error(f"Небезопасный project_id: {project_id}")
                return None
            if project_path.exists():
                return Project(project_id, project_path)
            else:
                logger.warning(f"Проект {project_id} не найден")
                return None
        except Exception as e:
            logger.error(f"Ошибка загрузки проекта {project_id}: {e}")
            return None
    
    def backup_project(self, project_id: str) -> Optional[str]:
        """Создает бэкап проекта"""
        try:
            info = project_store.create_backup(
                self.projects_dir, self.backup_dir, project_id,
                max_backups=app_settings.get("max_backup_files", 10),
            )
            return info["path"]
        except StudioError as e:
            logger.error(f"Не удалось создать бэкап проекта {project_id}: {e.message}")
            return None
        except Exception as e:
            logger.error(f"Ошибка создания бэкапа проекта {project_id}: {e}")
            return None

    def export_project(self, project_id: str, output_path: str, progress_callback=None) -> bool:
        """Экспортирует проект в ZIP-архив"""
        try:
            project_path = self.projects_dir / project_id
            if not project_path.exists():
                logger.warning(f"Проект {project_id} не найден для экспорта")
                return False

            def on_progress(current, total, _message=""):
                if progress_callback:
                    progress_callback(current, total)

            project_store.export_zip(project_path, Path(output_path), on_progress)
            logger.info(f"Проект {project_id} экспортирован в {output_path}")
            return True
        except StudioError as e:
            logger.warning(f"Проект {project_id} не экспортирован: {e.message}")
            return False
        except Exception as e:
            logger.error(f"Ошибка экспорта проекта {project_id}: {e}")
            return False
    
    def _cleanup_old_backups(self, project_id: str):
        """Удаляет старые бэкапы, оставляя только последние N"""
        project_store.cleanup_backups(self.backup_dir, project_id, app_settings.get("max_backup_files", 10))
    
    def delete_project(self, project_id: str, create_backup: bool = True) -> bool:
        """Удаляет проект (бэкап делается всегда, create_backup оставлен для совместимости)"""
        try:
            project_store.delete_project(
                self.projects_dir, self.backup_dir, project_id,
                max_backups=app_settings.get("max_backup_files", 10),
            )
            return True
        except StudioError as e:
            logger.error(f"Проект {project_id} не удалён: {e.message}")
            return False
        except Exception as e:
            logger.error(f"Ошибка удаления проекта {project_id}: {e}")
            return False
    
    def get_project_files(self, project_id: str) -> Dict[str, str]:
        """Возвращает пути к основным файлам проекта"""
        project_path = self.projects_dir / project_id
        # Защита от path traversal через project_id (L56)
        try:
            project_path.resolve().relative_to(self.projects_dir.resolve())
        except ValueError:
            logger.error(f"Небезопасный project_id: {project_id}")
            return {}

        files = {
            "brief": str(project_path / "00_brief.json"),
            "synopsis": str(project_path / "10_synopsis/synopsis.json"),
            "beats": str(project_path / "10_synopsis/beats.json"),
            "characters": str(project_path / "20_bible/characters.json"),
            "locations": str(project_path / "20_bible/locations.json"),
            "consistency_rules": str(project_path / "20_bible/consistency_rules.json"),
            "story": str(project_path / "20_story/story.json"),
            "style_text": str(project_path / "30_style/style_text.json"),
            "style_images": str(project_path / "30_style/style_images.json"),
            "negative_prompt_list": str(project_path / "30_style/negative_prompt_list.txt"),
            "screenplay": str(project_path / "91_screenplay/screenplay.json"),
            "shots": str(project_path / "97_shots/shots.json"),
            "asset_map": str(project_path / "93_blockout/asset_map.json"),
            "scene_spec": str(project_path / "93_blockout/scene_spec.json"),
            "chains": str(project_path / "93_blockout/chains.json"),
            "pdf": str(project_path / "95_pdf/book.pdf"),
            "markdown": str(project_path / "90_md/book.md")
        }
        
        return files
