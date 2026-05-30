import functools
import os
import yaml

from retry_openai_model import RetryOpenAIServerModel

custom_role_conversions = {"tool-call": "assistant", "tool-response": "user"}

_MODEL_CONFIGS: dict[str, dict] = {
    "model_search": {
        "model_id": "llmgateway/light_model",
        "fallback_models": "",
        "max_tokens": 32768,
        "stream": False,
        "max_retries": 8,
    },
    "model_lite": {
        "model_id": "llmgateway/light_model",
        "fallback_models": "",
        "max_tokens": 32768,
        "stream": False,
        "max_retries": 8,
    },
    "model_code": {
        "model_id": "llmgateway/qwen3.5",
        "fallback_models": "",
        "max_tokens": 32768,
        "stream": False,
        "temperature": 0.7,
        "max_retries": 8,
    },
    "model_hard": {
        "model_id": "llmgateway/high",
        "fallback_models": (
            "clr.Qwen/Qwen3-235B-A22B-Instruct-2507,"
            "clr.Qwen/Qwen3-Coder-480B-A35B-Instruct"
        ),
        "max_tokens": 32768,
        "stream": False,
        "temperature": 0.7,
        "max_retries": 8,
    },
    "model_summary": {
        "model_id": "llmgateway/big_context",
        "fallback_models": "",
        "max_tokens": 32768,
        "stream": False,
        "temperature": 0.2,
        "max_retries": 8,
    },
    "model_big": {
        "model_id": "llmgateway/big_context",
        "fallback_models": "",
        "max_tokens": 32768,
        "stream": False,
        "max_retries": 8,
    },
    "model_vision": {
        "model_id": "llmgateway/qwen3.5",
        "max_tokens": 32768,
        "stream": False,
        "temperature": 0.4,
        "max_retries": 8,
    },
    "model_reranker": {
        "model_id": "llmgateway/light_model",
        "fallback_models": "vse.amazon/nova-micro-v1",
        "max_tokens": 32768,
        "stream": False,
        "temperature": 0.2,
        "max_retries": 8,
    },
    "model_ultimate": {
        "model_id": "llmgateway/high",
        "fallback_models": "",
        "max_tokens": 65536,
        "stream": False,
        "temperature": 0.6,
        "max_retries": 8,
    },
}


@functools.lru_cache(maxsize=None)
def _get_model(name: str) -> RetryOpenAIServerModel:
    if name not in _MODEL_CONFIGS:
        raise AttributeError(f"Unknown model: {name!r}")
    config = dict(_MODEL_CONFIGS[name])
    api_key = os.getenv("OPENAI_API_KEY_DB")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY_DB is required but not set")
    config.setdefault("api_base", os.getenv("OPENAI_API_BASE_DB"))
    config.setdefault("api_key", api_key)
    config.setdefault("custom_role_conversions", custom_role_conversions)
    config.setdefault("extra_headers", {"X-Title": "MAgent"})
    return RetryOpenAIServerModel(**config)


class _ModelMapping:
    """Ленивое отображение имя→модель. ``mapping[name]``/``mapping.get(name)`` создаёт модель по требованию."""

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in _MODEL_CONFIGS

    def __getitem__(self, name: str) -> RetryOpenAIServerModel:
        if name not in _MODEL_CONFIGS:
            raise KeyError(name)
        return _get_model(name)

    def get(self, name: str, default=None):
        if name not in _MODEL_CONFIGS:
            return default
        return _get_model(name)

    def keys(self):
        return _MODEL_CONFIGS.keys()

    def values(self):
        return [_get_model(k) for k in _MODEL_CONFIGS]

    def items(self):
        return [(k, _get_model(k)) for k in _MODEL_CONFIGS]

    def __iter__(self):
        return iter(_MODEL_CONFIGS)

    def __len__(self):
        return len(_MODEL_CONFIGS)


model_mapping = _ModelMapping()


# Sidecar-файлы в agent_profiles/, которые не являются боевыми профилями агентов
# и должны игнорироваться загрузчиком (см. EPIC 6, task 6.15).
_AGENT_PROFILE_SIDECARS = {"optimization_metadata"}


@functools.lru_cache(maxsize=1)
def _load_agent_profiles() -> dict:
    profiles: dict = {}
    profile_dir = "agent_profiles"
    if not os.path.isdir(profile_dir):
        return profiles
    for filename in os.listdir(profile_dir):
        if not filename.endswith(".yaml"):
            continue
        agent_name = filename[:-5]
        if agent_name in _AGENT_PROFILE_SIDECARS:
            continue
        with open(os.path.join(profile_dir, filename), "r", encoding="utf-8") as f:
            profile_data = yaml.safe_load(f)
        if not profile_data.get("enable", True):
            continue
        if "model" in profile_data and isinstance(profile_data["model"], str):
            profile_data["model_key"] = profile_data["model"]
            profile_data["model"] = model_mapping.get(profile_data["model"])
        profiles[agent_name] = profile_data
    return profiles


def load_agent_profiles() -> dict:
    """Сохранён публичный API: возвращает свежую копию словаря профилей."""
    return dict(_load_agent_profiles())


_LAZY_ATTRS = set(_MODEL_CONFIGS) | {"AGENT_PROFILES"}


def __getattr__(name: str):
    if name in _MODEL_CONFIGS:
        return _get_model(name)
    if name == "AGENT_PROFILES":
        return dict(_load_agent_profiles())
    raise AttributeError(f"module 'agent_command' has no attribute {name!r}")


def __dir__():
    return sorted(set(globals().keys()) | _LAZY_ATTRS)
