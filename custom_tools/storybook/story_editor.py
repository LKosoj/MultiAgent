import json
import os
from typing import Any, Dict, List, Optional

from agent_command import model_hard, model_ultimate
from utils import call_openai_api, extract_json_from_markdown
import logging

logger = logging.getLogger(__name__)


def story_editor_tool(
    session_id: str,
    project_id: str,
    force_edit: bool = False,
    chapter_numbers: Optional[List[int]] = None,
    edit_all_chapters: bool = False,
) -> str:
    """
    Редактирует и улучшает текст истории из story.json по главам.
    Проверяет стилистику, улучшает текст, добавляет детали.
    
    Работает только если story.json был перегенерирован или force_edit=True.
    
    Args:
        session_id: Идентификатор сессии (для трассировки, логирования).
        project_id: Идентификатор проекта.
        force_edit: Принудительное редактирование, даже если файл не был перегенерирован.
        chapter_numbers: Список номеров глав для редактирования (None = все главы).
        edit_all_chapters: Если True, редактировать все главы сразу. Если False, редактировать по одной главе.
    
    Returns:
        Путь к отредактированному файлу story.json.
    """
    base = f"plots/storybooks/{project_id}"
    story_path = f"{base}/20_story/story.json"
    
    if not os.path.exists(story_path):
        raise FileNotFoundError(f"Файл story.json не найден: {story_path}")
    
    # Читаем текущую историю
    with open(story_path, "r", encoding="utf-8") as f:
        story_data = json.load(f)
    
    title = story_data.get("title", "")
    pages = story_data.get("pages", [])
    
    if not pages:
        logger.warning("История не содержит страниц, редактирование пропущено")
        return story_path

    # Определяем какие главы редактировать
    if chapter_numbers is None:
        # Если не указаны номера глав, берем все
        chapters_to_edit = list(range(1, len(pages) + 1))
    else:
        # Проверяем валидность номеров глав
        chapters_to_edit = []
        for chapter_num in chapter_numbers:
            if 1 <= chapter_num <= len(pages):
                chapters_to_edit.append(chapter_num)
            else:
                logger.warning(f"Некорректный номер главы: {chapter_num}. Доступны главы 1-{len(pages)}")
        
        if not chapters_to_edit:
            logger.warning("Не найдено валидных номеров глав для редактирования")
            return story_path

    # Читаем данные из библии проекта для контекста
    bible_dir = f"{base}/20_bible"
    characters_data = {}
    consistency_rules = {}
    locations_data = {}
    style_data = {}
    
    # Загружаем контекстные данные
    for filename, dest in [
        ("characters.json", "characters"),
        ("consistency_rules.json", "consistency"),
        ("locations.json", "locations"),
    ]:
        file_path = f"{bible_dir}/{filename}"
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    _data = json.load(f)
                if dest == "characters":
                    characters_data = _data
                elif dest == "consistency":
                    consistency_rules = _data
                elif dest == "locations":
                    locations_data = _data
            except Exception as e:
                logger.warning(f"Не удалось загрузить {filename}: {e}")
    
    # Проверяем стиль из 30_style
    style_path = f"{base}/30_style/style.json"
    if os.path.exists(style_path):
        try:
            with open(style_path, "r", encoding="utf-8") as f:
                style_data = json.load(f)
        except Exception as e:
            logger.warning(f"Не удалось загрузить style.json: {e}")
    
    # Читаем бриф для получения настроек
    brief_path = f"{base}/00_brief.json"
    brief: Dict[str, Any] = {}
    if os.path.exists(brief_path):
        try:
            with open(brief_path, "r", encoding="utf-8") as f:
                brief = json.load(f)
        except Exception:
            brief = {}
    
    lang = brief.get("language", "ru")
    age = brief.get("target_age", "all")
    genre = brief.get("genre", "fiction")
    
    # Создаем копию данных для редактирования
    edited_pages = pages.copy()
    edited_title = title

    # Формируем системный промпт для редактирования
    system_parts = [
        f"Ты опытный редактор книг и мастер стилистики. Жанр: {genre}.",
        f"Язык: {lang}. Целевая аудитория: {age}.",
        "Твоя задача: отредактировать и улучшить текст глав истории.",
        "",
        "ОБЯЗАТЕЛЬНЫЕ ТРЕБОВАНИЯ:",
        "1. Сохрани общий сюжет и структуру главы",
        "2. Улучши стилистику и читаемость текста",
        "3. Добавь живые детали и образные описания",
        "4. Сделай диалоги более естественными",
        "5. Усиль эмоциональную составляющую",
        "6. Соблюдай консистентность персонажей и локаций",
        "7. Проверь грамматику и пунктуацию",
        "",
        "СТИЛИСТИЧЕСКИЕ ТРЕБОВАНИЯ:",
        "- Используй язык, соответствующий возрасту целевой аудитории",
        "- Добавляй сенсорные детали (звуки, запахи, тактильные ощущения)",
        "- Делай текст более живым и увлекательным",
        "- Проверяй испольуемые образы, действия и элементы на корректность (например, небо не может быть голубым как морковка).",
        "- Избегай тона ИИ: слишком формального, отточенного или шаблонного выражения.",
        "- Каждое предложение должно быть осознанным, а не механически сгенерированным.",
        "Не используй длинные тире, чрезмерные кавычки, корпоративный жаргон или бюрократический язык."
        "",
    ]

    if edit_all_chapters:
        # Редактируем все главы сразу
        system_parts.extend([
            "СТРОГО верни JSON в формате: {\"title\": str, \"pages\": [{\"page\": int, \"title\": str, \"body\": str}]}",
            "ПРИМЕР: {\"title\":\"Название\",\"pages\":[{\"page\":1,\"title\":\"Глава 1\",\"body\":\"Текст\"}]}",
            "КРИТИЧНО: `pages` — массив объектов, `page` — целое число.",
            "Не добавляй комментарии или лишние поля в JSON."
        ])
        
        system_prompt = "\n".join(system_parts)
        
        # Фильтруем страницы для редактирования
        pages_to_edit = [page for page in pages if page["page"] in chapters_to_edit]
        
        payload_json = {
            "story": {
                "title": title,
                "pages": pages_to_edit
            },
            "context": {
                "characters": characters_data,
                "locations": locations_data,
                "consistency_rules": consistency_rules,
                "style_guidelines": style_data,
                "brief": brief
            },
            "instruction": f"Отредактируй и улучши текст глав {chapters_to_edit}, сохранив структуру и сюжет."
        }
        
        # Вызываем ИИ для редактирования
        edited_result = _edit_chapters_batch(payload_json, system_prompt)
        if edited_result:
            edited_title = edited_result.get("title", title)
            edited_chapters = edited_result.get("pages", [])
            
            # Обновляем отредактированные главы в общем списке
            for edited_chapter in edited_chapters:
                chapter_num = edited_chapter["page"]
                for i, page in enumerate(edited_pages):
                    if page["page"] == chapter_num:
                        edited_pages[i] = edited_chapter
                        break
    
    else:
        # Редактируем по одной главе
        system_parts.extend([
            "СТРОГО верни JSON в формате: {\"page\": int, \"title\": str, \"body\": str}",
            "ПРИМЕР: {\"page\":1,\"title\":\"Глава 1\",\"body\":\"Отредактированный текст\"}",
            "Не добавляй комментарии или лишние поля в JSON."
        ])
        
        system_prompt = "\n".join(system_parts)
        
        for chapter_num in chapters_to_edit:
            # Находим главу для редактирования
            chapter_to_edit = None
            chapter_index = None
            for i, page in enumerate(pages):
                if page["page"] == chapter_num:
                    chapter_to_edit = page
                    chapter_index = i
                    break
            
            if chapter_to_edit is None:
                logger.warning(f"Глава {chapter_num} не найдена")
                continue
                
            payload = {
                "chapter": chapter_to_edit,
                "context": {
                    "story_title": title,
                    "all_chapters": [{"page": p["page"], "title": p["title"]} for p in pages],
                    "characters": characters_data,
                    "locations": locations_data,
                    "consistency_rules": consistency_rules,
                    "style_guidelines": style_data,
                    "brief": brief
                },
                "instruction": f"Отредактируй и улучши текст главы {chapter_num}, сохранив её структуру и роль в общем сюжете."
            }
            
            # Редактируем главу
            edited_chapter = _edit_single_chapter(payload, system_prompt, chapter_num)
            if edited_chapter:
                edited_pages[chapter_index] = edited_chapter

    # Сохраняем отредактированную историю
    edited_story = {
        "title": edited_title,
        "pages": edited_pages
    }
    
    # Создаем rolling backup: каждый запуск сохраняет предыдущую версию с timestamp.
    import shutil
    from datetime import datetime as _dt
    _ts = _dt.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = f"{story_path}.backup_{_ts}"
    _n = 1
    while os.path.exists(backup_path):
        backup_path = f"{story_path}.backup_{_ts}_{_n}"
        _n += 1
    shutil.copy2(story_path, backup_path)
    logger.info(f"Создан бэкап предыдущей версии: {backup_path}")
    
    # Сохраняем отредактированную версию
    with open(story_path, "w", encoding="utf-8") as f:
        json.dump(edited_story, f, ensure_ascii=False, indent=2)
    
    logger.info(f"✅ История отредактирована и сохранена: {story_path}")
    return story_path


def _edit_chapters_batch(payload: Dict[str, Any], system_prompt: str) -> Optional[Dict[str, Any]]:
    """
    Редактирует несколько глав за один вызов ИИ.
    
    Args:
        payload: Данные для редактирования
        system_prompt: Системный промпт
        
    Returns:
        Отредактированные данные или None в случае ошибки
    """
    max_attempts = 3
    last_err: Exception | None = None
    
    for attempt in range(max_attempts):
        try:
            logger.info(f"Редактирование глав (батч), попытка {attempt + 1}/{max_attempts}")
            
            resp = call_openai_api(
                prompt=json.dumps(payload, ensure_ascii=False),
                system_prompt=system_prompt,
                model=model_ultimate,
                max_tokens=32768,
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            
            # Извлекаем JSON из markdown блока если нужно
            clean_resp = extract_json_from_markdown(resp)
            
            edited_data = json.loads(clean_resp)
            
            # Проверяем валидность
            if "pages" in edited_data and isinstance(edited_data["pages"], list):
                return edited_data
            else:
                logger.warning(f"Неверный формат ответа на попытке {attempt + 1}")
                continue
            
        except Exception as e:
            last_err = e
            logger.warning(f"Ошибка редактирования (батч), попытка {attempt + 1}: {e}")
            continue
    
    logger.error(f"Не удалось отредактировать главы (батч): {last_err}")
    return None


def _edit_single_chapter(payload: Dict[str, Any], system_prompt: str, chapter_num: int) -> Optional[Dict[str, Any]]:
    """
    Редактирует одну главу.
    
    Args:
        payload: Данные для редактирования
        system_prompt: Системный промпт
        chapter_num: Номер главы
        
    Returns:
        Отредактированная глава или None в случае ошибки
    """
    max_attempts = 3
    last_err: Exception | None = None
    
    for attempt in range(max_attempts):
        try:
            logger.info(f"Редактирование главы {chapter_num}, попытка {attempt + 1}/{max_attempts}")
            
            resp = call_openai_api(
                prompt=json.dumps(payload, ensure_ascii=False),
                system_prompt=system_prompt,
                model=model_ultimate,
                max_tokens=32768,
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            
            # Извлекаем JSON из markdown блока если нужно
            clean_resp = extract_json_from_markdown(resp)
            
            edited_chapter = json.loads(clean_resp)
            
            # Проверяем валидность
            required_fields = ["page", "title", "body"]
            if all(field in edited_chapter for field in required_fields):
                return edited_chapter
            else:
                logger.warning(f"Неверный формат главы {chapter_num} на попытке {attempt + 1}")
                continue
            
        except Exception as e:
            last_err = e
            logger.warning(f"Ошибка редактирования главы {chapter_num}, попытка {attempt + 1}: {e}")
            logger.info(f"Получен ответ для главы {chapter_num}: {resp[:500]}{'...' if len(resp) > 500 else ''}")
            continue
    
    logger.error(f"Не удалось отредактировать главу {chapter_num}: {last_err}")
    return None
