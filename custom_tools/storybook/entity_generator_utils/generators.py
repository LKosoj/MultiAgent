import json
import logging
from typing import Dict, Any, List, Optional

from agent_command import model_hard
from utils import call_openai_api, extract_json_from_markdown
from .prompt_templates import (
    get_location_description_system_prompt,
    get_character_description_system_prompt
)

logger = logging.getLogger(__name__)

def generate_location_description(
    location_data: Dict[str, Any], 
    concept: Dict[str, Any], 
    existing_locations: list
) -> Dict[str, Any]:
    """Генерация описания локации с полным контекстом"""
    
    # Создаем краткое описание существующих локаций для стилистической согласованности
    existing_summary = []
    for loc in existing_locations[:3]:  # Первые 3 для контекста
        existing_summary.append(f"- {loc.get('name', '')}: {loc.get('atmosphere', '')} атмосфера, {loc.get('lighting', '')}")
    
    system_prompt = get_location_description_system_prompt(chr(10).join(existing_summary))
    
    # Формируем пользовательский промпт с контекстом
    user_context = f"""ЛОКАЦИЯ: "{location_data.get('name', '')}"
КОНТЕКСТ ИЗ СЦЕНАРИЯ: {location_data.get('context', '')}
СЦЕНЫ С УПОМИНАНИЕМ: {location_data.get('scene_references', [])}
ОБЩИЙ СТИЛЬ ПРОЕКТА: {concept.get('visual_style', '')}"""

    sanitized_name = location_data.get('name', '').lower().replace(' ', '_').replace("'", '').replace('"', '')
    
    user_prompt = f"{user_context}\n\nСоздай детальное описание локации для анимационного проекта"
    
    try:
        resp = call_openai_api(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model_hard,
            max_tokens=2000,
            temperature=0.3,
            response_format={"type": "json_object"}
        )
        clean_resp = extract_json_from_markdown(resp)
        result = json.loads(clean_resp)
        
        # Обеспечиваем корректный reference_image_path
        result["reference_image_path"] = f"/references/locations/{sanitized_name}.png"
        return result
        
    except Exception as e:
        logger.error(f"❌ Ошибка генерации описания локации: {e}")
        # Возвращаем минимальную заглушку
        return {
            "name": location_data.get('name', 'Неизвестная локация'),
            "description": f"Локация из сценария: {location_data.get('context', '')}",
            "key_objects": ["основные элементы"],
            "atmosphere": "нейтральная",
            "lighting": "естественное освещение",
            "color_palette": ["#ffffff", "#000000", "#808080", "#cccccc"],
            "reference_image_path": f"/references/locations/{sanitized_name}.png",
            "location_sheet_instruction": "Create a multi-view location sheet: Wide shot, Cinematic low angle, Top-down plan, Side view details."
        }


def generate_character_description(
    character_data: Dict[str, Any], 
    concept: Dict[str, Any], 
    existing_characters: list
) -> Dict[str, Any]:
    """Генерация описания персонажа с ролевым контекстом"""
    
    # Создаем краткое описание главных персонажей для согласованности
    main_characters_summary = []
    for char in existing_characters[:2]:  # Первые 2 для контекста
        main_characters_summary.append(f"- {char.get('name', '')}: {char.get('role', '')}")
    
    system_prompt = get_character_description_system_prompt(chr(10).join(main_characters_summary))
    
    user_context = f"""ПЕРСОНАЖ: "{character_data.get('name', '')}"
РОЛЬ В СЦЕНАРИИ: {character_data.get('role', '')}
КОНТЕКСТ ПОЯВЛЕНИЯ: {character_data.get('context', '')}
ЕСТЬ ЛИ ДИАЛОГИ: {character_data.get('has_dialogue', False)}
СЦЕНЫ С УПОМИНАНИЕМ: {character_data.get('scene_references', [])}
ОБЩИЙ СТИЛЬ ПРОЕКТА: {concept.get('visual_style', '')}"""
    
    sanitized_name = character_data.get('name', '').lower().replace(' ', '_').replace("'", '').replace('"', '')
    
    user_prompt = f"{user_context}\n\nСоздай детальное описание персонажа для анимационного проекта"
    
    try:
        resp = call_openai_api(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model_hard,
            max_tokens=2000,
            temperature=0.3,
            response_format={"type": "json_object"}
        )
        clean_resp = extract_json_from_markdown(resp)
        result = json.loads(clean_resp)
        
        # Обеспечиваем корректный reference_image_path
        result["reference_image_path"] = f"/references/characters/{sanitized_name}.png"
        return result
        
    except Exception as e:
        logger.error(f"❌ Ошибка генерации описания персонажа: {e}")
        # Возвращаем минимальную заглушку
        return {
            "name": character_data.get('name', 'Неизвестный персонаж'),
            "age": "неопределенный",
            "role": character_data.get('role', 'второстепенный персонаж'),
            "immutable_attributes": {
                "face_shape": "обычное",
                "eye_color": "темные",
                "skin_tone": "нейтральный",
                "body_proportions": "средние",
                "unique_features": ["стандартная внешность"]
            },
            "variable_attributes": {
                "base_clothing": "повседневная одежда",
                "base_hairstyle": "обычная прическа",
                "accessories": []
            },
            "reference_image_path": f"/references/characters/{sanitized_name}.png",
            "gesture_set": ["обычные жесты"],
            "speech_patterns": ["нейтральная речь"],
            "no_go_rules": ["стандартные ограничения"]
        }

