import json
import re
import os
import random
import logging
from typing import Any, Dict, List, Optional

from agent_command import model_hard
from utils import call_openai_api, parse_llm_json

logger = logging.getLogger(__name__)


def _extract_image_path_from_prompt(prompt: str) -> Optional[str]:
    """Пытается извлечь путь к изображению из текста промпта.
    Поддерживает абсолютные и относительные POSIX/Windows пути и file:// ссылки.
    """
    if not prompt:
        return None

    # file:// URI
    m = re.search(r"file://([^\s]+)", prompt)
    if m:
        return m.group(1)

    # Абсолютные/относительные пути, грубая эвристика (наличие / или \\ и расширения)
    candidates = re.findall(r"[\w\-/\\\.:]+\.(?:png|jpg|jpeg|webp)", prompt, flags=re.IGNORECASE)
    return candidates[0] if candidates else None


def brief_from_prompt_tool(
    session_id: str,
    project_id: str,
    storybook_prompt: str,
    pages_min: Optional[int] = 9,
    pages_max: Optional[int] = 12,
    words_per_page_min: Optional[int] = 400,
    words_per_page_max: Optional[int] = 450,
    language: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Генерирует brief JSON для init_project на основе текстового промпта с использованием LLM.

    Args:
        session_id: Идентификатор сессии для трассировки выполнения.
        project_id: Идентификатор проекта (используется как название книги).
        storybook_prompt: Текстовый промпт с идеей книги и деталями.
        pages_min: Минимальное количество страниц.
        pages_max: Максимальное количество страниц.
        words_per_page_min: Минимальное количество слов на страницу.
        words_per_page_max: Максимальное количество слов на страницу.
        language: Язык книги (если не указан — по умолчанию 'ru').

    Политика значений:
    - language: если не указан — 'ru'
    - genre: определить из промпта
    - target_age: если не указан явно — определить из промпта (например, '5-7', '6-8')
    - pages_min/pages_max: 9 и 12 соответственно
    - words_per_page_min/words_per_page_max: {words_per_page_min} и {words_per_page_max}
    - title: сгенерировать по промпту
    - description, main_characters, main_locations, moral: извлечь/синтезировать из промпта
    - protagonist_picture: если найден путь в промпте — вернуть, иначе пропустить ключ

    Returns:
        Словарь с брифом.
    """

    story_path = f"plots/storybooks/{project_id}/00_brief.json"
    # Проверяем, существует ли уже история
    if os.path.exists(story_path):
        logger.info(f"📖 История уже существует: {story_path}, пропускаем генерацию")
        with open(story_path, "r", encoding="utf-8") as f:
            brief = json.load(f)
        return brief

    system = (
        "Ты опытный литературный редактор. По данному текстовому промпту сформируй бриф для пайплайна.\n"
        "ВАЖНО: Определи жанр (genre) и целевую аудиторию (target_age) СТРОГО из промпта.\n"
        "Если в промпте указан возраст аудитории (например, '18+', 'для взрослых', '6-8 лет') — "
        "используй именно его. Не подменяй взрослый контент детским и наоборот.\n"
        "target_age всегда возвращай как СТРОКУ (например '18+', '6-8', '12+').\n"
        "Верни ТОЛЬКО валидный JSON без комментариев и лишнего текста со следующими полями: \n"
        "language (str), genre (str), target_age (str), pages_min (int), pages_max (int), \n"
        "words_per_page_min (int), words_per_page_max (int), title (str), description (str), \n"
        "main_characters (array of str), main_locations (array of str), moral (str).\n"
        "ПРИМЕР КОРРЕКТНОГО ФОРМАТА ОТВЕТА:\n"
        "{\"language\":\"ru\",\"genre\":\"fantasy\",\"target_age\":\"6-8\",\"pages_min\":8,\"pages_max\":10,"
        "\"words_per_page_min\":120,\"words_per_page_max\":180,"
        "\"title\":\"Название\",\"description\":\"Описание\","
        "\"main_characters\":[\"Герой\"],\"main_locations\":[\"Лес\"],\"moral\":\"Мораль\"}\n"
        "КРИТИЧНО: `main_characters` и `main_locations` — массивы строк."
        f"Правила: language по умолчанию 'ru', pages_min={pages_min}, pages_max={pages_max}, words_per_page_min={words_per_page_min}, words_per_page_max={words_per_page_max}.\n"
        "Определи genre, target_age, title, description, main_characters, main_locations, moral по смыслу промпта."
    )

    user = json.dumps({
        "project_id": project_id,
        "prompt": storybook_prompt,
        "hints": {
            "language": language or "ru",
            "pages_min": pages_min,
            "pages_max": pages_max,
            "words_per_page_min": words_per_page_min,
            "words_per_page_max": words_per_page_max,
        },
    }, ensure_ascii=False)

    # Основная генерация через LLM
    resp = call_openai_api(
        prompt=user,
        system_prompt=system,
        model=model_hard,
        max_tokens=32768,
        temperature=0.6,
        response_format={"type": "json_object"},
    )
    data = parse_llm_json(resp)
    if not data:
        logger.error(f"brief_from_prompt: не удалось распарсить JSON ответа, resp={resp[:500] if isinstance(resp, str) else resp}")

    # Постобработка и гарантии значений
    def _str(v: Any, default: str = "") -> str:
        if v is None:
            return default
        s = str(v).strip()
        return s if s else default

    def _int(v: Any, default: int) -> int:
        try:
            return int(v)
        except Exception:
            return default

    def _list_str(v: Any) -> List[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            # разделение по запятым как fallback
            return [s.strip() for s in v.split(",") if s.strip()]
        return []

    brief: Dict[str, Any] = {}
    brief["language"] = _str(data.get("language"), language or "ru")
    brief["genre"] = _str(data.get("genre"), "fiction")
    brief["target_age"] = _str(data.get("target_age"), "all")

    pages_min_result = max(pages_min or 9, _int(data.get("pages_min"), pages_min or 9))
    pages_max_result = min(20, _int(data.get("pages_max"), pages_max or 12))
    if pages_max_result < pages_min_result:
        pages_max_result = max(pages_min_result, pages_max or 12)
    brief["pages_min"] = pages_min_result
    brief["pages_max"] = pages_max_result

    brief["words_per_page_min"] = words_per_page_min
    brief["words_per_page_max"] = words_per_page_max

    brief["title"] = _str(data.get("title"), project_id)
    brief["description"] = _str(data.get("description"), storybook_prompt)
    brief["main_characters"] = _list_str(data.get("main_characters"))
    brief["main_locations"] = _list_str(data.get("main_locations"))
    brief["moral"] = _str(data.get("moral"), "")
    brief["storybook_prompt"] = storybook_prompt

    # Если в промпте указан путь к картинке — добавляем protagonist_picture
    img_path = _extract_image_path_from_prompt(storybook_prompt)
    if img_path:
        brief["protagonist_picture"] = img_path
    
    # Генерируем seed для проекта
    brief["seed"] = random.randint(1, 1000000)
    logger.info(f"🎲 Сгенерирован seed для проекта: {brief['seed']}")

    return brief
