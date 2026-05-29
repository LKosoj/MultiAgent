import types
import importlib.util
from pathlib import Path


def _load_root_utils():
    """
    В репозитории есть конфликт имен: `utils.py` в корне и пакет `StoryBookManager/utils`.
    Для теста нам нужно явно загрузить корневой `/.../MultiAgent/utils.py`.
    """
    repo_root = Path(__file__).resolve().parents[1]
    utils_path = repo_root / "utils.py"
    spec = importlib.util.spec_from_file_location("root_utils", str(utils_path))
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_get_text_topic_relevance_score_returns_0_without_api_key(monkeypatch):
    utils = _load_root_utils()

    monkeypatch.delenv("CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY_DB", raising=False)

    score = utils.get_text_topic_relevance_score(text="тест", topic="тема")
    assert score == 0.0


def test_get_text_topic_relevance_score_parses_score(monkeypatch):
    utils = _load_root_utils()

    class _FakeResponse:
        def json(self):
            return {"data": [{"index": 0, "score": 0.77}]}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, *args, **kwargs):
            return _FakeResponse()

    monkeypatch.setenv("CLOUD_API_KEY", "dummy")
    monkeypatch.setenv("CLOUD_API_BASE", "https://foundation-models.api.cloud.ru")
    monkeypatch.setenv("RERANK_MODEL", "Qwen/Qwen3-Reranker-0.6B")

    monkeypatch.setattr(utils, "OpenAI", _FakeClient)
    monkeypatch.setattr(utils, "httpx", types.SimpleNamespace(Response=object))

    score = utils.get_text_topic_relevance_score(text="Пишите читабельный код.", topic="Как написать хороший код?")
    assert abs(score - 0.77) < 1e-9


