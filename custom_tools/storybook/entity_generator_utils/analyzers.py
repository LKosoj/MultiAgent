import json
import logging
from typing import Dict, Any, List

from agent_command import model_hard
from utils import call_openai_api, extract_json_from_markdown
from .prompt_templates import (
    SYSTEM_PROMPT_ANALYZE_MISSING_LOCATIONS,
    SYSTEM_PROMPT_ANALYZE_MISSING_CHARACTERS
)

logger = logging.getLogger(__name__)

def analyze_missing_locations(screenplay_data: Dict[str, Any], existing_locations: list) -> list:
    """Анализ новых локаций через LLM"""
    
    existing_names = [loc.get("name", "") for loc in existing_locations]
    
    user_prompt = f"""СГЕНЕРИРОВАННЫЙ СЦЕНАРИЙ:
{json.dumps(screenplay_data, ensure_ascii=False, indent=2)}

СУЩЕСТВУЮЩИЕ ЛОКАЦИИ В БИБЛИИ:
{existing_names}"""
    
    try:
        resp = call_openai_api(
            prompt=user_prompt,
            system_prompt=SYSTEM_PROMPT_ANALYZE_MISSING_LOCATIONS,
            model=model_hard,
            max_tokens=4000,
            temperature=0.1,
            response_format={"type": "json_object"}
        )
        clean_resp = extract_json_from_markdown(resp)
        result = json.loads(clean_resp)
        return result.get("new_locations", [])
    except Exception as e:
        logger.error(f"❌ Ошибка анализа локаций: {e}")
        return []


def analyze_missing_characters(screenplay_data: Dict[str, Any], existing_characters: list) -> list:
    """Анализ новых персонажей через LLM"""
    
    existing_names = [char.get("name", "") for char in existing_characters]
    
    user_prompt = f"""СГЕНЕРИРОВАННЫЙ СЦЕНАРИЙ:
{json.dumps(screenplay_data, ensure_ascii=False, indent=2)}

СУЩЕСТВУЮЩИЕ ПЕРСОНАЖИ В БИБЛИИ:
{existing_names}"""
    
    try:
        resp = call_openai_api(
            prompt=user_prompt,
            system_prompt=SYSTEM_PROMPT_ANALYZE_MISSING_CHARACTERS,
            model=model_hard,
            max_tokens=4000,
            temperature=0.1,
            response_format={"type": "json_object"}
        )
        clean_resp = extract_json_from_markdown(resp)
        result = json.loads(clean_resp)
        return result.get("new_characters", [])
    except Exception as e:
        logger.error(f"❌ Ошибка анализа персонажей: {e}")
        return []

