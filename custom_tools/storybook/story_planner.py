import json
import os
import logging
from typing import Dict, Any
from utils import call_openai_api, extract_json_from_markdown
from agent_command import model_code

logger = logging.getLogger(__name__)


def _preview_text(value: Any, limit: int = 1000) -> str:
    """Короткое превью для безопасного логирования больших/битых ответов."""
    text = "" if value is None else str(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.3):]
    return f"{head}...<truncated>...{tail}"


def story_planner_tool(session_id: str, project_id: str, task: str = None) -> str:
    """Генерирует synopsis.json и beats.json на основе 00_brief.json и дополнительного task.
    Сохраняет в plots/storybooks/{project_id}/10_synopsis/.

    Args:
        project_id (str): Идентификатор проекта. Используется для определения
            базового каталога `plots/storybooks/{project_id}` и чтения `00_brief.json`.
        session_id (str): Идентификатор сессии.
        task (str): Дополнительные детали для генерации сказки (опционально).

    Returns:
        str: Путь к каталогу с материалами синопсиса (`10_synopsis`).
    """
    base = f"plots/storybooks/{project_id}"
    brief_path = f"{base}/00_brief.json"
    synopsis_path = f"{base}/10_synopsis/synopsis.json"
    beats_path = f"{base}/10_synopsis/beats.json"
    
    # Проверяем, существуют ли уже файлы
    if os.path.exists(synopsis_path) and os.path.exists(beats_path):
        logger.info(f"📖 Синопсис и биты уже существуют, пропускаем генерацию")
        return f"Файлы уже существуют: {synopsis_path}, {beats_path}"
    if not os.path.exists(brief_path):
        raise FileNotFoundError(f"Brief not found: {brief_path}")
    with open(brief_path, "r", encoding="utf-8") as f:
        brief = json.load(f)

    # Извлекаем ограничения по страницам из brief
    pages_min = brief.get("pages_min", 9)
    pages_max = brief.get("pages_max", 12)
    target_age = brief.get("target_age", "all")
    genre = brief.get("genre", "fiction")
    
    system = f"""Ты опытный литературный редактор. Пиши по-русски. Соблюдай возрастные ограничения, жанр и структуру арки. Строго следи за объемом произведения.

Жанр: {genre}.

ВАЖНО: История должна содержать от {pages_min} до {pages_max} страниц. Целевая аудитория: {target_age} лет.

Ответ возвращай ТОЛЬКО в формате JSON c полями synopsis и beats.
synopsis: {{ logline, moral, description, target_age, vocabulary_limits }}
beats: [ {{ id, page_number, goal, characters_in_frame, key_object, emotion, location_hint, must_have_details, character_appearance_changes }} ]

КРИТИЧНО: В массиве beats должно быть точно от {pages_min} до {pages_max} элементов, где каждый элемент соответствует одной странице книги. 
Поле page_number должно идти последовательно от 1 до количества страниц.

ПРИМЕР КОРРЕКТНОГО ФОРМАТА ОТВЕТА (JSON OBJECT):
{{
  "synopsis": {{
    "logline": "Короткий логлайн",
    "moral": "Мораль истории",
    "description": "Краткое описание",
    "target_age": "6-8",
    "vocabulary_limits": "простые слова"
  }},
  "beats": [
    {{
      "id": "beat_1",
      "page_number": 1,
      "goal": "Цель страницы",
      "characters_in_frame": "Герой, друг",
      "key_object": "Ключевой объект",
      "emotion": "радость",
      "location_hint": "лесная поляна",
      "must_have_details": "что обязательно показать",
      "character_appearance_changes": "нет"
    }}
  ]
}}
КРИТИЧНО: `beats` ДОЛЖЕН быть массивом объектов, `page_number` — целое число."""
    
    # Создаем входные данные для генерации
    input_data = {
        "brief": brief,
        "pages_requirements": {
            "pages_min": pages_min,
            "pages_max": pages_max,
            "target_age": target_age
        }
    }
    if task:
        input_data["task"] = task
        system += f"\n\nДополнительные требования к истории: {task}"
    
    prompt = json.dumps(input_data, ensure_ascii=False)

    resp = call_openai_api(
        prompt=prompt,
        system_prompt=system,
        model=model_code,
        max_tokens=32768,
        temperature=0.4,
        response_format={"type": "json_object"}
    )
    if resp is None or (isinstance(resp, str) and not resp.strip()):
        raise RuntimeError(
            "story_planner_tool: получен пустой ответ от модели при генерации synopsis/beats"
        )

    try:
        if isinstance(resp, dict):
            data = resp
        else:
            raw_text = str(resp).strip()
            clean_json = extract_json_from_markdown(raw_text)
            data = json.loads(clean_json)
    except Exception as e:
        logger.error(
            "story_planner_tool: не удалось распарсить JSON ответа. "
            f"type={type(resp)}, preview='{_preview_text(resp)}', error={e}"
        )
        raise RuntimeError(
            "story_planner_tool: модель вернула невалидный JSON для synopsis/beats"
        ) from e

    syn_dir = f"{base}/10_synopsis"
    os.makedirs(syn_dir, exist_ok=True)
    with open(f"{syn_dir}/synopsis.json", "w", encoding="utf-8") as f:
        json.dump(data.get("synopsis", {}), f, ensure_ascii=False, indent=2)
    with open(f"{syn_dir}/beats.json", "w", encoding="utf-8") as f:
        json.dump(data.get("beats", []), f, ensure_ascii=False, indent=2)
    return syn_dir


