import os
import json
from typing import Dict, Any
from utils import call_openai_api, parse_llm_json
from agent_command import model_code
import logging

logger = logging.getLogger(__name__)


def style_keeper_tool(session_id: str, project_id: str) -> str:
    """Фиксирует style_text.json, style_images.json, negative_prompt_list.txt.

    Args:
        project_id (str): Идентификатор проекта. Используется для чтения материалов
            из `10_synopsis` и `20_bible` и записи итогов в `30_style`.
        session_id (str): Идентификатор сессии.

    Returns:
        str: Путь к каталогу `30_style` с итоговыми файлами.
    """
    base = f"plots/storybooks/{project_id}"
    syn_dir = f"{base}/10_synopsis"
    bible_dir = f"{base}/20_bible"
    out_dir = f"{base}/30_style"
    style_text_path = f"{out_dir}/style_text.json"
    style_images_path = f"{out_dir}/style_images.json"
    negative_path = f"{out_dir}/negative_prompt_list.txt"
    
    # Проверяем, существуют ли УЖЕ ВСЕ основные файлы
    if (os.path.exists(style_text_path) and 
        os.path.exists(style_images_path) and 
        os.path.exists(negative_path)):
        logger.info(f"🎨 Стиль уже существует (все файлы найдены), пропускаем генерацию")
        return out_dir
    with open(f"{syn_dir}/synopsis.json", "r", encoding="utf-8") as f:
        synopsis = json.load(f)
    with open(f"{syn_dir}/beats.json", "r", encoding="utf-8") as f:
        beats = json.load(f)
    with open(f"{bible_dir}/characters.json", "r", encoding="utf-8") as f:
        characters = json.load(f)
    with open(f"{bible_dir}/locations.json", "r", encoding="utf-8") as f:
        locations = json.load(f)

    system = """Ты арт-директор. Верни JSON с полями style_text и style_images, а также plain-text negative_list.
style_text: { narrative_voice, sentence_length, vocabulary_bounds }
style_images: { art_style, color_palette, composition_rules, lighting, texture, detail_density, do_not_include, model }
negative_list: строка с запятыми (nsfw, watermark, text, logo, distorted hands, extra limbs, deformed face, lowres, distorted parts)

ПРИМЕР КОРРЕКТНОГО ФОРМАТА ОТВЕТА:
{
  "style_text": {
    "narrative_voice": "мягкий",
    "sentence_length": "средняя",
    "vocabulary_bounds": "простой"
  },
  "style_images": {
    "art_style": "storybook watercolor",
    "color_palette": ["warm", "pastel"],
    "composition_rules": ["rule of thirds"],
    "lighting": "soft warm",
    "texture": "paper grain",
    "detail_density": "medium",
    "do_not_include": ["nsfw", "watermark"],
    "model": "illustration"
  },
  "negative_list": "nsfw, watermark, text, logo"
}
КРИТИЧНО: `style_text` и `style_images` — объекты, `negative_list` — строка.
"""
    prompt = json.dumps({
        "synopsis": synopsis,
        "beats": beats,
        "characters": characters,
        "locations": locations
    }, ensure_ascii=False)

    resp = call_openai_api(
        prompt=prompt,
        system_prompt=system,
        model=model_code,
        max_tokens=32768,
        temperature=0.3,
        response_format={"type": "json_object"}
    )
    data = parse_llm_json(resp)

    out_dir = f"{base}/30_style"
    os.makedirs(out_dir, exist_ok=True)
    with open(f"{out_dir}/style_text.json", "w", encoding="utf-8") as f:
        json.dump(data.get("style_text", {}), f, ensure_ascii=False, indent=2)
    with open(f"{out_dir}/style_images.json", "w", encoding="utf-8") as f:
        json.dump(data.get("style_images", {}), f, ensure_ascii=False, indent=2)
    with open(f"{out_dir}/negative_prompt_list.txt", "w", encoding="utf-8") as f:
        f.write(data.get("negative_list", "nsfw, watermark, text, logo, distorted hands, extra limbs, deformed face, lowres, distorted parts"))
    return out_dir


