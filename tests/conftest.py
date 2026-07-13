import asyncio
import logging
import os
import sys
from pathlib import Path

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

    # Python 3.12 public get_event_loop() creates and warns when none is set;
    # inspect the policy slot so this fixture only closes an existing loop.
    policy = asyncio.get_event_loop_policy()
    policy_local = getattr(policy, "_local", None)
    current_loop = getattr(policy_local, "_loop", None)
    if current_loop is not None and not current_loop.is_closed():
        current_loop.close()

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


@pytest.fixture(autouse=True)
def _storybook_tool_projects_dir(request, monkeypatch):
    storybook_tool_tests = {
        "test_montage_assembler_tool.py",
        "test_storybook_audio_subtitle_tool.py",
        "test_storybook_music_generator_tool.py",
        "test_storybook_video_contract_tools.py",
    }
    if Path(str(request.node.path)).name in storybook_tool_tests:
        tmp_path = request.getfixturevalue("tmp_path")
        monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path / "plots" / "storybooks"))
    yield
