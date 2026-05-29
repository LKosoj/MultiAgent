"""
Настройки приложения StoryBook Manager
=====================================
"""

import os
import json
from pathlib import Path
from typing import Dict, Any

class AppSettings:
    """Управление настройками приложения"""
    
    def __init__(self):
        self.app_dir = Path(__file__).parent.parent
        self.config_file = self.app_dir / "config" / "settings.json"
        self.project_root = self.app_dir.parent
        
        # Значения по умолчанию
        self.defaults = {
            "projects_directory": str(self.project_root / "plots" / "storybooks"),
            "backup_directory": str(self.app_dir / "backups"),
            "logs_directory": str(self.app_dir / "logs"),
            "auto_save_interval": 30,  # секунды
            "max_backup_files": 10,
            "theme": "default",
            "window_geometry": "1200x800",
            "json_editor_mode": "structured",  # structured, tree, raw
            "enable_ai_assistant": True,
            "media_cache_size": 100,  # MB
            "video_preview_quality": "medium",
            "log_level": "INFO"
        }
        
        self.settings = self.load_settings()
    
    def load_settings(self) -> Dict[str, Any]:
        """Загружает настройки из файла или создает файл с настройками по умолчанию"""
        try:
            if self.config_file.exists():
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                    # Обновляем отсутствующие настройки значениями по умолчанию
                    for key, value in self.defaults.items():
                        if key not in settings:
                            settings[key] = value
                    return settings
            else:
                # Создаем файл настроек по умолчанию
                self.save_settings(self.defaults)
                return self.defaults.copy()
        except Exception as e:
            print(f"Ошибка загрузки настроек: {e}. Используются значения по умолчанию.")
            return self.defaults.copy()
    
    def save_settings(self, settings: Dict[str, Any] = None) -> bool:
        """Сохраняет настройки в файл"""
        try:
            # Создаем директорию config если её нет
            self.config_file.parent.mkdir(exist_ok=True)
            
            settings_to_save = settings or self.settings
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(settings_to_save, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"Ошибка сохранения настроек: {e}")
            return False
    
    def get(self, key: str, default=None):
        """Получает значение настройки"""
        return self.settings.get(key, default)
    
    def set(self, key: str, value: Any):
        """Устанавливает значение настройки"""
        self.settings[key] = value
    
    def get_projects_directory(self) -> Path:
        """Возвращает путь к директории проектов"""
        return Path(self.get("projects_directory"))
    
    def get_backup_directory(self) -> Path:
        """Возвращает путь к директории бэкапов"""
        return Path(self.get("backup_directory"))
    
    def get_logs_directory(self) -> Path:
        """Возвращает путь к директории логов"""
        return Path(self.get("logs_directory"))
    
    def ensure_directories(self):
        """Создает необходимые директории"""
        directories = [
            self.get_backup_directory(),
            self.get_logs_directory()
        ]
        
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

# Глобальный экземпляр настроек
app_settings = AppSettings()
