import json
import os
from typing import Any, Dict, List, Optional

from agent_command import model_hard, model_ultimate
from utils import call_openai_api, extract_json_from_markdown
import logging

logger = logging.getLogger(__name__)


def story_writer_tool(
    session_id: str,
    project_id: str,
    language: Optional[str] = None,
    target_age: Optional[str] = None,
    words_per_page_min: Optional[int] = None,
    words_per_page_max: Optional[int] = None,
    tone: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Генерирует структурированный текст истории по страницам на основе synopsis.json, beats.json 
    и данных из библии проекта (characters.json, locations.json, consistency_rules.json).

    Сохраняет результат в plots/storybooks/{project_id}/20_story/story.json в формате:
    {
      "title": str,
      "pages": [ { "page": int, "title": str, "body": str } ]
    }

    Args:
        session_id: Идентификатор сессии (для трассировки, логирования).
        project_id: Идентификатор проекта.
        language: Язык текста (например, ru, en); по умолчанию из 00_brief.json.
        target_age: Целевая аудитория по возрасту; по умолчанию из 00_brief.json.
        words_per_page_min: Желаемый минимум слов на страницу (мягкое ограничение).
        words_per_page_max: Желаемый максимум слов на страницу (мягкое ограничение).
        tone: Тон повествования (например, дружелюбный, сказочный).

    Returns:
        Словарь с информацией о результате:
        {
            "story_path": str,  # Путь к файлу story.json
            "regenerated": bool  # True, если история была перегенерирована
        }
    """
    base = f"plots/storybooks/{project_id}"
    syn_dir = f"{base}/10_synopsis"
    story_path = f"{base}/20_story/story.json"
    
    # Проверяем, существует ли уже история
    if os.path.exists(story_path):
        logger.info(f"📖 История уже существует: {story_path}, пропускаем генерацию")
        return {
            "story_path": story_path,
            "regenerated": False
        }
    synopsis_path = f"{syn_dir}/synopsis.json"
    beats_path = f"{syn_dir}/beats.json"
    if not os.path.exists(synopsis_path) or not os.path.exists(beats_path):
        raise FileNotFoundError("Отсутствуют synopsis.json или beats.json. Сначала выполните story_planner_tool.")

    with open(synopsis_path, "r", encoding="utf-8") as f:
        synopsis = json.load(f)
    with open(beats_path, "r", encoding="utf-8") as f:
        beats = json.load(f)

    # Читаем данные из библии проекта (20_bible)
    bible_dir = f"{base}/20_bible"
    characters_data = {}
    consistency_rules = {}
    locations_data = {}
    
    characters_path = f"{bible_dir}/characters.json"
    rules_path = f"{bible_dir}/consistency_rules.json"
    locations_path = f"{bible_dir}/locations.json"
    
    if os.path.exists(characters_path):
        try:
            with open(characters_path, "r", encoding="utf-8") as f:
                characters_data = json.load(f)
        except Exception as e:
            logger.warning(f"Не удалось загрузить characters.json: {e}")
    
    if os.path.exists(rules_path):
        try:
            with open(rules_path, "r", encoding="utf-8") as f:
                consistency_rules = json.load(f)
        except Exception as e:
            logger.warning(f"Не удалось загрузить consistency_rules.json: {e}")
    
    if os.path.exists(locations_path):
        try:
            with open(locations_path, "r", encoding="utf-8") as f:
                locations_data = json.load(f)
        except Exception as e:
            logger.warning(f"Не удалось загрузить locations.json: {e}")

    # Подтягиваем бриф, чтобы не хардкодить язык/возраст
    brief_path = f"{base}/00_brief.json"
    brief: Dict[str, Any] = {}
    if os.path.exists(brief_path):
        try:
            with open(brief_path, "r", encoding="utf-8") as f:
                brief = json.load(f)
        except Exception:
            brief = {}

    lang = language or brief.get("language") or "ru"
    age = target_age or brief.get("target_age") or "all"
    min_w = words_per_page_min or brief.get("words_per_page_min") or None
    max_w = words_per_page_max or brief.get("words_per_page_max") or None
    tone_text = tone or brief.get("tone") or "увлекательный, соответствующий жанру и аудитории"

    # Формируем гибкую системную инструкцию без жёстких ограничений
    sys_parts: List[str] = []
    genre = brief.get("genre", "fiction")
    sys_parts.append(f"Ты самый лучший писатель и редактор. Жанр: {genre}.")
    if lang:
        if lang.lower().startswith("ru"):
            sys_parts.append("Пиши по-русски, фразами, соответствующими возрасту целевой аудитории.")
        else:
            sys_parts.append("Пиши на указанном языке, фразами, соответствующими возрасту целевой аудитории.")
    if age:
        sys_parts.append(f"Аудитория: {age}.")
    sys_parts.append(f"Тон: {tone_text}.")
    if min_w or max_w:
        if min_w and max_w:
            sys_parts.append(f"ВАЖНО: Каждая страница должна содержать {min_w}-{max_w} слов. Это критически важно для качества книги!")
        elif min_w:
            sys_parts.append(f"ВАЖНО: Каждая страница должна содержать не менее {min_w} слов. Это критически важно для качества книги!")
        elif max_w:
            sys_parts.append(f"ВАЖНО: Каждая страница должна содержать не более {max_w} слов. Это критически важно для качества книги!")
    sys_parts.append(
        "ОБЯЗАТЕЛЬНО используй данные о персонажах, локациях и правилах консистентности из библии проекта при написании истории."
    )
    sys_parts.append(
        "Соблюдай описания персонажей, их характеры и внешность. Используй описанные локации."
    )
    sys_parts.append(
        "Сначала продумай историю, учти все описанные персонажи и локации, затем напиши её."
    )
    sys_parts.append(
        "Не предсказывай будущее истории, пиши то, что уже произошло или происходит в истории."
    )
    sys_parts.append(
        "Строго придерживайся структуры арки и сюжета."
    )
    sys_parts.append(
        "Варьируй длину и ритм предложений, чтобы текст казался живым."
    )
    sys_parts.append(
        "Не используй длинные тире, чрезмерные кавычки, корпоративный жаргон или бюрократический язык."
    )
    sys_parts.append(
        "Избегай тона ИИ: слишком формального, отточенного или шаблонного выражения."
    )
    sys_parts.append(
        "Каждое предложение должно быть осознанным, а не механически сгенерированным."
    )
    sys_parts.append(
        f"ВАЖНО: Создай отдельную страницу для каждого элемента из beats. Всего должно быть {len(beats)} страниц, пронумерованных от 1 до {len(beats)}."
    )
    sys_parts.append(
        "Каждая страница должна соответствовать своему beat: используй goal, characters_in_frame, key_object, emotion, location_hint и must_have_details из соответствующего beat."
    )
    sys_parts.append(
        "Строго верни валидный JSON только с полями title и pages; pages — массив объектов: { page, title, body }."
    )
    sys_parts.append(
        "ПРИМЕР КОРРЕКТНОГО ФОРМАТА ОТВЕТА: "
        "{\"title\":\"Название\",\"pages\":[{\"page\":1,\"title\":\"Заголовок страницы\",\"body\":\"Текст страницы\"}]}"
    )
    sys_parts.append(
        "КРИТИЧНО: `pages` ДОЛЖЕН быть массивом объектов, `page` — целое число."
    )
    sys_parts.append(
        "Избегай повторов. Без насилия и мрачных деталей. Без комментариев и лишних полей в JSON."
    )
    system = "\n".join(sys_parts)

    payload = {
        "synopsis": synopsis,
        "beats": beats,
        "characters": characters_data,
        "locations": locations_data,
        "consistency_rules": consistency_rules,
    }

    max_attempts = 3
    last_err: Exception | None = None
    data: Dict[str, Any] | None = None
    for _ in range(max_attempts):
        try:
            resp = call_openai_api(
                prompt=json.dumps(payload, ensure_ascii=False),
                system_prompt=system,
                model=model_ultimate,
                max_tokens=32768,  # Увеличено для длинных историй
                temperature=0.7,
                response_format={"type": "json_object"},
            )
            # Извлекаем JSON из markdown блока если нужно
            clean_resp = extract_json_from_markdown(resp)
            data = json.loads(clean_resp)
            break
        except Exception as e:
            last_err = e
            continue

    if data is None:
        raise ValueError(f"Не удалось сгенерировать текст истории: {last_err}")

    title = data.get("title") or synopsis.get("title") or project_id
    pages: List[Dict[str, Any]] = data.get("pages", [])

    out_dir = f"{base}/20_story"
    os.makedirs(out_dir, exist_ok=True)
    out_path = f"{out_dir}/story.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"title": title, "pages": pages}, f, ensure_ascii=False, indent=2)
    
    logger.info(f"✅ История сгенерирована: {out_path}")
    return {
        "story_path": out_path,
        "regenerated": True
    }


