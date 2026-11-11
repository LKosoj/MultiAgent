"""Настройки прогона проекта: файл `.studio/run_settings.json`, проверка, оценка стоимости.

Файл хранит только то, что пользователь задал явно (`null` = «по умолчанию»).
Читают его сами видеоинструменты (`video_run_config.py`), потому что прогон идёт
в отдельном процессе и переменные окружения до него не доходят.
Все возможности этого модуля ничего не отправляют по сети и ничего не оплачивают;
исключение — `provider_catalog(refresh_remote=True)`: он скачивает списки моделей.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

from custom_tools.storybook import video_pricing, video_run_config
from custom_tools.storybook.project_paths import (
    acquire_storybook_project_guard,
    acquire_storybook_project_lock,
    current_series_scope,
    require_safe_storybook_project_id,
    release_storybook_project_lock,
    _require_current_lock,
)

from . import cost_estimate, provider_catalog, style_catalog
from .capabilities import capability
from .errors import StudioError, success
from .paths import project_dir, resolve_in_project

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SETTINGS_RELPATH = video_run_config.SETTINGS_RELPATH
NARRATION_EXTENSIONS = (".mp3", ".wav", ".m4a", ".ogg")
SELECTABLE_PROVIDERS = tuple(p["id"] for p in provider_catalog.PROVIDERS if p["selectable"])
LIMITED_PROVIDERS = ("veo", "minimax")
MSG_LOCKED = "Настройки изменить нельзя, пока идёт прогон. Можно менять только бюджет."
MSG_CONFLICT = "Настройки изменены в другом окне. Обновите страницу."

_SECTIONS = {
    "video": ("provider", "model", "fallback_model", "retry_cycles", "resolution", "allowed_durations"),
    "style": ("profile", "preset"),
    "audio": ("music", "sfx", "subtitles", "narration_path"),
    "advanced": ("force_update_prompts", "skip_prompt_enhancement", "sample_before_batch",
                 "sample_shot_key", "final_allow_missing_audio"),
}
_TOP_KEYS = ("version", *_SECTIONS, "budget_rub")
_BOOL_FIELDS = {("audio", "music"), ("audio", "sfx"), ("audio", "subtitles"),
                ("advanced", "force_update_prompts"), ("advanced", "skip_prompt_enhancement"),
                ("advanced", "sample_before_batch"), ("advanced", "final_allow_missing_audio")}
_RESOLUTION_RE = re.compile(r"^\d{3,4}[pP]$")
_SHOT_KEY_RE = re.compile(r"^\d+-\d+$")
_MODEL_RE = re.compile(r"^\S{1,200}$")


# ------------------------------------------------------------------ файл

def default_settings() -> dict:
    return {"version": SCHEMA_VERSION,
            **{name: {field: None for field in fields} for name, fields in _SECTIONS.items()},
            "budget_rub": None}


def normalize(stored: Any) -> dict:
    """Полная схема: известные поля из файла поверх значений `null`; лишнее отбрасывается."""
    result = default_settings()
    if not isinstance(stored, dict):
        return result
    for name, fields in _SECTIONS.items():
        section = stored.get(name)
        if isinstance(section, dict):
            for field in fields:
                if field in section:
                    result[name][field] = copy.deepcopy(section[field])
    if "budget_rub" in stored:
        result["budget_rub"] = stored["budget_rub"]
    return result


def digest(stored: Any) -> str:
    canonical = json.dumps(normalize(stored), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_stored(project_id: str) -> tuple[dict, Optional[str]]:
    """(сырой файл, ошибка чтения). Нет файла -> `({}, None)`; битый -> `({}, текст)`."""
    path = project_dir(project_id) / SETTINGS_RELPATH
    if not path.is_file():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("run_settings.json в %s не прочитан: %s", project_id, exc)
        return {}, "Файл настроек повреждён и не прочитан: действуют значения по умолчанию."
    if not isinstance(data, dict):
        return {}, "Файл настроек имеет неверный вид: действуют значения по умолчанию."
    return data, None


def _write_atomic(path: Path, payload: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _run_active(project_id: str) -> bool:
    handle = acquire_storybook_project_lock(project_id)
    if handle is None:
        return True
    release_storybook_project_lock(handle)
    return False


# ------------------------------------------------------------------ действующие значения

def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def effective(stored: Any, project_id: str) -> dict:
    """Действующее значение каждого поля с источником: `setting` / `env` / `default`."""
    s = normalize(stored)
    out: dict = {}

    def pick(key: str, value: Any, env_value: Any = None, default: Any = None):
        if value is not None:
            out[key] = {"value": value, "source": "setting"}
        elif env_value not in (None, ""):
            out[key] = {"value": env_value, "source": "env"}
        else:
            out[key] = {"value": default, "source": "default"}

    video = s["video"]
    # не строка в файле (число, список) не должна ронять чтение: берём запасной провайдер, ошибку даст _check_types
    stored_provider = video["provider"] if isinstance(video["provider"], str) else None
    pick("video.provider", stored_provider, _env("STORYBOOK_VIDEO_PROVIDER").lower(), video_run_config.DEFAULT_PROVIDER)
    provider = out["video.provider"]["value"]
    pick("video.model", video["model"], _env(f"{provider.upper()}_VIDEO_MODEL"), "")
    pick("video.fallback_model", video["fallback_model"], _env(f"{provider.upper()}_VIDEO_FALLBACK_MODEL"), "")
    retry_env = _env(f"{provider.upper()}_VIDEO_RETRY_CYCLES")
    pick("video.retry_cycles", video["retry_cycles"], int(retry_env) if retry_env.isdigit() else None, 1)
    pick("video.resolution", video["resolution"],
         _env(f"{provider.upper()}_RESOLUTION") if provider == "routerai" else "", "")
    pick("video.allowed_durations", video["allowed_durations"], None, [])
    style = s["style"]
    pick("style.profile", style["profile"], _env("STORYBOOK_STYLE_PROFILE"), "default")
    pick("style.preset", style["preset"], None, "")
    audio = s["audio"]
    for field in ("music", "sfx", "subtitles"):
        pick(f"audio.{field}", audio[field], None, True)
    pick("audio.narration_path", audio["narration_path"], None, "")
    pick("budget_rub", s["budget_rub"], None, 0)
    for field, default in (("force_update_prompts", False), ("skip_prompt_enhancement", True),
                           ("sample_before_batch", False), ("sample_shot_key", ""),
                           ("final_allow_missing_audio", False)):
        pick(f"advanced.{field}", s["advanced"][field], None, default)
    return out


def effective_values(stored: Any, project_id: str) -> dict:
    """Плоские значения для оценки стоимости и сводки."""
    eff = effective(stored, project_id)
    return {
        "provider": eff["video.provider"]["value"],
        "model": eff["video.model"]["value"],
        "resolution": eff["video.resolution"]["value"],
        "allowed_durations": eff["video.allowed_durations"]["value"],
        "music": eff["audio.music"]["value"],
        "sfx": eff["audio.sfx"]["value"],
        "subtitles": eff["audio.subtitles"]["value"],
        "budget_rub": eff["budget_rub"]["value"] or 0,
    }


# ------------------------------------------------------------------ проверка

def _issue(code: str, level: str, field: str, message: str, scope: str = "save", **details) -> dict:
    issue = {"code": code, "level": level, "field": field, "message": message, "scope": scope}
    if details:
        issue["details"] = details
    return issue


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_types(settings: Any, strict: bool) -> list[dict]:
    issues: list[dict] = []
    if not isinstance(settings, dict):
        return [_issue("invalid_type", "error", "", "Настройки должны быть объектом.")]
    unknown_level = "error" if strict else "warning"
    for key in settings:
        if key not in _TOP_KEYS:
            issues.append(_issue("unknown_field", unknown_level, str(key), f"Неизвестное поле «{key}».", "save"))
    for name, fields in _SECTIONS.items():
        section = settings.get(name)
        if section is None:
            continue
        if not isinstance(section, dict):
            issues.append(_issue("invalid_type", "error", name, "Раздел должен быть объектом."))
            continue
        for key in section:
            if key not in fields:
                issues.append(_issue("unknown_field", unknown_level, f"{name}.{key}", f"Неизвестное поле «{name}.{key}»."))
    s = normalize(settings)
    video = s["video"]
    for field in ("model", "fallback_model"):
        value = video[field]
        if value is not None and not (isinstance(value, str) and _MODEL_RE.match(value)):
            issues.append(_issue("invalid_type", "error", f"video.{field}", "Модель: строка до 200 символов без пробелов."))
    provider_value = video["provider"]
    if provider_value is not None and not isinstance(provider_value, str):
        issues.append(_issue("invalid_type", "error", "video.provider", "Провайдер: строка с именем."))
    retry = video["retry_cycles"]
    if retry is not None:
        if not _is_int(retry):
            issues.append(_issue("invalid_type", "error", "video.retry_cycles", "Число повторов должно быть целым."))
        elif not 0 <= retry <= 5:
            issues.append(_issue("out_of_range", "error", "video.retry_cycles", "Повторов может быть от 0 до 5."))
    resolution = video["resolution"]
    if resolution is not None and not (isinstance(resolution, str) and _RESOLUTION_RE.match(resolution)):
        issues.append(_issue("invalid_type", "error", "video.resolution", "Разрешение вида 480p, 720p, 1080p."))
    durations = video["allowed_durations"]
    if durations is not None:
        if not isinstance(durations, list) or not all(_is_int(v) for v in durations):
            issues.append(_issue("invalid_type", "error", "video.allowed_durations", "Длительности: список целых чисел."))
        elif any(not 1 <= v <= 60 for v in durations):
            issues.append(_issue("out_of_range", "error", "video.allowed_durations", "Длительность шота от 1 до 60 секунд."))
        elif len(set(durations)) != len(durations):
            issues.append(_issue("out_of_range", "error", "video.allowed_durations", "В списке длительностей есть повторы."))
    for name, field in sorted(_BOOL_FIELDS):
        value = s[name][field]
        if value is not None and not isinstance(value, bool):
            issues.append(_issue("invalid_type", "error", f"{name}.{field}", "Здесь нужно «да» или «нет»."))
    for name, field in (("style", "profile"), ("style", "preset"), ("audio", "narration_path"), ("advanced", "sample_shot_key")):
        value = s[name][field]
        if value is not None and not isinstance(value, str):
            issues.append(_issue("invalid_type", "error", f"{name}.{field}", "Здесь нужна строка."))
    key = s["advanced"]["sample_shot_key"]
    if isinstance(key, str) and key and not _SHOT_KEY_RE.match(key):
        issues.append(_issue("invalid_type", "error", "advanced.sample_shot_key", "Ключ шота вида 2-3."))
    budget = s["budget_rub"]
    if budget is not None:
        if isinstance(budget, bool) or not isinstance(budget, (int, float)):
            issues.append(_issue("invalid_type", "error", "budget_rub", "Бюджет должен быть числом."))
        elif not math.isfinite(budget) or budget < 0:
            issues.append(_issue("out_of_range", "error", "budget_rub", "Бюджет не может быть отрицательным."))
        elif abs(round(budget, 2) - budget) > 1e-9:
            issues.append(_issue("out_of_range", "error", "budget_rub", "Бюджет: не больше двух знаков после запятой."))
    return issues


def _caps_tool_changed(project_id: str, provider: str, model: str) -> bool:
    data = _read_json(project_dir(project_id) / "97_shots" / "video_model_caps.json")
    if not isinstance(data, dict):
        return False
    tool = data.get("tool_name")
    expected = video_run_config.tool_name_for(provider)
    if tool and expected and tool != expected:
        return True
    return bool(model and data.get("model") and data.get("model") != model)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None


def validate(stored: Any, project_id: str, *, running: bool = False, strict: bool = True) -> list[dict]:
    """Список проблем `{code, level, field, message, scope}`.

    `scope="save"` мешает сохранению, `scope="run"` — только запуску (зависит от
    окружения сервера: нет ключа, нет cookie). `strict=False` для старых файлов:
    лишние поля тогда только предупреждение.
    """
    issues = _check_types(stored, strict)
    if any(i["level"] == "error" and i["code"] in {"invalid_type"} and i["field"] in {"", "video", "style", "audio", "advanced"}
           for i in issues):
        return issues
    s = normalize(stored)
    eff = effective(s, project_id)
    values = effective_values(s, project_id)
    provider, model = values["provider"], values["model"]
    video, style, audio, advanced = s["video"], s["style"], s["audio"], s["advanced"]
    typed_bad = {i["field"] for i in issues if i["level"] == "error"}

    if provider not in SELECTABLE_PROVIDERS:
        issues.append(_issue("unknown_provider", "error", "video.provider",
                             f"Неизвестный провайдер «{provider}». Доступны: {', '.join(SELECTABLE_PROVIDERS)}."))
        return issues
    info = provider_catalog.provider_info(provider, project_id)
    model = model or (info.get("configured_model") or "")
    entry = next((m for m in info["models"] if m["id"] == model), None)

    # стиль
    profile_name = eff["style.profile"]["value"]
    try:
        presets = style_catalog.profile_presets(profile_name)
    except Exception as exc:
        presets = None
        issues.append(_issue("unavailable", "warning", "style.profile", f"Каталог стилей не прочитан: {exc}"))
    else:
        if presets is None:
            issues.append(_issue("unknown_profile", "error", "style.profile", f"Профиль стилей «{profile_name}» не найден."))
        elif style["preset"] and "style.preset" not in typed_bad and style["preset"] not in presets:
            issues.append(_issue("unknown_preset", "error", "style.preset",
                                 f"Пресет «{style['preset']}» не найден в профиле «{profile_name}»."))

    # озвучка
    narration = audio["narration_path"]
    if narration and "audio.narration_path" not in typed_bad:
        if Path(narration).suffix.lower() not in NARRATION_EXTENSIONS:
            issues.append(_issue("narration_path_invalid", "error", "audio.narration_path",
                                 f"Озвучка: допустимы файлы {', '.join(NARRATION_EXTENSIONS)}."))
        else:
            try:
                resolve_in_project(project_id, narration)
            except StudioError as exc:
                issues.append(_issue("narration_path_invalid", "error", "audio.narration_path",
                                     f"Озвучка: {exc.message}. Файл должен лежать внутри проекта."))

    # разрешение и модель
    resolution = video["resolution"]
    if resolution and "video.resolution" not in typed_bad:
        supported = (entry or {}).get("supported_resolutions") or []
        if provider != "routerai":
            issues.append(_issue("resolution_ignored", "warning", "video.resolution",
                                 f"Провайдер {info['title']} не использует это разрешение."))
        elif supported and resolution not in supported:
            issues.append(_issue("resolution_unsupported", "error", "video.resolution",
                                 f"Модель {model} не поддерживает {resolution}. Доступно: {', '.join(supported)}."))
    if (video["model"] and info["catalog_status"] == "network" and entry is not None
            and entry["source"] == "env"):
        issues.append(_issue("model_not_in_catalog", "warning", "video.model",
                             f"Модели «{model}» нет в каталоге {info['title']}."))
    if video["fallback_model"] and video["fallback_model"] == model:
        issues.append(_issue("fallback_equals_model", "warning", "video.fallback_model",
                             "Резервная модель совпадает с основной."))

    # бюджет
    budget = values["budget_rub"]
    if budget and "budget_rub" not in typed_bad:
        if provider in LIMITED_PROVIDERS:
            issues.append(_issue("budget_unsupported_provider", "error", "budget_rub",
                                 f"Провайдер {info['title']} не ведёт журнал задач: бюджет в рублях проверить нельзя. "
                                 "Поставьте бюджет 0 или выберите RouterAI/AITunnel."))
        else:
            jobs_payload = _read_json(project_dir(project_id) / "97_shots" / "provider_jobs.json")
            jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else jobs_payload
            price = video_pricing.unit_price(provider, model, values["resolution"],
                                             [j for j in (jobs or []) if isinstance(j, dict)])
            if price["rub"] is None:
                issues.append(_issue("budget_price_unknown", "error", "budget_rub",
                                     f"Для {provider}/{model or '(модель не задана)'} не задана цена за видео: бюджет в рублях "
                                     "проверить нельзя. Укажите цену в config/storybook/video_pricing.yaml или поставьте бюджет 0."))

    # ограничения провайдера
    if info["pipeline_support"] == "limited" and provider in LIMITED_PROVIDERS:
        if info["limitations"]:
            issues.append(_issue("provider_limited", "warning", "video.provider",
                                 f"У провайдера {info['title']}: " + "; ".join(info["limitations"]) + ".",
                                 limitations=info["limitations"]))
        if advanced["sample_before_batch"]:
            issues.append(_issue("provider_option_unsupported", "error", "advanced.sample_before_batch",
                                 f"Провайдер {info['title']} не умеет пробный шот."))

    # окружение сервера (мешает запуску, но не сохранению)
    if not info["credentials_found"]:
        issues.append(_issue("video_provider_not_configured", "error", "video.provider",
                             f"Для провайдера {info['title']} не найден ключ доступа или модель. "
                             f"Не хватает: {', '.join(info['missing_env']) or 'настроек'}.",
                             scope="run", missing_env=info["missing_env"]))
    if values["music"] and not _env("SUNO_COOKIE"):
        issues.append(_issue("music_cookie_missing", "error", "audio.music",
                             "Музыка включена, но не задан доступ к Suno (SUNO_COOKIE): шаг музыки упадёт.", scope="run"))
    if values["sfx"]:
        issues.append(_issue("credentials_unknown_sfx", "warning", "audio.sfx",
                             "Для звуковых эффектов наличие ключа определить нельзя: не проверяется.", scope="run"))

    # следы прошлых прогонов
    if _caps_tool_changed(project_id, provider, model):
        issues.append(_issue("provider_changed_after_shots", "warning", "video.provider",
                             "Раскадровка подогнана под другого провайдера или модель: "
                             "длительности шотов могут не подойти. Перезапустите с шага «Раскадровка»."))
    images = _read_json(project_dir(project_id) / "30_style" / "style_images.json")
    if isinstance(images, dict):
        if images.get("source_style_quote") and style["preset"]:
            issues.append(_issue("style_preset_ignored_by_brief", "warning", "style.preset",
                                 "Стиль взят из текста задания: выбранный пресет будет проигнорирован."))
        elif style["preset"] and images.get("style_preset_id") not in (None, style["preset"]):
            issues.append(_issue("style_changed_after_style_keeper", "warning", "style.preset",
                                 "Стиль уже зафиксирован другим пресетом: запустите шаг «Стиль» заново."))
    return issues


def _blocking(issues: list[dict], *, for_save: bool) -> list[dict]:
    return [i for i in issues if i["level"] == "error" and (not for_save or i.get("scope") != "run")]


# ------------------------------------------------------------------ входы пайплайна (для Ф4)

def settings_to_inputs(project_id: str, stored: Optional[dict] = None) -> dict:
    """Входы пайплайна, которыми владеет Ф5. Плохие настройки -> `StudioError`."""
    if stored is None:
        stored, _ = read_stored(project_id)
    s = normalize(stored)
    issues = validate(s, project_id, strict=False)
    errors = _blocking(issues, for_save=False)
    if errors:
        raise StudioError("invalid_argument", "Настройки прогона содержат ошибки: " +
                          "; ".join(i["message"] for i in errors), issues=errors)
    values = effective_values(s, project_id)
    inputs: dict = {
        "video_provider": values["provider"],
        "generate_music": bool(values["music"]),
        "generate_sfx": bool(values["sfx"]),
        "generate_subtitles": bool(values["subtitles"]),
        "budget_rub": float(values["budget_rub"]) if values["budget_rub"] else 0,
        "narration_path": "",
        "style_profile": s["style"]["profile"] or "",
        "style_preset": s["style"]["preset"] or "",
    }
    narration = s["audio"]["narration_path"]
    if narration:
        inputs["narration_path"] = str(resolve_in_project(project_id, narration))
    if s["video"]["allowed_durations"] is not None:
        inputs["blockout_allowed_durations"] = list(s["video"]["allowed_durations"])
    for field in ("force_update_prompts", "skip_prompt_enhancement", "sample_before_batch",
                  "sample_shot_key", "final_allow_missing_audio"):
        if s["advanced"][field] is not None:
            inputs[field] = s["advanced"][field]
    return inputs


# ------------------------------------------------------------------ возможности

def _current(project_id: str) -> tuple[dict, Optional[str]]:
    stored, read_error = read_stored(project_id)
    return stored, read_error


@capability("run_settings_get", title="Настройки прогона", access="read")
def run_settings_get(project_id: str) -> dict:
    stored, read_error = _current(project_id)
    s = normalize(stored)
    issues = validate(stored, project_id, strict=False)
    if read_error:
        issues.append(_issue("settings_unreadable", "warning", "", read_error))
    eff = effective(s, project_id)
    return success(
        settings=s,
        effective=eff,
        defaults=default_settings(),
        digest=digest(s),
        run_active=_run_active(project_id),
        issues=issues,
    )


@capability("run_settings_save", title="Сохранить настройки прогона", access="write")
def run_settings_save(project_id: str, settings: dict, expected_digest: Optional[str] = None) -> dict:
    project_id = require_safe_storybook_project_id(project_id)
    base = project_dir(project_id)
    scope = current_series_scope()
    scoped = bool(scope and scope[0] == project_id)
    guard = None
    directory_fd = None
    try:
        directory_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        identity = os.fstat(directory_fd)
        if scoped:
            guard = acquire_storybook_project_guard(project_id)
            if guard is None:
                raise StudioError("locked", MSG_LOCKED)
        issues = validate(settings, project_id, strict=True)
        blocking = _blocking(issues, for_save=True)
        if blocking:
            raise StudioError("invalid_argument", "Настройки не сохранены: " + "; ".join(i["message"] for i in blocking),
                              issues=issues)
        new = normalize(settings)
        new["version"] = SCHEMA_VERSION
        path = base / SETTINGS_RELPATH
        path.parent.mkdir(exist_ok=True)
        with open(str(path) + ".lock", "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                _require_current_lock(lock)
                actual = base.stat()
                if (identity.st_dev, identity.st_ino) != (actual.st_dev, actual.st_ino):
                    raise StudioError("not_found", "Серия заменена. Выберите серию заново")
                exists = path.is_file()
                current, read_error = read_stored(project_id)
                if read_error and exists:
                    current = {}
                if exists and not read_error:
                    if expected_digest != digest(current):
                        raise StudioError("conflict", MSG_CONFLICT, digest=digest(current))
                elif expected_digest not in (None, digest({})):
                    raise StudioError("conflict", MSG_CONFLICT, digest=digest(current))
                if _run_active(project_id):
                    before = normalize(current)
                    after = copy.deepcopy(new)
                    before["budget_rub"] = after["budget_rub"] = None
                    if before != after:
                        raise StudioError("locked", MSG_LOCKED)
                _write_atomic(path, new)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return success(settings=new, digest=digest(new), issues=issues)
    except FileNotFoundError as exc:
        raise StudioError("not_found", "Проект или серия удалены. Выберите серию заново") from exc
    finally:
        release_storybook_project_lock(guard)
        if directory_fd is not None:
            os.close(directory_fd)


@capability("provider_catalog", title="Провайдеры и модели видео", access="read", project_scoped=False)
def provider_catalog_get(refresh_remote: bool = False, for_project_id: str = "") -> dict:
    # сеть вызывается только по явной просьбе (refresh_remote); имя for_project_id, а не project_id:
    # параметр project_id реестр всегда проверяет как id существующего проекта
    return success(**provider_catalog.build_catalog(bool(refresh_remote), for_project_id or ""))


@capability("style_catalog", title="Каталог стилей", access="read", project_scoped=False)
def style_catalog_get() -> dict:
    try:
        return success(**style_catalog.build_style_catalog())
    except Exception as exc:
        raise StudioError("unavailable", f"Каталог стилей недоступен: {exc}") from exc


def _values_for(project_id: str, settings: Optional[dict]) -> tuple[dict, dict]:
    stored = settings if settings is not None else _current(project_id)[0]
    return normalize(stored), effective_values(stored, project_id)


# внутренняя функция: оценку берут план запуска и проверка перед регенерацией; возможностью она не зарегистрирована
def cost_estimate_get(project_id: str, settings: Optional[dict] = None,
                      step_ids: Optional[list] = None, shot_keys: Optional[list] = None) -> dict:
    project_dir(project_id)
    _, values = _values_for(project_id, settings)
    return success(**cost_estimate.build_estimate(project_id, values, step_ids, shot_keys))


@capability("run_preflight_summary", title="Предпусковая сводка", access="read")
def run_preflight_summary(project_id: str, settings: Optional[dict] = None,
                          step_ids: Optional[list] = None) -> dict:
    project_dir(project_id)
    s, values = _values_for(project_id, settings)
    issues = validate(s, project_id, strict=settings is not None)
    estimate = cost_estimate.build_estimate(project_id, values, step_ids)
    providers = [provider_catalog.provider_info(values["provider"], project_id)]
    readiness: dict = {}
    try:
        from custom_tools.storybook.video_contract import storybook_video_music_readiness
        full = storybook_video_music_readiness(project_id, generate_music=bool(values["music"]),
                                               video_provider=values["provider"])
        readiness = {key: full.get(key) for key in ("ready", "blocking_reasons", "warnings", "errors", "video")}
    except Exception as exc:  # сводка не должна падать из-за чужого артефакта
        logger.warning("Готовность видео не прочитана: %s", exc)
        readiness = {"ready": False, "blocking_reasons": [], "warnings": [], "errors": [f"Готовность не прочитана: {exc}"]}
    blocking = _blocking(issues, for_save=False)
    reasons = list(readiness.get("blocking_reasons") or [])
    return success(
        ready=not blocking and not reasons,
        blocking=[i["message"] for i in blocking] + [str(r) for r in reasons],
        warnings=[i["message"] for i in issues if i["level"] == "warning"] + [w["message"] for w in estimate["warnings"]],
        estimate=estimate,
        providers=providers,
        readiness=readiness,
        issues=issues,
    )
