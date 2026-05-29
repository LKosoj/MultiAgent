#!/usr/bin/env python3
"""
Тест валидации обновленных схем
=============================

Проверяет соответствие схем реальным JSON файлам из проекта dolboyazher6.
"""

import sys
import json
from pathlib import Path

# Добавляем путь к проекту
sys.path.insert(0, str(Path(__file__).parent))

# Удален импорт schemas.py - теперь используется гибридная генерация схем
# from config.schemas import SCHEMA_MAPPING
from utils.json_validator import JSONValidator

def test_schema_validation():
    """Тестирует валидацию схем на реальных данных"""
    
    project_path = Path("/Users/kosoj/Documents/MultiAgent/plots/storybooks/dolboyazher6")
    validator = JSONValidator()
    
    # Тестовые файлы
    test_files = [
        ("brief", project_path / "00_brief.json"),
        ("synopsis", project_path / "10_synopsis" / "synopsis.json"),
        ("beats", project_path / "10_synopsis" / "beats.json"),
        ("story", project_path / "20_story" / "story.json"),
        ("characters", project_path / "20_bible" / "characters.json"),
        ("locations", project_path / "20_bible" / "locations.json"),
        ("screenplay", project_path / "91_screenplay" / "screenplay.json"),
        # ("shots", project_path / "97_shots" / "shots.json"),  # Слишком большой файл
    ]
    
    print("🔍 Тестирование валидации схем")
    print("=" * 50)
    
    all_passed = True
    
    for schema_type, file_path in test_files:
        print(f"\n📋 Тестируется: {schema_type}")
        print(f"📁 Файл: {file_path}")
        
        if not file_path.exists():
            print(f"❌ Файл не найден: {file_path}")
            all_passed = False
            continue
        
        try:
            # Загружаем данные
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Валидируем
            result = validator.validate_file(str(file_path), schema_type)
            
            if result["valid"]:
                print(f"✅ Валидация прошла успешно")
                
                # Показываем предупреждения если есть
                if result.get("warnings"):
                    print(f"⚠️  Предупреждения ({len(result['warnings'])}):")
                    for warning in result["warnings"][:3]:  # Показываем первые 3
                        print(f"   • {warning}")
                    if len(result["warnings"]) > 3:
                        print(f"   ... и еще {len(result['warnings']) - 3}")
                
                # Показываем предложения если есть
                if result.get("suggestions"):
                    print(f"💡 Предложения ({len(result['suggestions'])}):")
                    for suggestion in result["suggestions"][:2]:  # Показываем первые 2
                        print(f"   • {suggestion}")
                    if len(result["suggestions"]) > 2:
                        print(f"   ... и еще {len(result['suggestions']) - 2}")
                        
            else:
                print(f"❌ Ошибки валидации ({len(result['errors'])}):")
                for error in result["errors"]:
                    print(f"   • {error}")
                all_passed = False
                
        except Exception as e:
            print(f"❌ Ошибка обработки: {e}")
            all_passed = False
    
    print("\n" + "=" * 50)
    if all_passed:
        print("🎉 Все схемы валидированы успешно!")
    else:
        print("⚠️  Найдены проблемы в некоторых схемах")
    
    return all_passed

def test_new_schemas():
    """Тестирует новые добавленные схемы"""
    print("\n🆕 Тестирование новых схем")
    print("=" * 30)
    
    # Проверяем наличие новых схем
    new_schemas = ["synopsis", "beats"]
    
    for schema_name in new_schemas:
        if schema_name in SCHEMA_MAPPING:
            print(f"✅ Схема '{schema_name}' найдена")
            schema = SCHEMA_MAPPING[schema_name]
            
            # Базовая проверка структуры схемы
            if "type" in schema and "properties" in schema:
                print(f"   📋 Поля: {len(schema['properties'])}")
                print(f"   🔒 Обязательные: {len(schema.get('required', []))}")
            else:
                print(f"   ⚠️  Неполная структура схемы")
        else:
            print(f"❌ Схема '{schema_name}' не найдена")

def test_shots_schema_fields():
    """Тестирует дополненную схему shots"""
    print("\n🎬 Тестирование схемы shots")
    print("=" * 30)
    
    shot_schema = SCHEMA_MAPPING.get("shot")
    if not shot_schema:
        print("❌ Схема shot не найдена")
        return
    
    # Проверяем наличие новых полей
    new_fields = [
        "image_path", "video_prompt", "reference_image_paths", 
        "seed", "number", "output_path", "video_path",
        "characters", "locations", "should_use_prev_end_as_reference",
        "continuity_score", "composition_stability", "spatial_changes_from_start",
        "link_type", "link_reasoning", "extended_context_used",
        "narrative_position", "scene_pacing", "camera_position",
        "character_orientation", "spatial_composition"
    ]
    
    properties = shot_schema.get("properties", {})
    
    found_fields = []
    missing_fields = []
    
    for field in new_fields:
        if field in properties:
            found_fields.append(field)
        else:
            missing_fields.append(field)
    
    print(f"✅ Найдено новых полей: {len(found_fields)}")
    if missing_fields:
        print(f"❌ Отсутствуют поля: {missing_fields}")
    else:
        print("🎉 Все новые поля добавлены в схему!")
    
    print(f"📊 Общее количество полей в схеме: {len(properties)}")

if __name__ == "__main__":
    test_new_schemas()
    test_shots_schema_fields()
    test_schema_validation()
