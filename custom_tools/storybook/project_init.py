import os
import json
import logging
import random
from typing import Dict, Any, Union

logger = logging.getLogger(__name__)


def project_init_tool(session_id: str, project_id: str, brief: Union[str, Dict[str, Any]]) -> str:
    """Создаёт структуру проекта и сохраняет 00_brief.json.

    Args:
        session_id: Идентификатор сессии для отслеживания выполнения задач
        project_id: ID проекта
        brief: словарь с брифом или JSON-строка

    Returns:
        Строка с путём к файлу brief.
    """
    # Если brief передан как строка, парсим JSON
    if isinstance(brief, str):
        try:
            brief = json.loads(brief)
        except json.JSONDecodeError as e:
            logger.error(f"❌ Ошибка парсинга JSON brief: {e}")
            logger.error(f"❌ Полученная строка: {repr(brief)}")
            raise ValueError(f"Некорректный JSON в параметре brief: {e}")
    
    # Отладочная информация
    logger.info(f"📋 Тип brief: {type(brief)}")
    if isinstance(brief, dict):
        logger.info(f"📋 Ключи brief: {list(brief.keys())}")
    else:
        logger.info(f"📋 Значение brief: {brief}")
    base = f"plots/storybooks/{project_id}"
    brief_path = f"{base}/00_brief.json"
    
    # Проверяем, существует ли уже бриф
    if os.path.exists(brief_path):
        logger.info(f"📄 Бриф уже существует: {brief_path}, пропускаем создание")
        return brief_path
    
    structure = [
        base,
        f"{base}/10_synopsis",
        f"{base}/20_bible/references/characters",
        f"{base}/20_bible/references/locations",
        f"{base}/30_style",
        f"{base}/40_prompts",
        f"{base}/50_images",
        f"{base}/60_layout/preview",
    ]
    for d in structure:
        os.makedirs(d, exist_ok=True)

    brief_path = f"{base}/00_brief.json"
    
    # Генерируем seed если его нет в brief
    if "seed" not in brief:
        brief["seed"] = random.randint(1, 1000000)
        logger.info(f"🎲 Сгенерирован seed для проекта: {brief['seed']}")
    
    with open(brief_path, "w", encoding="utf-8") as f:
        json.dump(brief, f, ensure_ascii=False, indent=2)
    return brief_path


