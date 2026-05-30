import os
import json
import shutil
import logging
from typing import Any, Dict, Optional

from agent_factory import AgentFactory

logger = logging.getLogger(__name__)


def _ensure_parent_dir(path: Optional[str]) -> None:
    if not path:
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _parse_generated_path(agent_output: str, session_id: str) -> Optional[str]:
    import re, glob
    candidates = re.findall(r"[\w\-/\\\.]+\.png", str(agent_output), flags=re.IGNORECASE)
    for p in candidates:
        if os.path.exists(p):
            return p
    pattern = f"plots/image*{session_id}.png"
    files = glob.glob(pattern)
    if files:
        return max(files, key=os.path.getctime)
    return None


def protagonist_initializer_tool(session_id: str, project_id: str) -> str:
    """
    Инициализирует единое базовое изображение протагониста проекта.

    Логика:
    - Если в 00_brief.json указан путь к изображению протагониста (protagonist_picture | protagonist_image | protogonis_picture | hero_image)
      и файл существует — копируем его в 30_assets/protagonist/base.png
    - Иначе генерируем изображение протагониста через artist_agent.generate_image_tool на основе канона из 20_bible/characters.json

    Args:
        session_id: Идентификатор сессии (для трассировки, логирования).
        project_id: Идентификатор проекта.

    Returns:
        Абсолютный путь к созданному файлу base.png.
    """
    base_dir = f"plots/storybooks/{project_id}"
    protagonist_dir = f"{base_dir}/30_assets/protagonist"
    os.makedirs(protagonist_dir, exist_ok=True)
    base_path = f"{protagonist_dir}/base.png"
    
    # Проверяем, существует ли уже базовое изображение протаганиста
    if os.path.exists(base_path):
        logger.info(f"🎭 Базовое изображение протаганиста уже существует: {base_path}, пропускаем создание")
        return os.path.abspath(base_path)

    # 1) Проверяем brief на наличие картинки героя
    brief_path = f"{base_dir}/00_brief.json"
    brief: Dict[str, Any] = {}
    logger.debug(f"protagonist_initializer: читаем бриф из {brief_path}")
    if os.path.exists(brief_path):
        try:
            with open(brief_path, "r", encoding="utf-8") as f:
                brief = json.load(f)
            logger.debug(f"protagonist_initializer: бриф загружен, ключи: {list(brief.keys())}")
        except Exception as e:
            logger.debug(f"protagonist_initializer: ошибка чтения брифа: {e}")
            brief = {}
    else:
        logger.debug(f"protagonist_initializer: файл брифа не найден: {brief_path}")

    candidate_keys = [
        "protagonist_picture", "protagonist_image", "hero_image"
    ]
    src_image: Optional[str] = None
    for k in candidate_keys:
        v = brief.get(k)
        logger.debug(f"protagonist_initializer: проверяем ключ {k} = {v}")
        if isinstance(v, str) and v.strip():
            src_image = v.strip()
            logger.debug(f"protagonist_initializer: найден src_image = {src_image}")
            break

    if src_image and not os.path.isabs(src_image):
        # трактуем относительный путь относительно корня репозитория
        src_image = os.path.abspath(src_image)

    logger.debug(f"protagonist_initializer: src_image={src_image}")
    logger.debug(f"protagonist_initializer: файл существует={os.path.exists(src_image) if src_image else False}")

    if src_image and os.path.exists(src_image):
        _ensure_parent_dir(base_path)
        shutil.copy2(src_image, base_path)
        logger.debug(f"protagonist_initializer: файл скопирован в {base_path}")
        return os.path.abspath(base_path)

    # 2) Пытаемся сгенерировать героя на основе канона
    characters_path = f"{base_dir}/20_bible/characters.json"
    english_prompt = "Hero protagonist full-body, neutral pose, clean background"
    # Попробуем подмешать стили изображений и негативный список, если они есть
    style_images_path = f"{base_dir}/30_style/style_images.json"
    negative_list_path = f"{base_dir}/30_style/negative_prompt_list.txt"
    style_images: Dict[str, Any] = {}
    try:
        if os.path.exists(style_images_path):
            with open(style_images_path, "r", encoding="utf-8") as f:
                style_images = json.load(f) or {}
    except Exception:
        style_images = {}

    # Базовый негативный промпт + из файла + do_not_include из стиля
    negative_prompt = "watermark, text, logo, nsfw, lowres, extra limbs"
    try:
        if os.path.exists(negative_list_path):
            with open(negative_list_path, "r", encoding="utf-8") as f:
                nl = (f.read() or "").strip()
                if nl:
                    negative_prompt = nl
    except Exception:
        pass

    # Добавим явные запреты из style_images.do_not_include
    try:
        dni = style_images.get("do_not_include")
        if isinstance(dni, list) and dni:
            extra_neg = ", ".join([str(x) for x in dni if str(x).strip()])
            if extra_neg:
                if negative_prompt.strip():
                    negative_prompt = f"{negative_prompt}, {extra_neg}"
                else:
                    negative_prompt = extra_neg
    except Exception:
        pass
    if os.path.exists(characters_path):
        try:
            with open(characters_path, "r", encoding="utf-8") as f:
                chars = json.load(f)
            #берём первого персонажа как протагониста (или того, у кого role включает 'протаго')
            proto = None
            for c in chars:
                role = (c.get("role") or "").lower()
                if "протаго" in role or "hero" in role:
                    proto = c
                    break
            if proto is None and chars:
                proto = chars[0]
            if proto:
                name = proto.get("name") or "Hero"
                imm = proto.get("immutable_attributes", {})
                var = proto.get("variable_attributes", {})
                prompt = (
                    f"Full-body portrait of {name} as main protagonist, neutral pose, "
                    f"face_shape: {imm.get('face_shape','')}, eye_color: {imm.get('eye_color','')}, "
                    f"skin_tone: {imm.get('skin_tone','')}, body_proportions: {imm.get('body_proportions','')}, "
                    f"unique_features: {', '.join(imm.get('unique_features', []))}. "
                    f"base_clothing: {var.get('base_clothing','')}, base_hairstyle: {var.get('base_hairstyle','')}, "
                    f"accessories: {', '.join(var.get('accessories', []))}. "
                )

                # Встраиваем визуальный стиль из style_images
                try:
                    art_style = style_images.get("art_style")
                    color_palette = style_images.get("color_palette")
                    composition_rules = style_images.get("composition_rules")
                    lighting = style_images.get("lighting")
                    texture = style_images.get("texture")
                    detail_density = style_images.get("detail_density")
                    model_hint = style_images.get("model")

                    style_chunks = []
                    if art_style:
                        style_chunks.append(f"Art style: {art_style}.")
                    if color_palette:
                        style_chunks.append(f"Color palette: {color_palette}.")
                    if composition_rules:
                        style_chunks.append(f"Composition: {composition_rules}.")
                    if lighting:
                        style_chunks.append(f"Lighting: {lighting}.")
                    if texture:
                        style_chunks.append(f"Texture: {texture}.")
                    if detail_density:
                        style_chunks.append(f"Detail level: {detail_density}.")
                    if model_hint:
                        style_chunks.append(f"Prefer model: {model_hint}.")

                    if style_chunks:
                        prompt = f"{prompt} {' '.join(style_chunks)}"
                except Exception:
                    pass
        except Exception:
            pass

    # Вызываем artist_agent для генерации
    factory = AgentFactory()
    task = f"""
Ты — художник-иллюстратор (artist_agent). Сгенерируй изображение протагониста.

ВАЖНО: Перед генерацией усиль негативный промпт, добавив диаметрально противоположную стилистику к основному промпту.

Анализируй основной промпт: {prompt}

Если в основном промпте:
- реалистичное изображение → добавь в негативный промпт: cartoon, anime, stylized, illustrated, drawing
- мультяшный стиль → добавь: photorealistic, realistic, photography, hyperrealistic  
- детская иллюстрация → добавь: mature, adult, serious, dark, gritty
- темная атмосфера → добавь: bright, cheerful, colorful, happy, light
- минималистичный стиль → добавь: busy, cluttered, complex, detailed, ornate
- детализированный стиль → добавь: simple, minimal, basic, plain, undetailed

Базовый негативный промпт: "{negative_prompt}"

Используй generate_image_tool c параметрами:
  - prompt: "english_prompt" - строго на английском языке, его нужно сгенерировать на основе {prompt}
  - session_id: "{session_id}"
  - number: 1
  - negative_prompt: "усиленный_негативный_промпт" - базовый + противоположная стилистика
  - width: 1920
  - height: 1080
  - true_cfg_scale: 5.0
  - num_inference_steps: 50
  - output_path: "{base_path}"

В ответе верни только финальный путь к файлу.
"""
    agent = factory.create_agent(
        profile_type='artist_agent',
        session_id=session_id,
        task=task.strip(),
        pipeline_type='workflow'
    )
    output = agent.run(task.strip(), stream=False)
    gen_path = _parse_generated_path(str(output), session_id)
    if gen_path and os.path.exists(gen_path):
        try:
            if os.path.abspath(gen_path) != os.path.abspath(base_path):
                _ensure_parent_dir(base_path)
                shutil.move(gen_path, base_path)
        except Exception:
            pass

    return os.path.abspath(base_path)


