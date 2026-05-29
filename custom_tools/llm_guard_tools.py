from typing import List, Dict, Any, Union
import logging

# Создаем логгер для модуля LLM-Guard
logger = logging.getLogger(__name__)

try:
    # Импортируем компоненты LLM-Guard для защиты входящих запросов
    from llmguard.input_scanners import (
        PromptInjection,
        Secrets,
        TokenLimit,
        Toxicity,
        BanTopics,
        LanguageSame,
        Regex
    )
    
    # Инициализируем сканеры с оптимальными настройками
    _prompt_injection_scanner = PromptInjection(threshold=0.5)
    _secrets_scanner = Secrets()
    _toxicity_scanner = Toxicity(threshold=0.7)
    _token_limit_scanner = TokenLimit(limit=4096)
    _language_scanner = LanguageSame(expected_languages=["ru", "en"])
    
    # Настраиваем фильтр запрещенных тем
    forbidden_topics = [
        "Насилие и агрессия",
        "Наркотики и психоактивные вещества", 
        "Самоповреждение",
        "Финансовое мошенничество",
        "Создание вредоносного ПО"
    ]
    _ban_topics_scanner = BanTopics(topics=forbidden_topics, threshold=0.6)
    
    # Сканер для поиска PII с помощью регулярных выражений
    pii_patterns = [
        r'\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b',  # Номера карт
        r'\b\d{3}-\d{2}-\d{4}\b',  # SSN
        r'\b\+?[1-9]\d{1,14}\b',   # Телефоны
        r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'  # Email
    ]
    _pii_regex_scanner = Regex(patterns=pii_patterns, is_blocked=True)
    
    LLM_GUARD_AVAILABLE = True
    logger.info("✅ LLM-Guard успешно загружен")
    
except ImportError as e:
    logger.warning(f"⚠️  LLM-Guard не установлен: {e}")
    logger.info("💡 Установите: pip install llm-guard")
    LLM_GUARD_AVAILABLE = False
    

# --- Инструменты для Input-Guard-Agent ---

def prompt_injection_detector(text: str) -> Dict[str, bool]:
    """Обнаруживает попытки промпт-инъекций и джейлбрейков с использованием LLM-Guard.
    
    Args:
        text: Текст для проверки на инъекции
    
    Returns:
        Словарь с результатами проверки на инъекции
    """
    logger.debug(f"🔍 Проверка на промпт-инъекции: {text[:50]}...")
    
    if not LLM_GUARD_AVAILABLE:
        # Fallback к простой проверке
        injection_keywords = ["забудь", "игнорируй", "предыдущие инструкции", "действуй как", "forget", "ignore"]
        is_injection = any(keyword in text.lower() for keyword in injection_keywords)
        logger.warning(f"⚠️  Используется fallback-метод. Результат: {'⛔ ИНЪЕКЦИЯ' if is_injection else '✅ ЧИСТО'}")
        return {"is_injection": is_injection}
    
    try:
        # Используем LLM-Guard для профессиональной проверки
        sanitized_prompt, is_valid, risk_score = _prompt_injection_scanner.scan(text)
        is_injection = not is_valid
        
        logger.info(f"🛡️  LLM-Guard результат: {'⛔ ИНЪЕКЦИЯ' if is_injection else '✅ ЧИСТО'} (риск: {risk_score:.2f})")
        return {"is_injection": is_injection, "risk_score": risk_score}
        
    except Exception as e:
        logger.error(f"❌ Ошибка в LLM-Guard сканере: {e}")
        # Fallback к простой проверке
        injection_keywords = ["забудь", "игнорируй", "предыдущие инструкции", "действуй как"]
        is_injection = any(keyword in text.lower() for keyword in injection_keywords)
        return {"is_injection": is_injection}


def pii_scanner(text: str) -> Dict[str, object]:
    """Сканирует текст на наличие Персональных Идентифицируемых Данных (PII) с использованием LLM-Guard.
    
    Args:
        text: Текст для сканирования на PII
    
    Returns:
        Словарь с результатами сканирования PII
    """
    logger.debug(f"🔍 Сканирование PII: {text[:50]}...")
    
    if not LLM_GUARD_AVAILABLE:
        # Fallback к регулярным выражениям
        import re
        emails = re.findall(r'\S+@\S+', text)
        phones = re.findall(r'\+?\d[\d -]{8,12}\d', text)
        pii_types = []
        if emails:
            pii_types.append('EMAIL')
        if phones:
            pii_types.append('PHONE')
        has_pii = bool(pii_types)
        logger.warning(f"⚠️  Используется fallback-метод. Результат: {'⛔ PII НАЙДЕНО' if has_pii else '✅ ЧИСТО'}")
        return {"has_pii": has_pii, "pii_types": pii_types}
    
    try:
        # Комбинируем проверку секретов и PII через регулярные выражения
        pii_types = []
        
        # Проверяем секреты (API ключи, пароли и т.д.)
        _, secrets_valid, _ = _secrets_scanner.scan(text)
        if not secrets_valid:
            pii_types.append('SECRETS')
        
        # Проверяем PII через регулярные выражения
        _, pii_valid, _ = _pii_regex_scanner.scan(text)
        if not pii_valid:
            # Определяем, что именно найдено
            import re
            if re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', text):
                pii_types.append('EMAIL')
            if re.search(r'\b\+?[1-9]\d{1,14}\b', text):
                pii_types.append('PHONE')
            if re.search(r'\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b', text):
                pii_types.append('CREDIT_CARD')
        
        has_pii = bool(pii_types)
        logger.info(f"🛡️  LLM-Guard результат: {'⛔ PII НАЙДЕНО' if has_pii else '✅ ЧИСТО'} {pii_types}")
        return {"has_pii": has_pii, "pii_types": pii_types}
        
    except Exception as e:
        logger.error(f"❌ Ошибка в LLM-Guard сканере: {e}")
        # Fallback
        import re
        emails = re.findall(r'\S+@\S+', text)
        phones = re.findall(r'\+?\d[\d -]{8,12}\d', text)
        pii_types = []
        if emails:
            pii_types.append('EMAIL')
        if phones:
            pii_types.append('PHONE')
        return {"has_pii": bool(pii_types), "pii_types": pii_types}


def topic_filter(text: str) -> Dict[str, bool]:
    """Проверяет, соответствует ли тема текста разрешенной политике с использованием LLM-Guard.
    
    Args:
        text: Текст для проверки темы
    
    Returns:
        Словарь с результатами проверки темы
    """
    logger.debug(f"🔍 Проверка темы: {text[:50]}...")
    
    if not LLM_GUARD_AVAILABLE:
        # Fallback к простой проверке по ключевым словам
        forbidden_topics = ["политика", "религия", "оружие", "наркотики"]
        is_allowed = not any(topic in text.lower() for topic in forbidden_topics)
        logger.warning(f"⚠️  Используется fallback-метод. Результат: {'✅ РАЗРЕШЕНО' if is_allowed else '⛔ ЗАПРЕЩЕНО'}")
        return {"is_allowed": is_allowed}
    
    try:
        # Комбинируем несколько проверок LLM-Guard
        
        # 1. Проверка на токсичность
        _, toxicity_valid, toxicity_score = _toxicity_scanner.scan(text)
        
        # 2. Проверка запрещенных тем
        _, topics_valid, topics_score = _ban_topics_scanner.scan(text)
        
        # 3. Проверка лимита токенов (защита от спама)
        _, token_valid, _ = _token_limit_scanner.scan(text)
        
        # 4. Проверка языка (опционально)
        _, language_valid, _ = _language_scanner.scan(text)
        
        # Запрос разрешен, только если все проверки пройдены
        is_allowed = all([toxicity_valid, topics_valid, token_valid])
        
        # Собираем детали для логирования
        issues = []
        if not toxicity_valid:
            issues.append(f"токсичность({toxicity_score:.2f})")
        if not topics_valid:
            issues.append(f"запрещенная_тема({topics_score:.2f})")
        if not token_valid:
            issues.append("превышен_лимит_токенов")
        if not language_valid:
            issues.append("неподходящий_язык")
        
        result_text = '✅ РАЗРЕШЕНО' if is_allowed else f"⛔ ЗАПРЕЩЕНО: {', '.join(issues)}"
        logger.info(f"🛡️  LLM-Guard результат: {result_text}")
        
        return {
            "is_allowed": is_allowed,
            "toxicity_score": toxicity_score if not toxicity_valid else 0.0,
            "topics_score": topics_score if not topics_valid else 0.0,
            "issues": issues
        }
        
    except Exception as e:
        logger.error(f"❌ Ошибка в LLM-Guard сканере: {e}")
        # Fallback к простой проверке
        forbidden_topics = ["политика", "религия", "оружие", "наркотики"]
        is_allowed = not any(topic in text.lower() for topic in forbidden_topics)
        return {"is_allowed": is_allowed}


def comprehensive_security_check(text: str) -> Dict[str, object]:
    """Комплексная проверка безопасности, объединяющая все сканеры.
    
    Args:
        text: Текст для полной проверки безопасности
    
    Returns:
        Словарь с результатами всех проверок безопасности
    """
    logger.info(f"🛡️  Запуск комплексной проверки безопасности...")
    
    # Запускаем все проверки
    injection_result = prompt_injection_detector(text)
    pii_result = pii_scanner(text)
    topic_result = topic_filter(text)
    
    # Определяем итоговое решение
    is_safe = (
        not injection_result.get("is_injection", True) and
        not pii_result.get("has_pii", True) and
        topic_result.get("is_allowed", False)
    )
    
    # Собираем все причины блокировки
    block_reasons = []
    if injection_result.get("is_injection"):
        block_reasons.append("Обнаружена попытка промпт-инъекции")
    if pii_result.get("has_pii"):
        pii_types = pii_result.get("pii_types", [])
        block_reasons.append(f"Найдены персональные данные: {', '.join(pii_types)}")
    if not topic_result.get("is_allowed"):
        issues = topic_result.get("issues", ["неразрешенная тема"])
        block_reasons.append(f"Проблемы с содержанием: {', '.join(issues)}")
    
    result = {
        "is_safe": is_safe,
        "decision": "ALLOW" if is_safe else "BLOCK",
        "reason": "; ".join(block_reasons) if block_reasons else "Проверка пройдена",
        "details": {
            "injection_check": injection_result,
            "pii_check": pii_result,
            "topic_check": topic_result
        }
    }
    
    logger.info(f"🎯 Итоговое решение: {'✅ РАЗРЕШЕНО' if is_safe else '⛔ ЗАБЛОКИРОВАНО'}")
    if not is_safe:
        logger.warning(f"📋 Причины: {result['reason']}")
    
    return result

