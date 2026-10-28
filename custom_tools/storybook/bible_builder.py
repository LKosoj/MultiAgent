import os
import json
import re
from typing import Dict, Any
from utils import call_openai_api, parse_llm_json
from agent_command import model_hard
from .project_paths import safe_storybook_project_dir
import logging

logger = logging.getLogger(__name__)
_LOCATION_PASSPORT_VERSION = 3


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
    base = str(safe_storybook_project_dir(project_id))
    syn_dir = f"{base}/10_synopsis"
    out_dir = f"{base}/20_bible"
    characters_path = f"{out_dir}/characters.json"
    locations_path = f"{out_dir}/locations.json"
    rules_path = f"{out_dir}/consistency_rules.json"
    
    canon_paths = {"characters": characters_path, "locations": locations_path, "consistency_rules": rules_path}
    sections_to_generate = [key for key, path in canon_paths.items() if not os.path.exists(path)]
    if os.path.exists(locations_path):
        with open(locations_path, "r", encoding="utf-8") as f:
            previous_locations = json.load(f)
        if any(location.get("_passport_version") != _LOCATION_PASSPORT_VERSION for location in previous_locations):
            sections_to_generate.append("locations")

    # Проверяем, существуют ли УЖЕ ВСЕ основные файлы
    if not sections_to_generate:
        logger.info(f"📚 Библия персонажей уже существует (все файлы найдены), пропускаем генерацию")
        return out_dir
    with open(f"{syn_dir}/synopsis.json", "r", encoding="utf-8") as f:
        synopsis = json.load(f)
    with open(f"{syn_dir}/beats.json", "r", encoding="utf-8") as f:
        beats = json.load(f)

    system = """Ты систематизатор канона персонажей и мира. 

КРИТИЧЕСКИ ВАЖНО: Верни строго валидный JSON. Генерируй только разделы из sections_to_generate; существующие разделы служат контекстом и не должны пересоздаваться. Для полного нового канона нужны characters, locations, consistency_rules.

Схема JSON:
{
  "characters": [
    {
      "name": "string",
      "age": "string", 
      "role": "string",
      "historical_period": "string",
      "environment_context": "string",
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
      "dynamic_changes": ["изменения и результаты действий, относящиеся к сценам, а не к паспорту места"],
      "static_passport": {
        "description": "устойчивое видимое устройство места без пересказа действий",
        "historical_period": "string",
        "environment_context": "string",
        "key_objects": ["физический предмет или носитель без его изменяемого сюжетного содержимого"],
        "atmosphere": "string",
        "lighting": "string",
        "color_palette": ["color1", "color2"]
      },
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

НЕ добавляй комментарии в JSON. НЕ используй trailing commas.""" + (
        f"\n\nLANGUAGE CONTRACT: Generate every descriptive text value in the requested "
        f"language `{language}`. Keep JSON keys unchanged and preserve proper names unchanged."
    )
    brief = {}
    if os.path.exists(f"{base}/00_brief.json"):
        with open(f"{base}/00_brief.json", "r", encoding="utf-8") as f:
            brief = json.load(f)
    existing_canon = {}
    for key, path in (("characters", characters_path), ("locations", locations_path)):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                existing_canon[key] = json.load(f)
    if "locations" in sections_to_generate and "locations" in existing_canon:
        existing_canon["locations"] = [
            {key: location[key] for key in ("name", "reference_image_path") if key in location}
            for location in existing_canon["locations"]
        ]
    prompt = json.dumps({"brief": brief, "language": language, "synopsis": synopsis, "beats": beats, "existing_canon": existing_canon, "sections_to_generate": sections_to_generate}, ensure_ascii=False)

    system += "\nВключи всех действующих персонажей и группы из исходного задания brief.storybook_prompt, даже если их нет в кратком main_characters."
    system += "\nРазные группы людей из разных мест исходной истории — отдельные записи characters, не объединяй их из-за схожей роли. immutable_attributes содержат только неизменные признаки, а не альтернативные внешности разных людей."
    system += "\nДля КАЖДОГО персонажа и локации заполни historical_period и environment_context по исходному заданию: эпоха, место, сезон, погода, архитектура и одежда. Не допускай анахронизмов. Не выдумывай эпоху, если она не задана: оставь historical_period пустым."
    system += "\nЕсли передан existing_canon, сохрани имена и внешность персонажей, а также reference_image_path всех идентичностей; дополни контекст эпохи и окружения. Название локации исправляй, если оно вместо места описывает результат события. Ошибки событий и окружения исправляй по исходнику и pipeline_quality_feedback, не сохраняй их ради совпадения с existing_canon и не создавай заново идентичности персонажей."
    system += "\nПАСПОРТА ЛОКАЦИЙ: name, description, environment_context, key_objects, atmosphere, lighting и color_palette описывают устойчивые признаки места и, при необходимости, его НАЧАЛЬНОЕ состояние при первом появлении в исходной истории. Это не пересказ сюжетной арки и не выбор самого выразительного момента. Не превращай узнаваемое событие истории в постоянный признак места. Если состояние предмета меняется по сюжету и начальное состояние неочевидно, опиши сам предмет нейтрально. Не включай последующие изменения, последствия действий, переходы и итоговые состояния. Они относятся к конкретным сценам и шотам, а не к постоянному канону локации."
    system += "\nНачальное состояние определяй по самому первому появлению места в детальной раскадровке исходника. При расхождении сводного описания с покадровой последовательностью приоритет имеет последовательность конкретных действий: её поздние результаты нельзя выдавать за уже существующие признаки места. Сохрани все прежние идентичности из existing_canon.locations и их reference_image_path; эти записи задают сопоставление, а не прежние состояния."
    system += "\nДля каждой локации сначала отдели dynamic_changes — факты изменений, действий и их результатов внутри этой локации. Затем создай static_passport только из устойчивого устройства места. Изменяемое содержимое предмета относится к dynamic_changes, а в static_passport остаётся сам физический носитель. Не дублируй сюжетные изменения в description, key_objects, environment_context или других полях static_passport. В файл паспорта код сохранит только static_passport и идентификаторы; dynamic_changes в него не попадут."
    system += "\nИмена локаций должны обозначать место нейтрально, без будущего результата события. При исправлении существующего паспорта сохраняй reference_image_path и идентичность места; прежние данные из brief.pipeline_quality_feedback используй для сопоставления, а не как основание сохранять ошибочное итоговое состояние."
    system += "\nВ consistency_rules.applies_to используй только точные актуальные имена из существующих или генерируемых characters и locations; старые названия из замечаний не являются актуальными целями. Временные ограничения формулируй с явным условием этапа, не выдавая их за постоянные свойства места."
    for attempt in range(2):
        resp = call_openai_api(
            prompt=prompt,
            system_prompt=system,
            model=model_hard,
            max_tokens=32768,
            temperature=0.4,
            response_format={"type": "json_object"}
        )
        data = parse_llm_json(resp, fallback_list_key="characters")
        if not data:
            raise ValueError("bible_builder_tool: не удалось распарсить JSON ответа от LLM")
        if not (language.startswith("ru") and re.search(r"[\u3400-\u9fff]", json.dumps(data, ensure_ascii=False))):
            break
        if attempt == 1:
            raise ValueError("bible_builder_tool: иероглифы в русскоязычном описании")
        logger.warning("bible_builder_tool: иероглифы в русском тексте, повторяем генерацию")
        system += "\nПредыдущий ответ содержал иероглифы внутри русского текста. Перегенерируй полностью на русском, без иероглифов."

    out_dir = f"{base}/20_bible"
    os.makedirs(out_dir, exist_ok=True)
    for key in sections_to_generate:
        if key == "locations":
            data[key] = [
                {
                    **location["static_passport"],
                    "name": location["name"],
                    "reference_image_path": location["reference_image_path"],
                    "_passport_version": _LOCATION_PASSPORT_VERSION,
                }
                for location in data.get(key, [])
            ]
        with open(canon_paths[key], "w", encoding="utf-8") as f:
            json.dump(data.get(key, []), f, ensure_ascii=False, indent=2)
    # Референсы могут быть предзагружены пользователем — здесь только гарантируем каталоги
    os.makedirs(f"{out_dir}/references/characters", exist_ok=True)
    os.makedirs(f"{out_dir}/references/locations", exist_ok=True)
    return out_dir

