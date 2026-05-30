import asyncio
import logging
import os
import sys

import pytest

# Добавляем корень репозитория в PYTHONPATH
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Подсовываем dummy-значения для OpenAI env-vars ДО collection, потому что
# отдельные тесты тянут через цепочку импорта модули, которые могут лениво
# обращаться к LLM-клиенту на этапе подготовки фикстур. Реальные значения,
# если они уже выставлены окружением, никогда не перетираются.
for _env_key, _env_default in (
    ("OPENAI_API_KEY", "test-dummy-key"),
    ("OPENAI_API_KEY_DB", "test-dummy-key"),
    ("OPENAI_API_BASE_DB", "http://localhost:0/v1"),
):
    if not os.environ.get(_env_key):
        os.environ[_env_key] = _env_default


@pytest.fixture(autouse=True)
def ensure_current_event_loop(request):
    """
    Python 3.12: создаём current event loop для legacy-тестов с get_event_loop().
    Не вмешиваемся в тесты, помеченные `@pytest.mark.asyncio` — там pytest-asyncio
    управляет циклом сам, а двойная инициализация ломает фикстуру.
    """
    if request.node.get_closest_marker("asyncio") is not None:
        yield
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture(autouse=True)
def _clear_text_to_sql_llm_safety_cache():
    """EPIC 7.4: TTL-кеш LLM safety живёт module-level. Между тестами нужен сброс,
    иначе результат от monkeypatch'енного call_openai_api предыдущего теста
    «прорастает» в следующий. Кеш импортируем лениво, чтобы конфтест не тащил
    text_to_sql при collection для тестов вне этого пакета.
    """
    try:
        from custom_tools.text_to_sql.core._sql_generation_api import _clear_llm_safety_cache
    except ImportError:
        yield
        return
    except Exception:
        logging.getLogger(__name__).warning(
            "_clear_text_to_sql_llm_safety_cache: не удалось импортировать "
            "_clear_llm_safety_cache — LLM safety кэш не будет очищен между тестами",
            exc_info=True,
        )
        yield
        return
    _clear_llm_safety_cache()
    try:
        yield
    finally:
        _clear_llm_safety_cache()
