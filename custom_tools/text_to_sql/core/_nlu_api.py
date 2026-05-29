"""NLU API подмодуль core (Phase 7 декомпозиция).

Реализации natural_language_processing и intent_extraction.
Singletons передаются через keyword-only аргументы из фасада.
"""
from typing import Dict, List, Optional


def natural_language_processing(
    text: str,
    session_id: Optional[str] = None,
    *,
    nlu_processor,
) -> Dict[str, List[str]]:
    """LLM-анализ текста: токены и простые POS-теги.

    Args:
        text: Входной текст для анализа
        session_id: ID сессии для контекста (опционально). Пробрасывается в
            ``NLUProcessor.process_text`` для логирования и аудита.

    Returns:
        Словарь с токенами и POS-тегами
    """
    return nlu_processor.process_text(text, session_id=session_id)


def intent_extraction(
    text: str,
    session_id: Optional[str] = None,
    *,
    nlu_processor,
) -> Dict[str, object]:
    """LLM-интенто- и сущностное извлечение.

    Args:
        text: Входной текст для извлечения интентов и сущностей
        session_id: ID сессии для контекста (опционально). Пробрасывается в
            ``NLUProcessor.extract_intent`` для логирования и аудита.

    Returns:
        Словарь с извлеченными интентами и сущностями
    """
    return nlu_processor.extract_intent(text, session_id=session_id)
