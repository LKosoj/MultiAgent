import os
import json
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from typing import Dict, Any
from utils import call_openai_api, parse_llm_json
from agent_command import model_code
from custom_tools.storybook.style_library_config import (
    compose_negative_seed,
    compose_style_catalog_note,
    get_active_style_profile,
    merge_preset_into_style_images,
    resolve_preset,
)
from custom_tools.storybook.screenplay_shots_generator_utils.technical import (
    _dedup_negative_prompt,
)
from .project_paths import safe_storybook_project_dir
import logging

logger = logging.getLogger(__name__)
_IMAGE_STYLE_CATALOG_VERSION = 2
_STYLE_REFERENCE_INSTRUCTION = (
    "Image 1 — опорное изображение персонажа, Image 2 — опорное изображение локации этого же стиля. "
    "Создай по ним новый самостоятельный визуальный эталон стиля: одну цельную нейтральную композицию "
    "с этим персонажем в этой локации, без сюжетного действия. Полностью покажи заданную технику, носитель, "
    "фактуру, характер линии, палитру, свет и обработку, сохрани внешний вид персонажа и устройство среды. "
    "Если референсы содержат несколько ракурсов, выбери один согласованный вид, а не сетку изображений. "
    "Без панелей, таблиц, подписей и читаемых надписей; "
    "газетная фактура передаётся растром и нечитаемыми следами печати, а не словами.\n\n"
)


def resolve_image_style_id(catalog, frame_key, locations, style_id=None):
    bound = catalog.get("frame_bindings", {}).get(frame_key)
    if bound:
        return bound
    if style_id and style_id != catalog["default_style_id"]:
        return style_id
    for location in locations or []:
        name = location.get("name") if isinstance(location, dict) else location
        bound = catalog.get("location_bindings", {}).get(name)
        if bound:
            return bound
    return style_id or catalog["default_style_id"]


def ensure_image_style_catalog(base: str, style_images: Dict[str, Any]) -> Dict[str, Any]:
    """Однократно очищает стили; привязки кадров впоследствии применяет код."""
    path = f"{base}/30_style/image_styles.json"
    existing = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
        if existing.get("version") == _IMAGE_STYLE_CATALOG_VERSION:
            return existing
    if not style_images:
        return existing
    locations_path = Path(base) / "20_bible/locations.json"
    locations = json.loads(locations_path.read_text(encoding="utf-8")) if locations_path.is_file() else []
    response = call_openai_api(
        prompt=json.dumps({"style_images": style_images,
                           "locations": [{k: location.get(k) for k in ("name", "description")} for location in locations],
                           "existing_styles": existing.get("styles", {})}, ensure_ascii=False),
        system_prompt=(
            "Подготовь переиспользуемый каталог художественных стилей изображений из исходной библии. "
            "Верни JSON: {\"default_style_id\":\"main\",\"styles\":{\"main\":{\"prompt\":\"...\"}},\"location_bindings\":{\"точное имя локации\":\"style_id\"}}. "
            "prompt — готовое описание художественной техники, носителя, фактуры, характера линии, палитры, света и обработки. "
            "Пиши на языке исходных материалов. Сохрани явно заданный автором стиль. "
            "Основной стиль содержит общую технику проекта, без перечисления всех локационных палитр. "
            "Явно заданные различия палитры, света и обработки для разных миров также являются отдельными стилями, "
            "даже при общей технике. Каждый вариант содержит полную общую технику и только свой колорит и обработку. "
            "Не создавай вариант автоматически для каждой локации или персонажа: только по исходным отличиям. "
            "Привяжи КАЖДУЮ переданную локацию по её точному имени к готовому варианту; несколько локаций могут использовать один стиль. "
            "Существующий основной style_id не переименовывай. "
            "Не включай сюжет, предметы, персонажей, будущие события, монтаж, звук, имя модели, "
            "производственные запреты или названия произведений-референсов: вырази их визуальные признаки. "
            "Не назначай стили кадрам: привязки использует код."
        ),
        model=model_code,
        max_tokens=8000,
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    catalog = parse_llm_json(response)
    styles = catalog.get("styles") or {}
    if catalog.get("default_style_id") not in styles or not all(
        isinstance(v, dict) and str(v.get("prompt") or "").strip() for v in styles.values()
    ):
        raise ValueError("image_styles: missing default style or empty style prompt")
    for style_id, style in existing.get("styles", {}).items():
        styles.setdefault(style_id, style)
    catalog["default_style_id"] = existing.get("default_style_id", catalog["default_style_id"])
    location_bindings = catalog.setdefault("location_bindings", {})
    if set(location_bindings) != {location["name"] for location in locations} or any(
        style_id not in styles for style_id in location_bindings.values()
    ):
        raise ValueError("image_styles: unknown location or style binding")
    catalog["version"] = _IMAGE_STYLE_CATALOG_VERSION
    catalog["frame_bindings"] = {
        key: style_id for key, style_id in existing.get("frame_bindings", {}).items()
        if len(existing.get("styles", {})) > 1 or style_id != existing.get("default_style_id")
    }
    if "page_bindings" in existing:
        catalog["page_bindings"] = existing["page_bindings"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)
    return catalog


def ensure_video_style_references(base, catalog, style_ids, session_id, seed, items, width=1920, height=1088):
    """Generate one dedicated style image per requested catalog style, reusing completed files."""
    from custom_tools.image_tools import edit_image_vse_tool
    from .background_references import _reference_path

    if not style_ids:
        return {}

    def render(style_id):
        candidates = {"characters": Counter(), "locations": Counter()}
        for item in items:
            key = f"{item['scene_number']}:{item['shot_number']}:{item.get('shot_type', 'start')}"
            if resolve_image_style_id(catalog, key, item.get("locations"), item.get("style_id")) != style_id:
                continue
            for field, counts in candidates.items():
                counts.update({str(_reference_path(Path(base), entry["reference_image_path"]))
                               for entry in item.get(field) or [] if entry.get("reference_image_path")
                               and _reference_path(Path(base), entry["reference_image_path"]).is_file()})
        if not all(candidates.values()):
            raise ValueError(f"Style {style_id} requires at least one character and one location reference")
        inputs = [min(counts, key=lambda path: (-counts[path], path)) for counts in candidates.values()]
        prompt = _STYLE_REFERENCE_INSTRUCTION + catalog["styles"][style_id]["prompt"]
        digest = hashlib.sha256(json.dumps([style_id, prompt, seed, width, height,
                                           os.getenv("IMG2IMG_MODEL") or os.getenv("VSEGPT_IMG2IMG_MODEL"),
                                           [(path, hashlib.sha256(Path(path).read_bytes()).hexdigest()) for path in inputs]],
                                          ensure_ascii=False).encode()).hexdigest()
        path = Path(base) / "30_style/references" / f"style_{digest[:16]}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            logger.info("🎨 Generating dedicated video style reference: style=%s size=%sx%s seed=%s path=%s",
                        style_id, width, height, seed, path)
            logger.info("🎨 Style sources: character=%s location=%s", inputs[0], inputs[1])
            result = edit_image_vse_tool(prompt=prompt, session_id=session_id, image_paths=inputs, output_path=str(path),
                                        width=width, height=height, seed=seed)
            if not path.is_file():
                raise RuntimeError(f"Style reference was not generated: {result}")
        return style_id, str(path.resolve())

    with ThreadPoolExecutor(max_workers=4) as executor:
        references = dict(executor.map(render, sorted(set(style_ids))))
    catalog.setdefault("reference_images", {}).update(references)
    (Path(base) / "30_style/image_styles.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    return references


def style_keeper_tool(session_id: str, project_id: str) -> str:
    """Фиксирует style_text.json, style_images.json, negative_prompt_list.txt.

    Args:
        project_id (str): Идентификатор проекта. Используется для чтения материалов
            из `10_synopsis` и `20_bible` и записи итогов в `30_style`.
        session_id (str): Идентификатор сессии.

    Returns:
        str: Путь к каталогу `30_style` с итоговыми файлами.
    """
    base = str(safe_storybook_project_dir(project_id))
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
    system = f"""Ты арт-директор. Верни JSON с полями style_text, style_preset_id, style_overrides, source_style_quote и negative_list.

{catalog_note}

style_text: {{ narrative_voice, sentence_length, vocabulary_bounds }}
Исходный явно заданный визуальный стиль в brief.storybook_prompt имеет приоритет над каталогом. Не подменяй его похожим пресетом. Это относится и к стилю, названному рекомендуемым в переданном сценарии.
source_style_quote: если исходник явно задаёт стиль, скопируй одну короткую непрерывную фразу из brief.storybook_prompt, без перефразирования. Не цитируй пересказ из synopsis или description. Иначе верни пустую строку.
style_preset_id: ровно один ближайший id из каталога; при явно заданном исходном стиле это только базовый шаблон, а не замена исходной техники.
style_overrides: поля color_palette (список), composition_rules (список), lighting, detail_density, do_not_include (список), project_note. Модель генерации не выбирай: она задаётся настройками проекта. Если source_style_quote непустая, ОБЯЗАТЕЛЬНО добавь art_style, texture и material_signature: точно опиши исходную технику, фактуру и материал; не переноси в них технику и ограничения выбранного шаблона. Без исходной цитаты эти три поля не переопределяй.
negative_list: строка с запятыми — запреты, специфичные для ЭТОЙ истории (нежелательные объекты, сущности, мотивы). Технические запреты каталог добавит сам. При source_style_quote стилевые запреты должны следовать исходной технике; ограничения выбранного пресета не применяются.

ПРИМЕР КОРРЕКТНОГО ФОРМАТА ОТВЕТА:
{{
  "style_text": {{
    "narrative_voice": "мягкий",
    "sentence_length": "средняя",
    "vocabulary_bounds": "простой"
  }},
  "style_preset_id": "storybook_watercolor",
  "source_style_quote": "",
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
    brief = {}
    if os.path.exists(f"{base}/00_brief.json"):
        with open(f"{base}/00_brief.json", "r", encoding="utf-8") as f:
            brief = json.load(f)
    prompt = json.dumps({
        "brief": brief,
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
    source_quote = str(data.get("source_style_quote") or "").strip()
    if source_quote:
        source_text = " ".join(str(brief.get("storybook_prompt") or "").replace("**", "").split()).casefold()
        quote_text = " ".join(source_quote.replace("**", "").split()).casefold()
        if quote_text not in source_text:
            raise ValueError(f"style_keeper: цитата исходного стиля не найдена в задании: {source_quote!r}")
        overrides = data.get("style_overrides") or {}
        for field in ("art_style", "texture", "material_signature"):
            value = str(overrides.get(field) or "").strip()
            if not value:
                raise ValueError(f"style_keeper: отсутствует исходное описание {field}")
            style_images[field] = value
        profile = get_active_style_profile()
        style_images["source_style_quote"] = source_quote
        style_images["camera_defaults"] = {}
        style_images["style_pitfalls"] = list(profile.universal_pitfalls)
        negative_terms = list(profile.universal_negative_seed)
    style_images["model"] = os.getenv("TEXT2IMAGE_MODEL", "").strip() or style_images["model"]
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

