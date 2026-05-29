import os
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def load_bible_data(project_id: str) -> tuple[List[Dict], List[Dict], List[Dict]]:
    """Загружает данные из 20_bible: персонажей, локации и правила консистентности.
    
    Args:
        project_id (str): Идентификатор проекта
        
    Returns:
        tuple: (characters, locations, consistency_rules)
    """
    base = f"plots/storybooks/{project_id}/20_bible"
    
    # Загрузка персонажей
    characters_path = f"{base}/characters.json"
    characters = []
    if os.path.exists(characters_path):
        try:
            with open(characters_path, "r", encoding="utf-8") as f:
                characters = json.load(f)
        except Exception as e:
            logger.warning(f"Не удалось загрузить characters.json: {e}")
    
    # Загрузка локаций
    locations_path = f"{base}/locations.json"
    locations = []
    if os.path.exists(locations_path):
        try:
            with open(locations_path, "r", encoding="utf-8") as f:
                locations = json.load(f)
        except Exception as e:
            logger.warning(f"Не удалось загрузить locations.json: {e}")
    
    # Загрузка правил консистентности
    consistency_rules_path = f"{base}/consistency_rules.json"
    consistency_rules = []
    if os.path.exists(consistency_rules_path):
        try:
            with open(consistency_rules_path, "r", encoding="utf-8") as f:
                consistency_rules = json.load(f)
        except Exception as e:
            logger.warning(f"Не удалось загрузить consistency_rules.json: {e}")
    
    return characters, locations, consistency_rules


def find_entities_by_reference_paths(reference_paths: List[str], 
                                   characters: List[Dict], 
                                   locations: List[Dict]) -> tuple[List[Dict], List[Dict]]:
    """Находит персонажей и локации по их reference_image_path.
    
    Args:
        reference_paths (List[str]): Список путей к изображениям-референсам
        characters (List[Dict]): Список всех персонажей
        locations (List[Dict]): Список всех локаций
        
    Returns:
        tuple: (найденные персонажи, найденные локации)
    """
    found_characters = []
    found_locations = []
    
    for ref_path in reference_paths:
        # Нормализуем путь - убираем префикс проекта если есть
        normalized_path = ref_path
        if ref_path.startswith("plots/storybooks/"):
            # Извлекаем часть после project_id
            parts = ref_path.split("/")
            if len(parts) >= 5:  # plots/storybooks/project_id/20_bible/references/...
                normalized_path = "/" + "/".join(parts[4:])
        
        # Ищем среди персонажей
        for character in characters:
            if character.get("reference_image_path") == normalized_path:
                found_characters.append(character)
                break
        
        # Ищем среди локаций
        for location in locations:
            if location.get("reference_image_path") == normalized_path:
                found_locations.append(location)
                break
    
    return found_characters, found_locations


def items_for_artist_tool(session_id: str, project_id: str, language: str) -> str:
    """Формирует список items для artist_agent_batch_edit_tool на основе 40_prompts/*.
    Каждый item создаёт/редактирует финальное изображение сцены, с output_path.
    
    Возвращает единый массив items с добавленными персонажами, локациями и правилами консистентности.

    Args:
        session_id (str): Идентификатор сессии.
        project_id (str): Идентификатор проекта. Используется для путей вида
            `plots/storybooks/{project_id}` и поиска входных файлов в каталоге
            `40_prompts/`.
        language (str): Язык для генерации контента.

    Returns:
        str: JSON-строка с объектом, содержащим:
            - items: массив сцен с персонажами и локациями
            - consistency_rules: массив правил консистентности
    """
    base = f"plots/storybooks/{project_id}"
    prompts_dir = f"{base}/40_prompts"
    items_path = f"{base}/50_items/items.json"
    
    # Загружаем данные из 20_bible
    characters, locations, consistency_rules = load_bible_data(project_id)
    
    items: List[Dict[str, Any]] = []
    # единый базовый герой
    protagonist_base = f"{base}/30_assets/protagonist/base.png"
    page = 1
    while True:
        prompt_path = f"{prompts_dir}/page_{page:02d}_prompt.json"
        if not os.path.exists(prompt_path):
            break
        with open(prompt_path, "r", encoding="utf-8") as f:
            pp = json.load(f)
        
        # Технические параметры
        technical = pp.get("technical", {})
        width = technical.get("width") or 1920
        height = technical.get("height") or 1080
        
        # Собираем все референс-пути
        references = pp.get("references", {}) or {}
        character_paths = references.get("character_paths", [])
        location_path = references.get("location_path")
        all_reference_paths = character_paths[:]
        if location_path:
            all_reference_paths.append(location_path)
        
        # Находим соответствующих персонажей и локации
        item_characters, item_locations = find_entities_by_reference_paths(
            all_reference_paths, characters, locations
        )
        
        item = {
            "project_id": project_id,
            "page_number": page,
            # базовое изображение теперь единое: герой
            "image_path": protagonist_base if os.path.exists(protagonist_base) else None,
            "english_prompt": pp.get("english_prompt", ""),
            "negative_prompt": pp.get("negative_prompt", ""),
            "reference_image_paths": all_reference_paths,
            "width": width,
            "height": height,
            "true_cfg_scale": technical.get("guidance", 4.0),
            "num_inference_steps": technical.get("steps", 50),
            "seed": None,
            "number": page,
            "output_path": f"{base}/50_images/page_{page:02d}/img_final.png",
            # Новые поля с персонажами и локациями
            "characters": item_characters,
            "locations": item_locations
        }
        os.makedirs(os.path.dirname(item["output_path"]), exist_ok=True)
        items.append(item)
        page += 1
    
    # Формируем финальный результат с единым массивом items и consistency_rules
    result = {
        "items": items,
        "consistency_rules": consistency_rules
    }
    
    # Сохраняем items в файл для кэширования
    os.makedirs(os.path.dirname(items_path), exist_ok=True)
    result_json = json.dumps(result, ensure_ascii=False, indent=2)
    with open(items_path, "w", encoding="utf-8") as f:
        f.write(result_json)
    
    return result_json


