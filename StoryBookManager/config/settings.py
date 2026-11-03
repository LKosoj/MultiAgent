"""
Настройки приложения StoryBook Manager
=====================================
"""

import copy
import os
import json
import logging
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)

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
        
        # Исходные значения путей, подменённых в памяти: при сохранении
        # без явной правки на диск возвращаются они, а не значения по умолчанию
        self._raw_replaced: Dict[str, Any] = {}
        # Что было в памяти после загрузки или последнего сохранения: по отличиям от этого
        # видно, какие ключи изменил Tk, и только они пишутся поверх свежего файла
        self._baseline: Dict[str, Any] = {}
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
                    self._replace_missing_paths(settings)
                    self._baseline = copy.deepcopy(settings)
                    return settings
            else:
                # Создаем файл настроек по умолчанию
                self.save_settings(self.defaults)
                return self.defaults.copy()
        except Exception as e:
            print(f"Ошибка загрузки настроек: {e}. Используются значения по умолчанию.")
            return self.defaults.copy()
    
    def _replace_missing_paths(self, settings: Dict[str, Any]) -> None:
        """Подменяет в памяти несуществующие пути значениями по умолчанию (файл не трогаем)"""
        self._raw_replaced.clear()
        for key in ("projects_directory", "backup_directory", "logs_directory"):
            value = settings.get(key)
            try:
                path = Path(str(value)).expanduser()
                if not path.is_absolute():
                    path = self.project_root / path
                if key == "projects_directory":
                    ok = path.is_dir()
                else:
                    # Одну недостающую папку создать безопасно, чужое дерево - нет;
                    # папка должна быть ещё и доступна на запись (туда пишутся бэкапы и логи)
                    base = path if path.exists() else path.parent
                    ok = base.exists() and os.access(base, os.W_OK)
            except (TypeError, ValueError, OSError):
                ok = False
            if ok:
                if key == "projects_directory" and str(path) != value:
                    # Нормализованный путь только в памяти, на диск вернётся исходный
                    self._raw_replaced[key] = (value, str(path))
                    settings[key] = str(path)
                continue
            logger.warning("Путь %s=%s не найден, используется %s", key, value, self.defaults[key])
            self._raw_replaced[key] = (value, self.defaults[key])
            settings[key] = self.defaults[key]

    def save_settings(self, settings: Dict[str, Any] = None) -> bool:
        """Сохраняет настройки: под блокировкой пишет поверх свежего файла только изменённые ключи"""
        try:
            # Создаем директорию config если её нет
            self.config_file.parent.mkdir(exist_ok=True)

            settings_to_save = dict(settings or self.settings)
            # Подменённый путь, который пользователь не менял, пишем как было
            for key, (raw, substituted) in self._raw_replaced.items():
                if settings_to_save.get(key) == substituted:
                    settings_to_save[key] = raw
            # Переданные явно настройки пишем целиком, иначе - только то, что Tk изменил
            if settings is None:
                changed = {k: v for k, v in settings_to_save.items()
                           if k not in self._baseline or self.settings.get(k) != self._baseline[k]}
            else:
                changed = settings_to_save
            lock = open(self.config_file.with_name(self.config_file.name + ".lock"), "a+", encoding="utf-8")
            try:
                try:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                except ImportError:
                    pass
                fresh: Dict[str, Any] = {}
                try:
                    loaded = json.loads(self.config_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        fresh = loaded
                except (OSError, ValueError):
                    pass
                if not fresh:
                    changed = settings_to_save  # файла нет или он испорчен - пишем всё
                fresh.update(changed)
                tmp = self.config_file.with_name(f".{self.config_file.name}.{os.getpid()}.tmp")
                try:
                    tmp.write_text(json.dumps(fresh, indent=2, ensure_ascii=False), encoding="utf-8")
                    os.replace(tmp, self.config_file)
                finally:
                    if tmp.exists():
                        tmp.unlink()
            finally:
                lock.close()
            self._baseline = copy.deepcopy(getattr(self, 'settings', settings_to_save))
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
        # Явно заданное значение больше не считается подменённым
        self._raw_replaced.pop(key, None)
    
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
