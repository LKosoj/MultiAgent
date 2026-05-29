import os
import json
from typing import Dict, Any
from utils import call_openai_api, parse_llm_json
from agent_command import model_code
import logging

logger = logging.getLogger(__name__)


def bible_builder_tool(session_id: str, project_id: str, language: str = "ru") -> str:
    """Строит characters.json, locations.json, consistency_rules.json и кладёт references/.
    Требует 10_synopsis/synopsis.json и beats.json.

    Args:
        project_id (str): Идентификатор проекта. Используется для поиска входных
            файлов в каталоге `10_synopsis` и записи результатов в `20_bible`.
        session_id (str): Идентификатор сессии.
        language (str): Язык генерации (по умолчанию "ru").

    Returns:
        str: Путь к каталогу `20_bible` с созданными файлами и референсами.
    """
    base = f"plots/storybooks/{project_id}"
    syn_dir = f"{base}/10_synopsis"
    out_dir = f"{base}/20_bible"
    characters_path = f"{out_dir}/characters.json"
    locations_path = f"{out_dir}/locations.json"
    rules_path = f"{out_dir}/consistency_rules.json"
    
    # Проверяем, существуют ли УЖЕ ВСЕ основные файлы
    if (os.path.exists(characters_path) and 
        os.path.exists(locations_path) and 
        os.path.exists(rules_path)):
        logger.info(f"📚 Библия персонажей уже существует (все файлы найдены), пропускаем генерацию")
        return out_dir
    with open(f"{syn_dir}/synopsis.json", "r", encoding="utf-8") as f:
        synopsis = json.load(f)
    with open(f"{syn_dir}/beats.json", "r", encoding="utf-8") as f:
        beats = json.load(f)

    system = """Ты систематизатор канона персонажей и мира. 

КРИТИЧЕСКИ ВАЖНО: Верни строго валидный JSON с полями characters, locations, consistency_rules.

Схема JSON:
{
  "characters": [
    {
      "name": "string",
      "age": "string", 
      "role": "string",
      "immutable_attributes": {
        "face_shape": "string",
        "eye_color": "string", 
        "skin_tone": "string",
        "body_proportions": "string",
        "unique_features": ["string1", "string2"]
      },
      "variable_attributes": {
        "base_clothing": "string",
        "base_hairstyle": "string", 
        "accessories": ["string1", "string2"]
      },
      "reference_image_path": "/references/characters/name.png",
      "gesture_set": ["string1", "string2"],
      "speech_patterns": ["string1", "string2"], 
      "no_go_rules": ["string1", "string2"]
    }
  ],
  "locations": [
    {
      "name": "string",
      "description": "string",
      "key_objects": ["string1", "string2"],
      "atmosphere": "string",
      "lighting": "string", 
      "color_palette": ["color1", "color2"],
      "reference_image_path": "/references/locations/name.png"
    }
  ],
  "consistency_rules": [
    {
      "rule": "string",
      "applies_to": ["target1", "target2"]
    }
  ]
}

НЕ добавляй комментарии в JSON. НЕ используй trailing commas."""
    prompt = json.dumps({"synopsis": synopsis, "beats": beats}, ensure_ascii=False)

    resp = call_openai_api(
        prompt=prompt,
        system_prompt=system,
        model=model_code,
        max_tokens=32768,
        temperature=0.4,
        response_format={"type": "json_object"}
    )
    data = parse_llm_json(resp, fallback_list_key="characters")
    if not data:
        raise ValueError("bible_builder_tool: не удалось распарсить JSON ответа от LLM")

    out_dir = f"{base}/20_bible"
    os.makedirs(out_dir, exist_ok=True)
    with open(f"{out_dir}/characters.json", "w", encoding="utf-8") as f:
        json.dump(data.get("characters", []), f, ensure_ascii=False, indent=2)
    with open(f"{out_dir}/locations.json", "w", encoding="utf-8") as f:
        json.dump(data.get("locations", []), f, ensure_ascii=False, indent=2)
    with open(f"{out_dir}/consistency_rules.json", "w", encoding="utf-8") as f:
        json.dump(data.get("consistency_rules", []), f, ensure_ascii=False, indent=2)
    # Референсы могут быть предзагружены пользователем — здесь только гарантируем каталоги
    os.makedirs(f"{out_dir}/references/characters", exist_ok=True)
    os.makedirs(f"{out_dir}/references/locations", exist_ok=True)
    return out_dir


