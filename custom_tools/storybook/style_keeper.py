import os
import json
from typing import Dict, Any
from utils import call_openai_api, parse_llm_json
from agent_command import model_code
from custom_tools.storybook.style_library_config import (
    compose_negative_seed,
    compose_style_catalog_note,
    merge_preset_into_style_images,
    resolve_preset,
)
from custom_tools.storybook.screenplay_shots_generator_utils.technical import (
    _dedup_negative_prompt,
)
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

    catalog_note = compose_style_catalog_note()
    system = f"""Ты арт-директор. Верни JSON с полями style_text, style_preset_id, style_overrides и negative_list.

{catalog_note}

style_text: {{ narrative_voice, sentence_length, vocabulary_bounds }}
style_preset_id: ровно один id из каталога выше — тот, чей визуальный язык точнее всего отвечает синопсису, героям и тону истории.
style_overrides: проектные уточнения ПОВЕРХ выбранного пресета. Разрешены только поля color_palette (список), composition_rules (список), lighting, detail_density, do_not_include (список), model, project_note. Сам визуальный язык, фактуру носителя и оптику держит пресет — не переопределяй их.
negative_list: строка с запятыми — только запреты, специфичные для ЭТОЙ истории (нежелательные объекты, сущности, мотивы). Технические и стилевые запреты каталог добавит сам, не дублируй их.

ПРИМЕР КОРРЕКТНОГО ФОРМАТА ОТВЕТА:
{{
  "style_text": {{
    "narrative_voice": "мягкий",
    "sentence_length": "средняя",
    "vocabulary_bounds": "простой"
  }},
  "style_preset_id": "storybook_watercolor",
  "style_overrides": {{
    "color_palette": ["autumn ochre", "dusty rose"],
    "lighting": "low evening sun through window",
    "do_not_include": ["modern electronics"],
    "project_note": "камерная домашняя история"
  }},
  "negative_list": "modern electronics, city traffic"
}}
КРИТИЧНО: `style_text` и `style_overrides` — объекты, `style_preset_id` и `negative_list` — строки.
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

    preset = resolve_preset(data.get("style_preset_id"))
    style_images = merge_preset_into_style_images(preset, data.get("style_overrides"))
    negative_terms = compose_negative_seed(preset)
    story_negatives = str(data.get("negative_list") or "").strip()
    if story_negatives:
        negative_terms.append(story_negatives)
    negative_list = _dedup_negative_prompt(", ".join(negative_terms))
    logger.info(
        "🎨 Стиль: пресет '%s' (%s), запретов в negative_prompt_list: %d",
        preset.id, preset.title, len(negative_list.split(",")),
    )

    out_dir = f"{base}/30_style"
    os.makedirs(out_dir, exist_ok=True)
    with open(f"{out_dir}/style_text.json", "w", encoding="utf-8") as f:
        json.dump(data.get("style_text", {}), f, ensure_ascii=False, indent=2)
    with open(f"{out_dir}/style_images.json", "w", encoding="utf-8") as f:
        json.dump(style_images, f, ensure_ascii=False, indent=2)
    with open(f"{out_dir}/negative_prompt_list.txt", "w", encoding="utf-8") as f:
        f.write(negative_list)
    return out_dir


