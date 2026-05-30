"""
Управление файлами проекта
=========================

Модуль для чтения, записи и валидации JSON файлов проекта.
Включает систему бэкапов и валидацию данных.
"""

import json
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime
import logging

import jsonschema
from jsonschema import ValidationError

# Удален импорт schemas.py - теперь используется гибридная генерация схем
# from config.schemas import SCHEMA_MAPPING
from config.settings import app_settings

logger = logging.getLogger(__name__)


class FileManager:
    """Управление файлами проекта с валидацией и бэкапами"""
    
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.projects_dir = app_settings.get_projects_directory()
        self.project_path = self.projects_dir / project_id
        self.backup_dir = app_settings.get_backup_directory() / "files"

        # Создаем директорию для бэкапов файлов
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        # Кэшированный SchemaIntrospector (инициализируется лениво при первом вызове validate_data)
        self._schema_introspector = None
    
    def load_json_file(self, file_type: str, file_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Загружает JSON файл с валидацией
        
        Args:
            file_type: Тип файла (brief, story, characters, etc.)
            file_path: Путь к файлу (если не указан, используется стандартный)
        
        Returns:
            Словарь с данными или None в случае ошибки
        """
        try:
            if file_path is None:
                file_path = self._get_default_file_path(file_type)
            
            file_path = Path(file_path)
            
            if not file_path.exists():
                logger.warning(f"Файл {file_path} не существует")
                return None
            
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Валидация данных
            validation_errors = self.validate_data(data, file_type)
            if validation_errors:
                logger.warning(f"Файл {file_path} содержит ошибки валидации: {validation_errors}")
                # Все равно возвращаем данные, но с предупреждением
            
            logger.debug(f"Загружен файл {file_path}")
            return data
            
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка парсинга JSON в файле {file_path}: {e}")
            return None
        except Exception as e:
            logger.error(f"Ошибка загрузки файла {file_path}: {e}")
            return None
    
    def save_json_file(self, data: Dict[str, Any], file_type: str, 
                      file_path: Optional[str] = None, create_backup: bool = True) -> bool:
        """
        Сохраняет JSON файл с валидацией и бэкапом
        
        Args:
            data: Данные для сохранения
            file_type: Тип файла для валидации
            file_path: Путь к файлу (если не указан, используется стандартный)
            create_backup: Создавать ли бэкап перед сохранением
        
        Returns:
            True если файл сохранен успешно
        """
        try:
            if file_path is None:
                file_path = self._get_default_file_path(file_type)
            
            file_path = Path(file_path)
            
            # Валидация данных перед сохранением
            validation_errors = self.validate_data(data, file_type)
            if validation_errors:
                logger.error(f"Данные не прошли валидацию: {validation_errors}")
                return False
            
            # Создаем бэкап существующего файла
            if create_backup and file_path.exists():
                backup_path = self._create_file_backup(file_path)
                if backup_path:
                    logger.debug(f"Создан бэкап файла: {backup_path}")
            
            # Создаем директорию если её нет
            file_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Сохраняем файл
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"Файл сохранен: {file_path}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка сохранения файла {file_path}: {e}")
            return False
    
    def validate_data(self, data: Dict[str, Any], file_type: str) -> List[str]:
        """
        Валидация данных с гибридной генерацией схем
        
        Args:
            data: Данные для валидации
            file_type: Тип файла для выбора схемы
        
        Returns:
            Список ошибок валидации (пустой если ошибок нет)
        """
        errors = []
        
        try:
            # Используем гибридную генерацию схем вместо predefined schemas
            from gui.universal_json_editor import generate_hybrid_schema, SchemaIntrospector

            # Кэшируем SchemaIntrospector чтобы не перечитывать ui_config.json на каждый вызов
            if self._schema_introspector is None:
                self._schema_introspector = SchemaIntrospector()
            introspector = self._schema_introspector
            ui_config = introspector.ui_config
            
            # Генерируем схему из данных
            schema = generate_hybrid_schema(ui_config, data, file_type)
            
            if schema is None:
                logger.warning(f"Не удалось сгенерировать схему для типа '{file_type}'")
                return []  # Пропускаем валидацию, если схему нельзя сгенерировать
            
            jsonschema.validate(data, schema)
            logger.debug(f"Валидация данных типа '{file_type}' прошла успешно")
            
        except ValidationError as e:
            error_msg = f"Ошибка валидации: {e.message}"
            if e.absolute_path:
                error_msg += f" в поле {'.'.join(str(x) for x in e.absolute_path)}"
            errors.append(error_msg)
            logger.warning(error_msg)
            
        except ImportError:
            # Если модули недоступны, пропускаем валидацию
            logger.warning(f"Модули валидации недоступны, пропускаем валидацию для '{file_type}'")
            return []
            
        except Exception as e:
            error_msg = f"Ошибка процесса валидации: {e}"
            errors.append(error_msg)
            logger.error(error_msg)
        
        return errors
    
    def _get_default_file_path(self, file_type: str) -> str:
        """Возвращает стандартный путь к файлу по его типу"""
        file_mapping = {
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
            "shots": "97_shots/shots.json"
        }
        
        relative_path = file_mapping.get(file_type)
        if relative_path is None:
            raise ValueError(f"Неизвестный тип файла: {file_type}")
        
        return str(self.project_path / relative_path)
    
    def _create_file_backup(self, file_path: Path) -> Optional[Path]:
        """Создает бэкап файла"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"{self.project_id}_{file_path.stem}_{timestamp}{file_path.suffix}"
            backup_path = self.backup_dir / backup_name
            
            shutil.copy2(file_path, backup_path)
            
            # Удаляем старые бэкапы этого файла
            self._cleanup_file_backups(file_path.stem)
            
            return backup_path
            
        except Exception as e:
            logger.error(f"Ошибка создания бэкапа файла {file_path}: {e}")
            return None
    
    def _cleanup_file_backups(self, file_stem: str):
        """Удаляет старые бэкапы файла, оставляя только последние N"""
        try:
            max_backups = app_settings.get("max_backup_files", 10)
            
            # Находим все бэкапы данного файла
            backup_pattern = f"{self.project_id}_{file_stem}_*"
            backups = list(self.backup_dir.glob(backup_pattern))
            
            # Сортируем по дате создания
            backups.sort(key=lambda p: p.stat().st_ctime, reverse=True)
            
            # Удаляем старые бэкапы
            for backup in backups[max_backups:]:
                backup.unlink()
                logger.debug(f"Удален старый бэкап файла: {backup}")
                
        except Exception as e:
            logger.warning(f"Ошибка очистки старых бэкапов файла: {e}")
    
    def get_edit_history(self, file_type: str) -> List[Dict[str, Any]]:
        """Возвращает историю изменений файла"""
        try:
            file_path = Path(self._get_default_file_path(file_type))
            file_stem = file_path.stem
            
            # Находим все бэкапы данного файла
            backup_pattern = f"{self.project_id}_{file_stem}_*"
            backups = list(self.backup_dir.glob(backup_pattern))
            
            # Сортируем по дате создания (новые сверху)
            backups.sort(key=lambda p: p.stat().st_ctime, reverse=True)
            
            history = []
            for backup in backups:
                stat = backup.stat()
                
                # Извлекаем timestamp из имени файла
                parts = backup.stem.split('_')
                if len(parts) >= 3:
                    timestamp_str = '_'.join(parts[-2:])  # Последние две части - дата и время
                    try:
                        timestamp = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
                    except ValueError:
                        timestamp = datetime.fromtimestamp(stat.st_ctime)
                else:
                    timestamp = datetime.fromtimestamp(stat.st_ctime)
                
                history.append({
                    "backup_path": str(backup),
                    "timestamp": timestamp,
                    "size": stat.st_size,
                    "created": datetime.fromtimestamp(stat.st_ctime)
                })
            
            return history
            
        except Exception as e:
            logger.error(f"Ошибка получения истории изменений файла {file_type}: {e}")
            return []
    
    def restore_from_backup(self, file_type: str, backup_path: str) -> bool:
        """Восстанавливает файл из бэкапа"""
        try:
            backup_path = Path(backup_path)
            target_path = Path(self._get_default_file_path(file_type))
            
            if not backup_path.exists():
                logger.error(f"Бэкап файл не найден: {backup_path}")
                return False

            # Проверяем, что backup_path находится внутри backup_dir
            try:
                backup_path.resolve().relative_to(self.backup_dir.resolve())
            except ValueError:
                logger.error(f"Небезопасный путь бэкапа: {backup_path} не принадлежит {self.backup_dir}")
                return False

            # Создаем бэкап текущего файла перед восстановлением
            if target_path.exists():
                current_backup = self._create_file_backup(target_path)
                if current_backup:
                    logger.info(f"Создан бэкап текущего файла перед восстановлением: {current_backup}")
            
            # Копируем бэкап на место оригинального файла
            shutil.copy2(backup_path, target_path)
            
            logger.info(f"Файл восстановлен из бэкапа: {backup_path} -> {target_path}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка восстановления файла из бэкапа: {e}")
            return False
