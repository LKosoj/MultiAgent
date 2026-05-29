"""
Валидация JSON данных
====================

Утилиты для валидации и проверки целостности JSON файлов проекта.
"""

import json
from typing import Dict, Any, List, Optional
from pathlib import Path
import logging

import jsonschema
from jsonschema import ValidationError

# Удален импорт schemas.py - теперь используется гибридная генерация схем
# from config.schemas import SCHEMA_MAPPING

logger = logging.getLogger(__name__)


class JSONValidator:
    """Валидатор JSON данных с расширенными проверками"""
    
    def __init__(self):
        # Больше не используем predefined schemas - переходим на гибридную генерацию
        # self.schemas = SCHEMA_MAPPING
        pass
    
    def validate_file(self, file_path: str, schema_type: str) -> Dict[str, Any]:
        """
        Валидирует JSON файл
        
        Args:
            file_path: Путь к файлу
            schema_type: Тип схемы для валидации
        
        Returns:
            Результат валидации с ошибками и предупреждениями
        """
        result = {
            "valid": True,
            "errors": [],
            "warnings": [],
            "suggestions": []
        }
        
        try:
            file_path = Path(file_path)
            
            if not file_path.exists():
                result["valid"] = False
                result["errors"].append(f"Файл не существует: {file_path}")
                return result
            
            # Загружаем и парсим JSON
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except json.JSONDecodeError as e:
                result["valid"] = False
                result["errors"].append(f"Ошибка парсинга JSON: {e}")
                return result
            
            # Валидация по схеме с гибридной генерацией
            schema_errors = self.validate_data(data, schema_type)
            if schema_errors:
                result["valid"] = False
                result["errors"].extend(schema_errors)
            
            # Дополнительные проверки
            warnings, suggestions = self._additional_checks(data, schema_type)
            result["warnings"].extend(warnings)
            result["suggestions"].extend(suggestions)
            
        except Exception as e:
            result["valid"] = False
            result["errors"].append(f"Ошибка валидации: {e}")
            logger.error(f"Ошибка валидации файла {file_path}: {e}")
        
        return result
    
    def validate_data(self, data: Dict[str, Any], schema_type: str) -> List[str]:
        """
        Валидирует данные с использованием гибридной генерации схем
        
        Args:
            data: Данные для валидации
            schema_type: Тип схемы
        
        Returns:
            Список ошибок валидации
        """
        errors = []
        
        try:
            # Используем гибридную генерацию схем вместо predefined schemas
            from gui.universal_json_editor import generate_hybrid_schema, SchemaIntrospector
            
            # Получаем UI config
            introspector = SchemaIntrospector()
            ui_config = introspector.ui_config
            
            # Генерируем схему из данных
            schema = generate_hybrid_schema(ui_config, data, schema_type)
            
            if schema is None:
                logger.warning(f"Не удалось сгенерировать схему для типа '{schema_type}'")
                return []  # Пропускаем валидацию, если схему нельзя сгенерировать
            
            jsonschema.validate(data, schema)
            
        except ValidationError as e:
            error_msg = f"Ошибка валидации: {e.message}"
            if e.absolute_path:
                field_path = '.'.join(str(x) for x in e.absolute_path)
                error_msg += f" в поле '{field_path}'"
            errors.append(error_msg)
            
        except ImportError:
            # Если модули недоступны, пропускаем валидацию
            logger.warning(f"Модули валидации недоступны, пропускаем валидацию для '{schema_type}'")
            return []
            
        except Exception as e:
            errors.append(f"Ошибка процесса валидации: {e}")
        
        return errors
    
    def _additional_checks(self, data: Dict[str, Any], schema_type: str) -> tuple[List[str], List[str]]:
        """
        Дополнительные проверки специфичные для типа данных
        
        Returns:
            Кортеж (предупреждения, предложения)
        """
        warnings = []
        suggestions = []
        
        if schema_type == "brief":
            warnings_brief, suggestions_brief = self._check_brief(data)
            warnings.extend(warnings_brief)
            suggestions.extend(suggestions_brief)
            
        elif schema_type == "story":
            warnings_story, suggestions_story = self._check_story(data)
            warnings.extend(warnings_story)
            suggestions.extend(suggestions_story)
            
        elif schema_type == "characters":
            warnings_chars, suggestions_chars = self._check_characters(data)
            warnings.extend(warnings_chars)
            suggestions.extend(suggestions_chars)
            
        elif schema_type == "shots":
            warnings_shots, suggestions_shots = self._check_shots(data)
            warnings.extend(warnings_shots)
            suggestions.extend(suggestions_shots)
        
        return warnings, suggestions
    
    def _check_brief(self, data: Dict[str, Any]) -> tuple[List[str], List[str]]:
        """Проверки для файла brief"""
        warnings = []
        suggestions = []
        
        # Проверка длины описания
        description = data.get("description", "")
        if len(description) < 50:
            warnings.append("Описание слишком короткое (менее 50 символов)")
            suggestions.append("Добавьте более подробное описание сюжета и персонажей")
        
        # Проверка количества персонажей
        characters = data.get("main_characters", [])
        if len(characters) > 6:
            warnings.append("Слишком много главных персонажей (больше 6)")
            suggestions.append("Рассмотрите возможность сделать некоторых персонажей второстепенными")
        elif len(characters) < 1:
            warnings.append("Нет главных персонажей")
        
        # Проверка соответствия возраста и количества слов
        target_age = data.get("target_age", "")
        words_per_page_max = data.get("words_per_page_max", 0)
        
        if "3-5" in target_age and words_per_page_max > 150:
            suggestions.append("Для возраста 3-5 лет рекомендуется до 150 слов на страницу")
        elif "6-8" in target_age and words_per_page_max > 300:
            suggestions.append("Для возраста 6-8 лет рекомендуется до 300 слов на страницу")
        
        return warnings, suggestions
    
    def _check_story(self, data: Dict[str, Any]) -> tuple[List[str], List[str]]:
        """Проверки для файла story"""
        warnings = []
        suggestions = []
        
        pages = data.get("pages", [])
        
        # Проверка количества слов на страницах
        for page in pages:
            page_num = page.get("page", 0)
            body = page.get("body", "")
            word_count = len(body.split())
            
            if word_count < 50:
                warnings.append(f"Страница {page_num}: слишком мало текста ({word_count} слов)")
            elif word_count > 500:
                warnings.append(f"Страница {page_num}: слишком много текста ({word_count} слов)")
                suggestions.append(f"Разделите содержимое страницы {page_num} на несколько страниц")
        
        # Проверка структуры сюжета
        if len(pages) > 0:
            first_page = pages[0].get("body", "")
            if not any(word in first_page.lower() for word in ["жил", "жила", "однажды", "давным-давно"]):
                suggestions.append("Рассмотрите добавление классического зачина сказки")
        
        return warnings, suggestions
    
    def _check_characters(self, data: List[Dict[str, Any]]) -> tuple[List[str], List[str]]:
        """Проверки для файла characters"""
        warnings = []
        suggestions = []
        
        # Проверка наличия главного героя
        main_heroes = [char for char in data if char.get("role") in ["главный герой", "главная героиня"]]
        if len(main_heroes) == 0:
            warnings.append("Нет главного героя или героини")
        elif len(main_heroes) > 2:
            warnings.append("Слишком много главных героев (больше 2)")
        
        # Проверка уникальности имен
        names = [char.get("name", "") for char in data]
        duplicate_names = set([name for name in names if names.count(name) > 1])
        if duplicate_names:
            warnings.append(f"Дублирующиеся имена персонажей: {', '.join(duplicate_names)}")
        
        # Проверка полноты описания персонажей
        for char in data:
            name = char.get("name", "Неизвестный")
            immutable_attrs = char.get("immutable_attributes", {})
            
            required_attrs = ["face_shape", "eye_color", "body_proportions"]
            missing_attrs = [attr for attr in required_attrs if not immutable_attrs.get(attr)]
            
            if missing_attrs:
                warnings.append(f"Персонаж '{name}': отсутствуют атрибуты {', '.join(missing_attrs)}")
        
        return warnings, suggestions
    
    def _check_shots(self, data: Dict[str, Any]) -> tuple[List[str], List[str]]:
        """Проверки для файла shots"""
        warnings = []
        suggestions = []
        
        items = data.get("items", [])
        
        # Проверка последовательности кадров
        scenes = {}
        for item in items:
            scene_num = item.get("scene_number", 0)
            shot_num = item.get("shot_number", 0)
            
            if scene_num not in scenes:
                scenes[scene_num] = []
            scenes[scene_num].append(shot_num)
        
        # Проверка пропусков в нумерации
        for scene_num, shots in scenes.items():
            shots.sort()
            expected_shots = list(range(1, len(shots) + 1))
            if shots != expected_shots:
                warnings.append(f"Сцена {scene_num}: нарушена последовательность кадров")
        
        # Проверка длительности кадров
        for item in items:
            timing = item.get("timing", "")
            if timing:
                try:
                    start_time, end_time = timing.split("-")
                    start_seconds = self._time_to_seconds(start_time.strip())
                    end_seconds = self._time_to_seconds(end_time.strip())
                    duration = end_seconds - start_seconds
                    
                    if duration < 1:
                        scene_num = item.get("scene_number", 0)
                        shot_num = item.get("shot_number", 0)
                        warnings.append(f"Кадр {scene_num}-{shot_num}: слишком короткая длительность ({duration}с)")
                    elif duration > 10:
                        scene_num = item.get("scene_number", 0)
                        shot_num = item.get("shot_number", 0)
                        suggestions.append(f"Кадр {scene_num}-{shot_num}: длинный кадр ({duration}с), рассмотрите разделение")
                        
                except ValueError:
                    pass  # Неправильный формат времени уже будет отловлен схемой
        
        return warnings, suggestions
    
    def _time_to_seconds(self, time_str: str) -> float:
        """Конвертирует время в формате MM:SS в секунды"""
        try:
            parts = time_str.split(":")
            minutes = int(parts[0])
            seconds = int(parts[1])
            return minutes * 60 + seconds
        except (ValueError, IndexError):
            return 0.0
    
    def validate_project_consistency(self, project_files: Dict[str, str]) -> Dict[str, Any]:
        """
        Проверяет согласованность данных между файлами проекта
        
        Args:
            project_files: Словарь путей к файлам проекта
        
        Returns:
            Результат проверки согласованности
        """
        result = {
            "consistent": True,
            "errors": [],
            "warnings": []
        }
        
        try:
            # Загружаем данные из файлов
            data = {}
            for file_type, file_path in project_files.items():
                if Path(file_path).exists():
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            data[file_type] = json.load(f)
                    except Exception:
                        continue
            
            # Проверяем согласованность персонажей
            self._check_character_consistency(data, result)
            
            # Проверяем согласованность локаций
            self._check_location_consistency(data, result)
            
            # Проверяем соответствие количества страниц
            self._check_page_consistency(data, result)
            
        except Exception as e:
            result["consistent"] = False
            result["errors"].append(f"Ошибка проверки согласованности: {e}")
        
        return result
    
    def _check_character_consistency(self, data: Dict, result: Dict):
        """Проверяет согласованность данных о персонажах"""
        brief_characters = set(data.get("brief", {}).get("main_characters", []))
        bible_characters = set(char.get("name", "") for char in data.get("characters", []))
        
        # Персонажи из brief должны быть в bible
        missing_in_bible = brief_characters - bible_characters
        if missing_in_bible:
            result["errors"].append(f"Персонажи из brief отсутствуют в characters: {', '.join(missing_in_bible)}")
            result["consistent"] = False
        
        # Дополнительные персонажи в bible
        extra_in_bible = bible_characters - brief_characters
        if extra_in_bible:
            result["warnings"].append(f"Дополнительные персонажи в characters: {', '.join(extra_in_bible)}")
    
    def _check_location_consistency(self, data: Dict, result: Dict):
        """Проверяет согласованность данных о локациях"""
        brief_locations = set(data.get("brief", {}).get("main_locations", []))
        bible_locations = set(loc.get("name", "") for loc in data.get("locations", []))
        
        # Локации из brief должны быть в bible
        missing_in_bible = brief_locations - bible_locations
        if missing_in_bible:
            result["errors"].append(f"Локации из brief отсутствуют в locations: {', '.join(missing_in_bible)}")
            result["consistent"] = False
    
    def _check_page_consistency(self, data: Dict, result: Dict):
        """Проверяет соответствие количества страниц"""
        brief_data = data.get("brief", {})
        story_data = data.get("story", {})
        
        pages_min = brief_data.get("pages_min", 0)
        pages_max = brief_data.get("pages_max", 0)
        actual_pages = len(story_data.get("pages", []))
        
        if actual_pages < pages_min:
            result["errors"].append(f"Недостаточно страниц: {actual_pages} < {pages_min}")
            result["consistent"] = False
        elif actual_pages > pages_max:
            result["warnings"].append(f"Слишком много страниц: {actual_pages} > {pages_max}")


# Глобальный экземпляр валидатора
json_validator = JSONValidator()
