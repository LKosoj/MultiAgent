"""
Управление файлами проекта
=========================

Модуль для чтения, записи и валидации JSON файлов проекта.
Включает систему бэкапов и валидацию данных.
"""

import copy
import fcntl
import json
import os
import shutil
import threading
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


# === Общий протокол записи 97_shots/shots.json (раздел 10.2 ТЗ, критерий A42) ===
#
# Все писатели shots.json обязаны сходиться на одном протоколе: flock на
# sidecar-файл {shots_path}.lock, перечитывание файла под захваченной
# блокировкой и слияние ПО ПОЛЯМ внутри элемента (а не замещение элемента
# целиком — это допустимо только пайплайн-шагам вроде
# screenplay_shots_generator, чьи "свежие" данные всегда только что
# посчитаны; у интерфейсных писателей "свежие" данные — это снимок,
# сделанный в момент открытия формы, поэтому наложение их целиком удалило бы
# конкурентные изменения других полей). Эталон протокола (flock/reread/tmp+
# os.replace) — custom_tools/storybook/screenplay_shots_generator.py::
# _merge_write_shots(); ключ слияния элемента продублирован здесь как
# маленькая чистая функция, чтобы не тянуть тяжёлый модуль пайплайна в GUI.

_MISSING = object()

# Внутрипроцессный лок, защищающий от гонки потоков этого процесса поверх
# межпроцессного fcntl.flock ниже — тот же приём, что и
# _SHOTS_WRITE_LOCK в custom_tools/storybook/screenplay_shots_generator.py::
# _merge_write_shots() (эталон протокола раздела 10.2).
_SHOTS_WRITE_LOCK = threading.Lock()


def _shot_merge_key(item: Dict[str, Any]) -> tuple:
    """Ключ слияния элемента shots.json: (scene_number, shot_number, shot_type).

    Совпадает с _shot_merge_key() в screenplay_shots_generator.py."""
    try:
        return (
            int(item.get("scene_number", 0)),
            int(item.get("shot_number", 0)),
            str(item.get("shot_type", "")),
        )
    except Exception:
        return (0, 0, str(item.get("shot_type", "")))


def _compute_shots_field_updates(
    fresh_items: List[Dict[str, Any]],
    baseline_items: Optional[List[Dict[str, Any]]],
):
    """Определяет, что изменилось в fresh_items относительно baseline_items.

    baseline_items — снимок items на момент, когда данные были прочитаны
    (load_json_file); fresh_items — то, что вызывающий код просит сохранить
    сейчас. Возвращает (field_updates, deleted_keys, fresh_by_key):
      - field_updates[key] — только реально изменённые поля элемента
        (для новых элементов — весь элемент целиком, сравнивать не с чем);
      - deleted_keys — ключи, присутствовавшие в baseline, но отсутствующие
        в fresh (элемент удалили в редакторе);
      - fresh_by_key — все элементы fresh по ключу (нужно, чтобы дописать
        на диск элементы, которых там ещё нет).

    Если baseline_items is None (save_json_file вызван без предшествующего
    load_json_file этим же FileManager — снимка нет), сравнивать не с чем:
    каждый элемент fresh считается изменённым целиком, ничего не удаляется.
    """
    fresh_by_key = {
        _shot_merge_key(item): item for item in fresh_items if isinstance(item, dict)
    }

    if baseline_items is None:
        field_updates = {key: dict(item) for key, item in fresh_by_key.items()}
        deleted_keys: set = set()
        return field_updates, deleted_keys, fresh_by_key

    baseline_by_key = {
        _shot_merge_key(item): item for item in baseline_items if isinstance(item, dict)
    }

    field_updates = {}
    for key, item in fresh_by_key.items():
        baseline_item = baseline_by_key.get(key)
        if baseline_item is None:
            field_updates[key] = dict(item)
        else:
            diff = {
                field: field_value
                for field, field_value in item.items()
                if baseline_item.get(field, _MISSING) != field_value
            }
            if diff:
                field_updates[key] = diff

    deleted_keys = set(baseline_by_key) - set(fresh_by_key)
    return field_updates, deleted_keys, fresh_by_key


def _compute_root_field_updates(
    fresh_data: Optional[Dict[str, Any]],
    baseline_data: Optional[Dict[str, Any]],
):
    """Определяет, что изменилось в полях документа верхнего уровня (кроме
    "items") относительно baseline_data — снимка документа на момент
    load_json_file. Симметрично _compute_shots_field_updates(), только на
    уровне документа, а не элемента.

    Возвращает (updated_fields, deleted_keys):
      - updated_fields — поля fresh_data (кроме "items"), которых нет в
        baseline_data или значение которых изменилось;
      - deleted_keys — ключи, присутствовавшие в baseline_data, но
        отсутствующие в fresh_data (пользователь удалил корневой ключ).

    Если baseline_data is None (снимка нет), сравнивать не с чем: все поля
    fresh_data считаются изменёнными, ничего не удаляется.
    """
    fresh_root = {
        key: value for key, value in fresh_data.items() if key != "items"
    } if isinstance(fresh_data, dict) else {}

    if baseline_data is None:
        return dict(fresh_root), set()

    baseline_root = {
        key: value for key, value in baseline_data.items() if key != "items"
    } if isinstance(baseline_data, dict) else {}

    updated_fields = {
        key: value
        for key, value in fresh_root.items()
        if baseline_root.get(key, _MISSING) != value
    }
    deleted_keys = set(baseline_root) - set(fresh_root)
    return updated_fields, deleted_keys


def _read_shots_json_best_effort(shots_path: Path) -> Dict[str, Any]:
    """Читает shots.json; отсутствие файла или битый JSON — это пустой документ."""
    try:
        with open(shots_path, "r", encoding="utf-8") as rf:
            raw = rf.read()
    except FileNotFoundError:
        return {}
    if not raw.strip():
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"⚠️ Не удалось перечитать {shots_path} для слияния — битый JSON")
        return {}
    return loaded if isinstance(loaded, dict) else {}


def merge_write_shots_json(
    shots_path: Path,
    fresh_items: List[Dict[str, Any]],
    baseline_items: Optional[List[Dict[str, Any]]],
    fresh_root: Optional[Dict[str, Any]] = None,
    baseline_root: Optional[Dict[str, Any]] = None,
) -> None:
    """Общий протокол записи shots.json для интерфейсных писателей (10.2, A42).

    flock на sidecar {shots_path}.lock -> перечитывание файла под захваченной
    блокировкой -> слияние по полям внутри элемента -> временный файл ->
    os.replace.

    Метаданные документа верхнего уровня (seed, consistency_rules и т.п.)
    сливаются симметрично items — по полям, относительно baseline_root
    (снимок документа на момент load_json_file): наложить только то, что
    реально изменилось в fresh_root, не трогая поля, которых fresh_root не
    касался (в т.ч. добавленные конкурентным писателем). fresh_root=None
    (по умолчанию) — вызывающий код не просит сливать корневые поля, они
    остаются как на диске (обратная совместимость для вызовов, которым нужен
    только items).
    """
    field_updates, deleted_keys, fresh_by_key = _compute_shots_field_updates(
        fresh_items, baseline_items
    )
    if fresh_root is not None:
        root_updates, deleted_root_keys = _compute_root_field_updates(fresh_root, baseline_root)
    else:
        root_updates, deleted_root_keys = {}, set()

    shots_path = Path(shots_path)
    shots_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = f"{shots_path}.lock"

    with _SHOTS_WRITE_LOCK:
        with open(lock_path, "a+", encoding="utf-8") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                on_disk = _read_shots_json_best_effort(shots_path)
                items = on_disk.get("items")
                if not isinstance(items, list):
                    items = []

                merged: List[Any] = []
                seen_keys = set()
                for item in items:
                    if isinstance(item, dict):
                        key = _shot_merge_key(item)
                        seen_keys.add(key)
                        if key in deleted_keys:
                            continue
                        updates = field_updates.get(key)
                        if updates:
                            item = {**item, **updates}
                    merged.append(item)

                for key, item in fresh_by_key.items():
                    if key not in seen_keys and key not in deleted_keys:
                        merged.append(item)

                for root_key in deleted_root_keys:
                    on_disk.pop(root_key, None)
                on_disk.update(root_updates)
                on_disk["items"] = merged

                tmp_path = f"{shots_path}.{os.getpid()}.tmp"
                try:
                    with open(tmp_path, "w", encoding="utf-8") as wf:
                        json.dump(on_disk, wf, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, shots_path)
                except Exception:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                    raise
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)


class FileManager:
    """Управление файлами проекта с валидацией и бэкапами"""
    
    def __init__(self, project_id: str):
        from custom_tools.storybook.project_paths import safe_storybook_project_dir

        self.project_id = project_id
        self.project_path = safe_storybook_project_dir(project_id)
        self.backup_dir = app_settings.get_backup_directory() / "files"

        # Создаем директорию для бэкапов файлов
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        # Кэшированный SchemaIntrospector (инициализируется лениво при первом вызове validate_data)
        self._schema_introspector = None

        # Снимок items из shots.json на момент последнего load_json_file("shots") —
        # базовая версия для слияния по полям в save_json_file (раздел 10.2 ТЗ).
        self._shots_load_snapshot: Optional[Dict[str, Any]] = None
    
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
            
            if file_type == "shots":
                self._shots_load_snapshot = copy.deepcopy(data) if isinstance(data, dict) else None

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

            if file_type == "shots":
                # Общий протокол записи shots.json (раздел 10.2 ТЗ, критерий A42):
                # слияние по полям относительно снимка, сделанного при загрузке —
                # см. merge_write_shots_json() выше. Корневые поля документа
                # (seed, consistency_rules и т.п.) сливаются так же — иначе
                # правка вкладки «Редактор» (Raw JSON) молча теряется.
                fresh_items = data.get("items", []) if isinstance(data, dict) else []
                baseline = self._shots_load_snapshot
                baseline_items = baseline.get("items") if isinstance(baseline, dict) else None
                fresh_root = data if isinstance(data, dict) else {}
                merge_write_shots_json(
                    file_path, fresh_items, baseline_items,
                    fresh_root=fresh_root, baseline_root=baseline,
                )
                self._shots_load_snapshot = copy.deepcopy(data) if isinstance(data, dict) else None
            else:
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
            "shots": "97_shots/shots.json",
            "asset_map": "93_blockout/asset_map.json",
            "scene_spec": "93_blockout/scene_spec.json",
            "chains": "93_blockout/chains.json"
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
