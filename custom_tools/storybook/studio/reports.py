"""Сводка отчётов проверок: один список замечаний (находок) по всем отчётам пайплайна.

Пайплайн пишет около десятка отчётов, у каждого свой формат и свои слова.
Модуль только читает эти файлы и отдаёт человеку единый список находок:
уровень, шот, понятный русский текст, откуда взято. Файлы проекта не меняются.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .capabilities import capability
from .errors import StudioError, error_result, success
from .labels import status_title, step_title
from .paths import project_dir, resolve_in_project

logger = logging.getLogger(__name__)

_FALLBACK_REVIEW_VERSION = 8
_REVIEW_VERSION_CACHE: tuple[int, bool] | None = None


def _review_version() -> tuple[int, bool]:
    """Актуальная версия ревью непрерывности и признак «узнали по-настоящему».

    continuity_review тянет за собой весь стек пайплайна, поэтому читаем лениво,
    один раз — при первом разборе continuity."""
    global _REVIEW_VERSION_CACHE
    if _REVIEW_VERSION_CACHE is None:
        try:
            from custom_tools.storybook.continuity_review import _REVIEW_VERSION
            _REVIEW_VERSION_CACHE = (int(_REVIEW_VERSION), True)
        except Exception:  # noqa: BLE001
            logger.warning("studio reports: не удалось узнать версию ревью, берём %s",
                           _FALLBACK_REVIEW_VERSION, exc_info=True)
            _REVIEW_VERSION_CACHE = (_FALLBACK_REVIEW_VERSION, False)
    return _REVIEW_VERSION_CACHE

LEVELS = ("error", "warning", "info")
_LEVEL_RANK = {level: index for index, level in enumerate(LEVELS)}

_MAX_FILE_BYTES = 64 * 1024 * 1024  # больше этого отчёт не читаем
_MAX_MESSAGE = 400
_CACHE_LIMIT = 256
_DRIFT_SECONDS = 1.0  # допустимое расхождение длительности клипа
_LOW_CONFIDENCE = 0.5

# --------------------------------------------------------------------------
# Безопасные приведения значений


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _i(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
        return int(value)
    return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


_TAG_BLOCK_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"</?[a-zA-Z!][^>]*>")
_SPACES_RE = re.compile(r"\s+")


def _clean_text(value: Any, limit: int = _MAX_MESSAGE) -> str:
    """Строка без HTML-тегов и лишних пробелов, не длиннее limit символов."""
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    text = value if isinstance(value, str) else str(value)
    text = _TAG_BLOCK_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _SPACES_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _fmt(value: Any) -> str:
    """Число для текста: без хвоста .0 и длинных дробей."""
    number = _num(value)
    if number is None:
        return _clean_text(value, 60)
    if number == int(number):
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


# --------------------------------------------------------------------------
# Ключи шотов

_RE_DASH = re.compile(r"^(\d+)-(\d+)$")
_RE_DIR = re.compile(r"^scene_(\d+)_shot_(\d+)$")
_RE_SCENE_SHOT = re.compile(r"^scene:(\d+):shot:(\d+)(?::|$)")
_RE_COLON = re.compile(r"^(\d+):(\d+)(?::|$)")
_RE_PHASE = re.compile(r"(?:^|[:\s(])(start|end)\)?\s*$", re.IGNORECASE)


def make_shot_key(scene: Any, shot: Any) -> str | None:
    """Пара номеров -> "сцена-шот" без нулей; нечисловое и отрицательное -> None."""
    a, b = _i(scene), _i(shot)
    if a is None or b is None or a < 0 or b < 0:
        return None
    return f"{a}-{b}"


def parse_shot_key(value: Any) -> str | None:
    """Любой известный вид ключа шота -> "S-N"; иначе None. Исключений не бросает."""
    try:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        if "/" in text or "\\" in text:
            for part in reversed(re.split(r"[\\/]", text)):
                match = _RE_DIR.match(part)
                if match:
                    return f"{int(match.group(1))}-{int(match.group(2))}"
            return None
        for pattern in (_RE_DASH, _RE_DIR, _RE_SCENE_SHOT, _RE_COLON):
            match = pattern.match(text)
            if match:
                return f"{int(match.group(1))}-{int(match.group(2))}"
    except Exception:  # noqa: BLE001 - разбор ключа не должен ронять отчёт
        return None
    return None


def parse_shot_keys(value: Any) -> list[str]:
    """"2-2|2-3" или список -> нормализованный список без повторов."""
    if isinstance(value, str):
        parts: list = value.split("|")
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return []
    result: list[str] = []
    for part in parts:
        key = parse_shot_key(part)
        if key and key not in result:
            result.append(key)
    return result


def parse_frame_key(value: Any) -> tuple[str | None, str | None]:
    """Ключ кадра -> (ключ шота или None, фаза "start"/"end"/None)."""
    shot = parse_shot_key(value)
    phase = None
    if isinstance(value, str):
        match = _RE_PHASE.search(value.strip())
        if match:
            phase = match.group(1).lower()
    return shot, phase


def shot_sort_key(key: Any) -> tuple[int, int]:
    """Числовой порядок: "9-6" раньше "10-1"; непонятные ключи в конец."""
    parsed = parse_shot_key(key) if isinstance(key, str) else None
    if not parsed:
        return (10**9, 10**9)
    a, b = parsed.split("-")
    return (int(a), int(b))


def _phase_of(shot_type: Any) -> str | None:
    text = _s(shot_type).lower()
    if text in ("start", "end"):
        return text
    return None


# --------------------------------------------------------------------------
# Находка


@dataclass(frozen=True)
class Finding:
    id: str
    level: str
    source: str
    code: str
    message: str
    shot_key: str | None
    related_shot_keys: tuple[str, ...]
    phase: str | None
    file: str
    details: dict = field(default_factory=dict)

    @property
    def scope(self) -> str:
        if self.shot_key:
            return "shot"
        return "group" if self.related_shot_keys else "project"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "level": self.level,
            "level_title": status_title("finding", self.level),
            "source": self.source,
            "source_title": REPORT_TITLES.get(self.source, self.source),
            "code": self.code,
            "message": self.message,
            "shot_key": self.shot_key,
            "related_shot_keys": list(self.related_shot_keys),
            "phase": self.phase,
            "scope": self.scope,
            "file": self.file,
            "details": dict(self.details),
        }


def _json_safe(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return _clean_text(value) if isinstance(value, str) else value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if depth >= 3:
        return None
    if isinstance(value, (list, tuple)):
        return [_json_safe(v, depth + 1) for v in value[:20]]
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in list(value.items())[:20]}
    return None


def _finding(source: str, level: str, code: str, message: str, file: str, *,
             shot_key: str | None = None, related: Any = (), phase: str | None = None,
             details: dict | None = None) -> Finding:
    if level not in _LEVEL_RANK:
        level = "info"
    text = _clean_text(message) or code
    related_t = tuple(k for k in related if k and k != shot_key)
    extra = _json_safe(details or {})
    raw = "|".join([source, code, shot_key or "", ",".join(related_t), phase or "", text, file,
                    str(extra.get("section", "")), str(extra.get("raw", "")),
                    str(extra.get("group", "")), str(extra.get("chain_id", ""))])
    fid = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return Finding(
        id=fid, level=level, source=source, code=code, message=text,
        shot_key=shot_key, related_shot_keys=related_t, phase=phase, file=file,
        details=extra,
    )


def _each(items: Any, meta: dict, handler: Callable[[Any], list]) -> list:
    """Разобрать каждый элемент отдельно: битая запись пропускается и считается."""
    out: list = []
    for item in _list(items):
        try:
            out.extend(handler(item))
        except Exception:  # noqa: BLE001
            meta["skipped"] = meta.get("skipped", 0) + 1
    return out


# --------------------------------------------------------------------------
# Подписи

REPORT_TITLES: dict[str, str] = {
    "preflight": "Предпусковая проверка видео",
    "delivery_promise": "Обещание поставки видео",
    "shots_prompt_qa": "Проверка промптов шотов",
    "blockout": "Болванка",
    "continuity_images": "Проверка кадров",
    "provider_jobs": "Задачи провайдера видео",
    "continuity_videos": "Проверка видео",
    "decision_log": "Журнал решений",
    "render_report": "Отчёт о сборке фильма",
    "final_review": "Техническая проверка фильма",
    "video_plan": "План видео",
    "quality_review": "Журнал проверок качества",
}

REPORT_STATUS_TITLES: dict[str, str] = {
    "ok": "Без замечаний",
    "warning": "Есть предупреждения",
    "error": "Есть ошибки",
    "missing": "Нет файла",
    "invalid": "Не удалось прочитать",
}

_BLOCKER_TEXT: dict[str, str] = {
    "shots_json_invalid": "Файл шотов повреждён.",
    "video_provider_unresolved": "Не выбран провайдер видео.",
    "video_provider_unavailable": "Провайдер видео не настроен.",
    "render_capability_unavailable": "Не найдены ffmpeg/ffprobe: сборка фильма невозможна.",
    "no_expected_video_clips": "В шотах нет ни одного видео к генерации.",
    "video_model_catalog_unavailable": "Не удалось получить список моделей видео у провайдера.",
}

CODE_TITLES: dict[str, str] = {
    "B01": "Длительность шота не нормализована",
    "B02": "Частота кадров болванки не целое число",
    "B03": "Неверное число кадров",
    "B04": "Пропуск в нумерации кадров",
    "B05": "Кадры разного размера",
    "B06": "Разрыв на стыке шотов цепочки",
    "B07": "Опорный кадр не совпал с первым кадром",
    "B08": "Треки не покрывают цепочку",
    "B09": "Ключи не на целых кадрах",
    "B10": "Разрыв движения камеры",
    "B11": "Неверная длина видео-референса",
    "B12": "Blender недоступен или устарел",
    "B14": "Нарушена целостность цепочек",
    "B15": "Не получен набор длительностей модели",
    "B16": "Пропорция кадра цепочки изменилась",
    "B17": "Сцена цепочки не собрана",
    "P01": "Вместо объекта библиотеки поставлена заглушка",
    "P07": "Стык прошёл по мягкому порогу",
    "P09": "Видео-референс болванки не подан",
    "P11": "Не распознан план камеры",
    "P12": "Не удалось скопировать конечный кадр связанного шота",
    "P13": "Размер болванки не согласован с видеомоделью",
    "P18": "Набор референсов не совпал с правилом",
    "SPEC_INVALID": "Описание сцены некорректно",
    "SHOTS_FILTER_ZERO": "Фильтр шотов отобрал ноль шотов",
}

_DECISION_NAMES = {
    "video_provider": "провайдер видео",
    "delivery_promise": "обещание поставки",
    "render_runtime": "средство сборки",
}

_QA_REASONS: dict[str, tuple[str, str]] = {
    "not_contradictory": ("info", "модель-судья не нашла противоречия"),
    "no_changes_after_patch": ("info", "правка ничего не меняет"),
    "judge_failed_no_rewrite": ("warning", "судья не смог переписать промпт"),
    "contains_room_markers": ("warning", "в промпте остались признаки помещения"),
    "shot_not_found": ("warning", "шот не найден"),
}

_QUALITY_STATUS_TEXT = {
    "rejected_regenerating": "отклонён, перегенерируется",
    "rejected_repairing": "отклонён, исправляется",
    "repaired": "исправлен",
    "repaired_passed": "исправлен, проверка пройдена",
    "repaired_repeating": "исправлен, повтор",
    "retrying": "повтор",
    "regenerating": "перегенерируется",
    "corrected_repeating": "поправлен, повтор",
    "partially_passed": "пройден частично",
    "invalidated_by_source_style_correction": "признан недействительным из-за правки стиля",
    "request_incompatible_repeating": "запрос не подошёл, повтор",
}

# строка из errors/warnings сборки фильма -> (код, русский текст)
_RENDER_TEXT: tuple[tuple[re.Pattern, str, str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), code, text)
    for pattern, code, text in (
        (r"^Missing video clips:\s*(\d+)", "render_missing_clips", "Не хватает видеоклипов: {0}."),
        (r"^No video clips found", "render_no_clips", "В шотах нет ни одного видеоклипа."),
        (r"^Blocked video paths outside project:\s*(\d+)", "render_blocked_paths",
         "Видеофайлы вне папки проекта отклонены: {0}."),
        (r"^Visual continuity acceptance required", "render_continuity_required",
         "Сборка требует принятой проверки видео на непрерывность."),
        (r"^Audio track missing", "render_no_audio",
         "Нет звуковой дорожки (разрешение собирать без звука выключено)."),
        (r"^music_enabled=True but no usable audio", "render_no_music",
         "Музыка включена, но пригодной дорожки нет."),
        (r"^SFX files missing", "render_no_sfx", "Не найдены файлы звуковых эффектов для монтажа."),
        (r"^ffmpeg render failed", "render_failed", "Сборка фильма не удалась."),
        (r"^ffmpeg\b.*\bbut final_video\.mp4 was not created", "render_failed", "Сборка фильма не удалась."),
        (r"^(ffmpeg|ffprobe) not available", "render_ffmpeg_missing", "Не найден ffmpeg/ffprobe."),
        (r"^Final Cut XML export failed", "render_timeline_failed", "Не удалось выгрузить таймлайн."),
        (r"^Montage assembly failed", "render_assembly_failed", "Сборка монтажа прервана ошибкой."),
        (r"^final_video_has_no_audio", "final_video_has_no_audio", "В фильме нет звука."),
    )
)


def _render_line(source: str, level: str, raw: Any, file: str) -> Finding | None:
    line = _clean_text(raw)
    if not line:
        return None
    for pattern, code, text in _RENDER_TEXT:
        match = pattern.search(line)
        if match:
            return _finding(source, level, code, text.format(*match.groups()), file, details={"raw": line})
    code = "render_error" if level == "error" else "render_warning"
    return _finding(source, level, code, line, file, details={"raw": line})


# --------------------------------------------------------------------------
# Парсеры. Каждый: (данные, ctx) -> (находки, meta). Диск не читают.


def _parse_preflight(data: dict, ctx: dict) -> tuple[list[Finding], dict]:
    file, src = ctx["file"], "preflight"
    caps = _dict(data.get("capabilities"))
    provider = _s(data.get("provider"))
    enabled = bool(data.get("generation_enabled"))
    expected = _i(data.get("expected_video_count")) or 0
    out: list[Finding] = []

    def add(level, code, message, **details):
        out.append(_finding(src, level, code, message, file, details=details))

    if _s(data.get("status")) != "success":
        add("error", "preflight_failed", "Предпусковая проверка завершилась с ошибкой.")
    if not enabled:
        add("info", "generation_disabled", "Генерация видео в этом прогоне выключена.")
    else:
        if data.get("shots_error"):
            add("error", "shots_json_invalid",
                f"Файл шотов повреждён: {_clean_text(data.get('shots_error'), 200)}.")
        if not caps.get("video"):
            if provider == "none":
                add("error", "video_provider_unresolved", "Не выбран провайдер видео.")
            else:
                env: Any = ""
                for item in _list(_dict(data.get("capability_details")).get("providers_considered")):
                    if isinstance(item, dict) and _s(item.get("name")) == provider:
                        env = item.get("required_env")
                        break
                env_text = ", ".join(_clean_text(e, 80) for e in env) if isinstance(env, list) else _clean_text(env, 160)
                add("error", "video_provider_unavailable",
                    f"Провайдер видео «{provider}» не настроен (нужны ключи: {env_text or 'не указаны'}).")
        if not caps.get("render"):
            add("error", "render_capability_unavailable",
                "Не найдены ffmpeg/ffprobe: сборка фильма невозможна.")
        if data.get("shots_exists") and expected == 0:
            add("error", "no_expected_video_clips", "В шотах нет ни одного видео к генерации.")
    if not data.get("shots_exists"):
        add("warning", "shots_json_missing_before_run", "Файла шотов ещё нет — состав видео неизвестен.")
    if not caps.get("image"):
        add("warning", "image_provider_unconfigured", "Не настроен провайдер картинок.")
    if not caps.get("audio"):
        add("info", "audio_provider_unconfigured", "Озвучка не настроена.")
    blockout = _dict(data.get("blockout"))
    forecast = _dict(blockout.get("reference_forecast"))
    not_eligible = _i(forecast.get("not_eligible")) or 0
    if blockout.get("enabled") and blockout.get("use_reference") and not_eligible > 0:
        eligible = _i(forecast.get("eligible")) or 0
        add("info", "blockout_reference_not_eligible",
            f"Видео-референс болванки получат {eligible} из {eligible + not_eligible} шотов.")
    if not any(f.level == "error" for f in out):
        add("info", "provider_selected",
            f"Провайдер видео: {provider or 'не выбран'}; ожидается клипов: {expected}.")
    meta = {"provider": provider, "expected_video_count": expected,
            "capabilities": _json_safe(caps)}
    return out, meta


def _parse_delivery_promise(data: dict, ctx: dict) -> tuple[list[Finding], dict]:
    file, src = ctx["file"], "delivery_promise"
    status = _s(data.get("status"))
    expected = _i(data.get("expected_video_count")) or 0
    out: list[Finding] = []
    meta: dict = {}
    if status == "error":
        reasons = _list(data.get("blocking_reasons"))

        def one(reason: Any) -> list[Finding]:
            raw = _clean_text(reason, 200) if isinstance(reason, str) else ""
            if not raw:
                return []
            code, _, tail = raw.partition(":")
            code = code.strip()
            if code == "video_input_invalid":
                key = parse_shot_key(tail.strip())
                text = (f"Шот {key}: " if key else "") + (
                    "входные данные непригодны для отправки провайдеру "
                    "(нет начального кадра или модель не подходит).")
                return [_finding(src, "error", code, text, file, shot_key=key, details={"raw": raw})]
            if code in _BLOCKER_TEXT:
                return [_finding(src, "error", code, _BLOCKER_TEXT[code], file, details={"raw": raw})]
            return [_finding(src, "error", "delivery_blocked",
                             f"Видео не будет сгенерировано: {code}.", file,
                             details={"raw_code": code, "raw": raw})]

        out = _each(reasons, meta, one)
        if not reasons:
            out.append(_finding(src, "error", "delivery_blocked",
                                "Видео не будет сгенерировано.", file,
                                details={"raw": _clean_text(data.get("error"), 200)}))
    elif status == "skipped":
        why = "выключена" if not data.get("generation_enabled", True) else "нечего генерировать"
        out.append(_finding(src, "info", "delivery_skipped",
                            f"Генерация видео не планируется ({why}).", file))
    elif status == "success" and data.get("will_generate_video"):
        out.append(_finding(src, "info", "delivery_promised",
                            f"Будет сгенерировано клипов: {expected}.", file))
    meta.update({"expected_video_count": expected,
                 "will_generate_video": bool(data.get("will_generate_video")),
                 "provider": _s(data.get("provider"))})
    return out, meta


def _parse_decision_log(data: dict, ctx: dict) -> tuple[list[Finding], dict]:
    file, src = ctx["file"], "decision_log"
    decisions = _list(data.get("decisions"))
    meta: dict = {"decisions": len(decisions), "approved": bool(data.get("approved")),
                  "final_video_status": _s(data.get("final_video_status"))}

    def one(item: Any) -> list[Finding]:
        if not isinstance(item, dict):
            raise ValueError("запись решения не словарь")
        d = item
        name = _s(d.get("name"))
        title = _DECISION_NAMES.get(name, name or "без названия")
        selected = _clean_text(d.get("selected"), 120)
        reason = _clean_text(d.get("reason"), 300)
        text = f"Решение «{title}»: выбрано {selected or 'ничего'}."
        out = [_finding(src, "info", "decision", text, file,
                        details={"name": name, "selected": selected, "raw": reason})]
        conf = _num(d.get("confidence"))
        if conf is not None and conf < _LOW_CONFIDENCE:
            out.append(_finding(src, "warning", "decision_low_confidence",
                                f"Решение «{title}» принято с низкой уверенностью ({_fmt(conf)}).",
                                file, details={"name": name}))
        return out

    out = _each(decisions, meta, one)
    if _s(data.get("status")) == "incomplete":
        out.append(_finding(src, "warning", "decision_incomplete", "Финальное видео ещё не собрано.", file))
    if data.get("approved") is False:
        out.append(_finding(src, "info", "decision_not_approved",
                            "Решения ещё не утверждены человеком.", file))
    return out, meta


def _continuity_parser(src: str) -> Callable[[dict, dict], tuple[list[Finding], dict]]:
    def parse(data: dict, ctx: dict) -> tuple[list[Finding], dict]:
        file = ctx["file"]
        records = [r for r in _dict(data.get("tasks")).values() if isinstance(r, dict)]
        version, version_known = _review_version()
        latest: dict[str, dict] = {}
        for r in records:
            rid = _s(r.get("id"))
            if rid and r.get("review_version") == version:
                latest.pop(rid, None)  # победитель — последняя запись в файле
                latest[rid] = r
        meta: dict = {"records": len(latest), "passed": 0, "failed": 0, "dismissed": 0,
                      "review_version": version}
        if not version_known:
            meta["review_version_known"] = False  # версию не узнали: взято запасное значение
        out: list[Finding] = []
        seen: set = set()
        for rid, rec in latest.items():
            try:
                group = parse_shot_keys(rid)
                found: list[Finding] = []
                auto = _dict(rec.get("auto_accepted"))
                if rec.get("passed") is True:
                    meta["passed"] += 1
                if auto:
                    # Принята после неудачных ремонтов: замечания остаются предупреждениями.
                    found.append(_finding(src, "warning", "continuity_auto_accepted",
                                          f"Группа шотов {rid} принята автоматически после "
                                          f"{auto.get('failed_rounds')} неудачных ремонтов.", file,
                                          related=group, details={"group": rid}))
                if rec.get("passed") is not True or auto:
                    if rec.get("passed") is not True:
                        meta["failed"] += 1
                    for verdict in _list(rec.get("verdicts")):
                        v = _dict(verdict)
                        meta["dismissed"] += len(_list(v.get("dismissed")))
                        for viol in _list(v.get("violations")):
                            vd = _dict(viol)
                            frame = _s(vd.get("frame_key"))
                            key, phase = parse_frame_key(frame)
                            observed = _clean_text(vd.get("observed"))
                            mark = (key, phase, observed[:80], None if key else rid)
                            if mark in seen:
                                continue
                            seen.add(mark)
                            found.append(_finding(
                                src, "warning" if auto else "error", "continuity_violation",
                                observed or "Нарушение непрерывности без описания.", file,
                                shot_key=key, related=() if key else group, phase=phase,
                                details={"required": _clean_text(vd.get("required")),
                                         "entity_id": _s(vd.get("entity_id")),
                                         "frame_key": frame, "group": rid}))
                        for unc in _list(v.get("uncertain")):
                            text = _clean_text(unc)
                            mark = ("u", text[:80], rid)
                            if not text or mark in seen:
                                continue
                            seen.add(mark)
                            found.append(_finding(src, "warning", "continuity_uncertain", text, file,
                                                  related=group, details={"group": rid}))
                    if not found and not auto:
                        found.append(_finding(src, "error", "continuity_failed",
                                              f"Группа шотов {rid} не принята проверкой.", file,
                                              related=group, details={"group": rid}))
                paths = [p for p in _dict(rec.get("media_hashes")) if isinstance(p, str)]
                if paths:
                    stale = _finding(src, "warning", "continuity_stale",
                                     "Проверка устарела: файлы изменились или удалены.", file,
                                     related=group, details={"group": rid})
                    ctx["guarded"].append(
                        (_dict(rec.get("media_hashes")), tuple(found), (stale,)))
                else:
                    out.extend(found)
            except Exception:  # noqa: BLE001
                meta["skipped"] = meta.get("skipped", 0) + 1
        if latest:
            out.append(_finding(src, "info", "continuity_summary",
                                f"Проверено групп: {len(latest)}, принято: {meta['passed']}.", file))
        return out, meta
    return parse


def _parse_shots_prompt_qa(data: dict, ctx: dict) -> tuple[list[Finding], dict]:
    file, src = ctx["file"], "shots_prompt_qa"
    meta: dict = {"repairs_suggested": _i(data.get("repairs_suggested")),
                  "repairs_applied": _i(data.get("repairs_applied")),
                  "repairs_rejected": _i(data.get("repairs_rejected")),
                  "items_total": _i(data.get("items_total"))}
    out: list[Finding] = []
    if data.get("dry_run"):
        out.append(_finding(src, "info", "qa_dry_run", "Это пробный прогон: изменения не сохранены.", file))

    def pair(d: dict) -> str | None:
        return make_shot_key(d.get("scene_number"), d.get("shot_number"))

    def missing(item: Any) -> list[Finding]:
        key = pair(_dict(item))
        text = f"Шот {key} не найден при проверке промптов." if key else "Шот не найден при проверке промптов."
        return [_finding(src, "error", "qa_shot_missing", text, file, shot_key=key)]

    def rendered(item: Any) -> list[Finding]:
        d = _dict(item)
        observed = _clean_text(d.get("observed"))
        if not observed:
            return []
        return [_finding(src, "warning", "qa_rendered_image", observed, file, shot_key=pair(d),
                         phase=_phase_of(d.get("shot_type")),
                         details={"required": _clean_text(d.get("required"))})]

    def rejected_for(code: str, prefix: str) -> Callable[[Any], list[Finding]]:
        def one(item: Any) -> list[Finding]:
            d = _dict(item)
            raw = _clean_text(d.get("reject_reason"), 200)
            level, why = "warning", raw or "причина не указана"
            for part in [raw, *[p.strip() for p in raw.split(":")]]:
                if part in _QA_REASONS:
                    level, why = _QA_REASONS[part]
                    break
            reasons = "; ".join(_clean_text(r, 160) for r in _list(d.get("reasons")) if _clean_text(r, 160))
            text = f"{prefix}Автоправка промпта отклонена ({why})" + (f": {reasons}" if reasons else ".")
            return [_finding(src, level, code, text, file, shot_key=pair(d),
                             phase=_phase_of(d.get("shot_type")),
                             details={"reject_reason": raw})]
        return one

    out += _each(data.get("missing_shots"), meta, missing)
    out += _each(data.get("rendered_image_findings"), meta, rendered)
    out += _each(data.get("rejected"), meta, rejected_for("qa_repair_rejected", ""))
    out += _each(data.get("global_rejected"), meta,
                 rejected_for("qa_global_repair_rejected", "Сквозная проверка сценария: "))
    if data.get("scenes_processed") is not None:
        out.append(_finding(
            src, "info", "qa_summary",
            f"Проверено сцен: {_fmt(data.get('scenes_processed'))}; правок предложено "
            f"{_fmt(data.get('repairs_suggested') or 0)}, применено {_fmt(data.get('repairs_applied') or 0)}, "
            f"отклонено {_fmt(data.get('repairs_rejected') or 0)}.", file))
    return out, meta


_JOB_OK = {"downloaded", "completed", "succeeded"}
_JOB_FAILED = {"failed", "download_failed"}
_JOB_RUNNING = {"submitting", "submitted", "queued", "running", "polling", "pending"}


def _job_error_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("message", "error", "detail"):
            if value.get(key):
                return _clean_text(value.get(key), 200)
        return ""
    return _clean_text(value, 200)


def _parse_provider_jobs(data: dict, ctx: dict) -> tuple[list[Finding], dict]:
    file, src = ctx["file"], "provider_jobs"
    jobs = _list(data.get("jobs"))
    meta: dict = {"jobs": len(jobs), "shots": 0, "by_latest_status": {}, "failed_attempts_total": 0,
                  "cost_rub": 0.0, "unkeyed": 0}
    latest: dict[str, tuple[tuple, dict]] = {}
    for index, item in enumerate(jobs):
        try:
            job = _dict(item)
            status = _s(job.get("status")).lower()
            if status in _JOB_FAILED:
                meta["failed_attempts_total"] += 1
            cost = _num(job.get("cost"))
            if cost is not None and _s(job.get("currency")).upper() == "RUB":
                meta["cost_rub"] += cost
            key = parse_shot_key(job.get("shot_key")) or parse_shot_key(job.get("output_path"))
            if not key:
                meta["unkeyed"] += 1
                continue
            stamps = _dict(job.get("timestamps"))
            order = (_s(stamps.get("updated_at")) or _s(job.get("updated_at")),
                     _s(stamps.get("created_at")) or _s(job.get("created_at")), index)
            if key not in latest or order >= latest[key][0]:
                latest[key] = (order, job)
        except Exception:  # noqa: BLE001
            meta["skipped"] = meta.get("skipped", 0) + 1
    meta["cost_rub"] = round(meta["cost_rub"], 2)
    meta["shots"] = len(latest)
    out: list[Finding] = []
    for key in sorted(latest, key=shot_sort_key):
        job = latest[key][1]
        status = _s(job.get("status")).lower()
        meta["by_latest_status"][status or "unknown"] = meta["by_latest_status"].get(status or "unknown", 0) + 1
        details = {"status": status, "provider": _s(job.get("provider")), "model": _s(job.get("model"))}
        if status in _JOB_OK:
            continue
        if status in _JOB_FAILED:
            why = _job_error_text(job.get("error")) or "причина не указана"
            out.append(_finding(src, "error", "provider_job_failed",
                                f"Шот {key}: видео не получено — {why}.", file, shot_key=key, details=details))
        elif status == "stale":
            out.append(_finding(src, "warning", "provider_job_stale",
                                f"Шот {key}: видео устарело — входные данные изменились после генерации.",
                                file, shot_key=key, details=details))
        elif status in _JOB_RUNNING:
            out.append(_finding(src, "info", "provider_job_in_progress",
                                f"Шот {key}: видео ещё генерируется.", file, shot_key=key, details=details))
        else:
            out.append(_finding(src, "info", "provider_job_status_unknown",
                                f"Шот {key}: статус задачи «{status or 'не указан'}».", file,
                                shot_key=key, details=details))
    return out, meta


def _parse_blockout(data: dict, ctx: dict) -> tuple[list[Finding], dict]:
    file, src = ctx["file"], "blockout"
    chains = _dict(ctx.get("chains"))
    meta: dict = {"sections": {}, "checks": 0}
    out: list[Finding] = []
    for section, body in data.items():
        if not isinstance(body, dict):
            continue
        meta["sections"][str(section)] = _s(body.get("status"))

        def one(check: Any, section=section) -> list[Finding]:
            c = _dict(check)
            if not c:
                return []
            meta["checks"] += 1
            level = _s(c.get("level")).lower()
            if level not in _LEVEL_RANK:
                level = "info"
            code = _s(c.get("code")) or "note"
            raw = _clean_text(c.get("message") or c.get("reason"))
            key = parse_shot_key(c.get("shot_key")) or make_shot_key(c.get("scene_number"), c.get("shot_number"))
            chain_id = _s(c.get("chain_id"))
            related = tuple(chains.get(chain_id, ())) if (not key and chain_id) else ()
            title = CODE_TITLES.get(code)
            text = title or raw or f"Замечание {code}"
            details: dict = {"section": str(section), "step_id": str(section)}
            if raw and title:
                details["raw"] = raw
            if not title:
                details["raw_code"] = code
            if chain_id:
                details["chain_id"] = chain_id
            extra = {k: v for k, v in _dict(c.get("details")).items()
                     if isinstance(v, (str, int, float, bool))}
            if extra:
                details["check_details"] = extra
            return [_finding(src, level, code, text, file, shot_key=key, related=related,
                             phase=_phase_of(c.get("shot_type")), details=details)]

        out += _each(body.get("checks"), meta, one)
    return out, meta


def _parse_render_report(data: dict, ctx: dict) -> tuple[list[Finding], dict]:
    file, src = ctx["file"], "render_report"
    result = _dict(data.get("render_result"))
    clips = _list(data.get("clips_actual_vs_planned"))
    meta: dict = {"clips": len(clips), "cached": bool(result.get("cached")),
                  "duration_seconds": _num(data.get("duration_seconds"))}
    out: list[Finding] = []
    for raw in _list(data.get("errors")):
        f = _render_line(src, "error", raw, file)
        if f:
            out.append(f)
    for raw in _list(data.get("warnings")):
        f = _render_line(src, "warning", raw, file)
        if f:
            out.append(f)
    if data.get("ffmpeg_available") is False or data.get("ffprobe_available") is False:
        out.append(_finding(src, "error", "render_ffmpeg_missing", "Не найден ffmpeg/ffprobe.", file))
    rc = _i(result.get("returncode"))
    if rc is not None and rc != 0:
        out.append(_finding(src, "error", "render_failed", "Сборка фильма не удалась.", file,
                            details={"returncode": rc}))

    def clip(item: Any) -> list[Finding]:
        d = _dict(item)
        planned, actual = _num(d.get("planned_duration_seconds")), _num(d.get("actual_duration_seconds"))
        key = make_shot_key(d.get("scene_number"), d.get("shot_number"))
        if planned is None or actual is None or abs(actual - planned) <= _DRIFT_SECONDS:
            return []
        return [_finding(src, "warning", "render_clip_drift",
                         f"Шот {key}: длительность клипа {_fmt(actual)} с вместо {_fmt(planned)} с." if key
                         else f"Длительность клипа {_fmt(actual)} с вместо {_fmt(planned)} с.",
                         file, shot_key=key)]

    out += _each(clips, meta, clip)
    return out, meta


_FINAL_CHECKS: dict[str, Callable[[dict], str]] = {
    "container": lambda c: "Файл фильма не читается или в нём нет видео.",
    "duration": lambda c: "Длительность отличается от плана"
        + (f" на {_fmt(c['drift_seconds'])} с" if _num(c.get("drift_seconds")) is not None else "")
        + (f" (допуск {_fmt(c['tolerance_seconds'])} с)" if _num(c.get("tolerance_seconds")) is not None else "") + ".",
    "black_frame": lambda c: "Чёрные кадры без объяснения"
        + (f": {len(c['unexplained_intervals'])} участков" if isinstance(c.get("unexplained_intervals"), list) else "") + ".",
    "audio": lambda c: "Проблема со звуком" + (f" ({_clean_text(c.get('status'), 60)})" if _s(c.get("status")) else "") + ".",
    "subtitles": lambda c: "Субтитры не найдены.",
    "monotony": lambda c: "Фильм однообразен"
        + (f" (риск {_clean_text(c.get('risk'), 40)})" if _s(c.get("risk")) else "") + ".",
    "freeze": lambda c: "Статичные кадры занимают"
        + (f" {_fmt(_num(c.get('freeze_ratio')) * 100)}%" if _num(c.get("freeze_ratio")) is not None else " слишком много времени")
        + (f" (порог {_fmt(_num(c.get('threshold')) * 100)}%)" if _num(c.get("threshold")) is not None else "") + ".",
    "continuity": lambda c: "Проверка на непрерывность не принята.",
}


def _parse_final_review(data: dict, ctx: dict) -> tuple[list[Finding], dict]:
    file, src = ctx["file"], "final_review"
    meta: dict = {"passed": data.get("passed") is True, "failed_checks": [],
                  "clip_count": _i(data.get("clip_count"))}
    out: list[Finding] = []
    checks = _dict(data.get("checks"))
    for name, check in checks.items():
        try:
            c = _dict(check)
            if c.get("passed") is not False:
                continue
            meta["failed_checks"].append(str(name))
            fn = _FINAL_CHECKS.get(str(name))
            text = fn(c) if fn else f"Проверка «{name}» не пройдена."
            out.append(_finding(src, "error", f"final_check_{name}", text, file))
        except Exception:  # noqa: BLE001
            meta["skipped"] = meta.get("skipped", 0) + 1
    for raw in _list(data.get("errors")):
        f = _render_line(src, "error", raw, file)
        if f:
            out.append(f)
    for raw in _list(data.get("warnings")):
        f = _render_line(src, "warning", raw, file)
        if f:
            out.append(f)
    if data.get("passed") is False and not out:
        out.append(_finding(src, "error", "final_review_failed", "Техническая проверка фильма не пройдена.", file))
    if data.get("passed") is True:
        clips = _i(data.get("clip_count"))
        dur = _num(data.get("expected_duration_seconds"))
        out.append(_finding(
            src, "info", "final_review_passed",
            "Техническая проверка пройдена"
            + (f": клипов {clips}" if clips is not None else "")
            + (f", длительность {_fmt(dur)} с" if dur is not None else "") + ".", file))
    return out, meta


def _parse_quality_review(data: dict, ctx: dict) -> tuple[list[Finding], dict]:
    file, src = ctx["file"], "quality_review"
    checks = _list(data.get("checks"))
    meta: dict = {"checks": len(checks), "passed": 0}
    if isinstance(data.get("generation_method"), str):
        meta["generation_method"] = _clean_text(data.get("generation_method"), 80)
    def one(item: Any) -> list[Finding]:
        c = _dict(item)
        step, status = _s(c.get("step")), _s(c.get("status")).lower()
        if not step or not status:
            return []
        if status in ("passed", "repaired_passed"):
            meta["passed"] += 1
            return []
        issues = c.get("issues")
        issue_text = "; ".join(_clean_text(i, 160) for i in issues if _clean_text(i, 160)) if isinstance(issues, list) else ""
        title = step_title(step)
        if "fail" in status or "error" in status:
            text = f"Шаг «{title}» не прошёл проверку качества." + (f" {issue_text}" if issue_text else "")
            return [_finding(src, "error", "quality_step_failed", text, file, details={"step": step, "status": status})]
        human = _QUALITY_STATUS_TEXT.get(status, status)
        text = f"Шаг «{title}»: {human}." + (f" {issue_text}" if issue_text else "")
        return [_finding(src, "info", "quality_step_note", text, file, details={"step": step, "status": status})]

    return _each(checks, meta, one), meta


def _parse_video_plan(data: dict, ctx: dict) -> tuple[list[Finding], dict]:
    file, src = ctx["file"], "video_plan"
    units, decisions = _list(data.get("units")), _list(data.get("decisions"))
    meta: dict = {"units": len(units), "decisions": len(decisions), "by_method": {}}

    def unit(item: Any) -> list[Finding]:
        d = _dict(item)
        keys = parse_shot_keys(d.get("shot_keys"))
        if len(keys) < 2:
            return []
        dur = _num(d.get("duration_s"))
        return [_finding(src, "info", "video_unit_group",
                         f"Шоты {', '.join(keys)} генерируются одним видео"
                         + (f" ({_fmt(dur)} с)." if dur is not None else "."), file, related=keys)]

    def decision(item: Any) -> list[Finding]:
        d = _dict(item)
        method = _s(d.get("method"))
        if method:
            meta["by_method"][method] = meta["by_method"].get(method, 0) + 1
        keys = parse_shot_keys(d.get("shot_keys"))
        if method == "references" and len(keys) == 1:
            return [_finding(src, "info", "video_method_references",
                             f"Шот {keys[0]}: видео строится по референсным картинкам, "
                             "а не по кадрам начала и конца.", file, shot_key=keys[0])]
        return []

    return _each(units, meta, unit) + _each(decisions, meta, decision), meta


# --------------------------------------------------------------------------
# Источники


@dataclass(frozen=True)
class _Source:
    id: str
    file: str
    step_ids: tuple[str, ...]
    parser: Callable[[dict, dict], tuple[list[Finding], dict]]
    deps: tuple[str, ...] = ()


# порядок пайплайна
SOURCES: tuple[_Source, ...] = (
    _Source("preflight", "96_video_contract/provider_menu_summary.json",
            ("storybook_video_preflight",), _parse_preflight),
    _Source("delivery_promise", "96_video_contract/delivery_promise.json",
            ("storybook_video_delivery_promise",), _parse_delivery_promise),
    _Source("shots_prompt_qa", "97_shots/shots_prompt_qa_report.json",
            ("shots_prompt_qa",), _parse_shots_prompt_qa),
    _Source("blockout", "93_blockout/report.json",
            ("blockout_scene_builder", "blockout_renderer", "blockout_preview"),
            _parse_blockout, deps=("93_blockout/chains.json",)),
    _Source("continuity_images", "97_shots/continuity_images_review.json",
            ("storybook_continuity_images_review",), _continuity_parser("continuity_images")),
    _Source("provider_jobs", "97_shots/provider_jobs.json", ("video_generator",), _parse_provider_jobs),
    _Source("continuity_videos", "97_shots/continuity_videos_review.json",
            ("storybook_continuity_videos_review",), _continuity_parser("continuity_videos")),
    _Source("decision_log", "96_video_contract/decision_log.json",
            ("storybook_video_decision_log",), _parse_decision_log),
    _Source("render_report", "99_final/render_report.json", ("montage_assembler",), _parse_render_report),
    _Source("final_review", "99_final/final_review.json", ("montage_assembler",), _parse_final_review),
    _Source("video_plan", "97_shots/video_plan.json", ("storybook_video_plan",), _parse_video_plan),
    _Source("quality_review", "quality_review.json", (), _parse_quality_review),
)
SOURCE_IDS = tuple(s.id for s in SOURCES)


# --------------------------------------------------------------------------
# Чтение файлов и кэш


@dataclass(frozen=True)
class _Parsed:
    status: str  # "ok" | "invalid"
    findings: tuple[Finding, ...] = ()
    meta: dict = field(default_factory=dict)
    error: str | None = None
    # (хеши файлов, находки пока файлы на месте, находки если файлы пропали)
    guarded: tuple = ()


_CACHE: "OrderedDict[str, tuple[Any, _Parsed]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        _CHAINS_CACHE.clear()


def _sig(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _read_json(path: Path) -> tuple[Any, str | None]:
    try:
        size = path.stat().st_size
        if size > _MAX_FILE_BYTES:
            return None, "файл слишком большой"
        text = path.read_bytes().decode("utf-8")
        if not text.strip():
            return None, "файл пуст"
        return json.loads(text), None
    except UnicodeDecodeError:
        return None, "файл не в кодировке UTF-8"
    except json.JSONDecodeError:
        return None, "файл повреждён (некорректный JSON)"
    except RecursionError:
        return None, "слишком глубокая вложенность"
    except OSError:
        return None, "файл недоступен"


def _locate(project_id: str, rel: str) -> Path | None:
    try:
        return resolve_in_project(project_id, rel, must_exist=True)
    except StudioError as exc:
        if exc.code in ("not_found", "forbidden_path"):
            return None
        raise


_CHAINS_CACHE: "OrderedDict[str, tuple[Any, dict]]" = OrderedDict()


def _chains_map(project_id: str, rel: str) -> tuple[dict, Any]:
    """chain_id -> ключи шотов; вторым значением сигнатура файла для кэша."""
    path = _locate(project_id, rel)
    if path is None:
        return {}, None
    sig = _sig(path)
    with _CACHE_LOCK:
        hit = _CHAINS_CACHE.get(str(path))
        if hit is not None and sig is not None and hit[0] == sig:
            return hit[1], sig
    data, error = _read_json(path)
    result: dict[str, tuple[str, ...]] = {}
    if error is None:
        for chain in _list(_dict(data).get("chains")):
            try:
                c = _dict(chain)
                cid = _s(c.get("chain_id"))
                if not cid:
                    continue
                keys = [make_shot_key(c.get("scene_number"), _dict(s).get("shot_number"))
                        for s in _list(c.get("shots"))]
                result[cid] = tuple(k for k in keys if k)
            except Exception:  # noqa: BLE001
                continue
    with _CACHE_LOCK:
        _CHAINS_CACHE[str(path)] = (sig, result)
        while len(_CHAINS_CACHE) > 32:
            _CHAINS_CACHE.popitem(last=False)
    return result, sig


def _load(project_id: str, spec: _Source, path: Path) -> _Parsed:
    sig: Any = _sig(path)
    chains: dict = {}
    if spec.deps:
        chains, dep_sig = _chains_map(project_id, spec.deps[0])
        sig = (sig, dep_sig)
    key = str(path)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == sig and sig is not None:
            _CACHE.move_to_end(key)
            return hit[1]
    parsed = _parse_file(spec, path, chains)
    with _CACHE_LOCK:
        _CACHE[key] = (sig, parsed)
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_LIMIT:
            _CACHE.popitem(last=False)
    return parsed


def _unreadable(spec: _Source, reason: str) -> _Parsed:
    f = _finding(spec.id, "warning", "report_unreadable",
                 f"Отчёт «{REPORT_TITLES[spec.id]}» не удалось прочитать: {reason}.", spec.file)
    return _Parsed("invalid", (f,), {}, reason)


def _parse_file(spec: _Source, path: Path, chains: dict) -> _Parsed:
    data, error = _read_json(path)
    if error:
        return _unreadable(spec, error)
    if not isinstance(data, dict):
        return _unreadable(spec, "неожиданная структура")
    ctx = {"file": spec.file, "chains": chains, "guarded": []}
    try:
        findings, meta = spec.parser(data, ctx)
    except Exception:  # noqa: BLE001
        logger.exception("studio reports: не удалось разобрать %s", spec.file)
        return _unreadable(spec, "не удалось разобрать")
    return _Parsed("ok", tuple(findings), _json_safe(meta), None, tuple(ctx["guarded"]))


# --------------------------------------------------------------------------
# Снимок проекта


def _empty_counts() -> dict:
    return {level: 0 for level in LEVELS}


def _utc_iso(path: Path) -> str | None:
    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_matches(path: str, expected: Any) -> bool:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected
    except OSError:
        return False


def _guard_ok(hashes: dict, verify: bool, root: Path) -> bool:
    for path, expected in hashes.items():
        try:
            real = Path(path).resolve()
            real.relative_to(root)  # файл вне проекта считаем отсутствующим
            if not real.is_file():
                return False
        except (OSError, ValueError):
            return False
        if verify and not _hash_matches(path, expected):
            return False
    return True


@dataclass
class ReportEntry:
    id: str
    title: str
    step_ids: tuple[str, ...]
    file: str
    status: str
    counts: dict
    updated_at: str | None
    meta: dict
    error: str | None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "step_ids": list(self.step_ids), "file": self.file,
            "status": self.status, "status_title": REPORT_STATUS_TITLES.get(self.status, self.status),
            "counts": dict(self.counts), "updated_at": self.updated_at,
            "meta": dict(self.meta), "error": self.error,
        }


@dataclass
class ReportsSnapshot:
    project_id: str
    entries: list[ReportEntry]
    findings: list[Finding]

    @property
    def totals(self) -> dict:
        return _count(self.findings)

    @property
    def by_shot(self) -> dict:
        result: dict[str, dict] = {}
        for f in self.findings:
            for key in _finding_keys(f):
                result.setdefault(key, _empty_counts())[f.level] += 1
        return dict(sorted(result.items(), key=lambda kv: shot_sort_key(kv[0])))


def _finding_keys(f: Finding) -> list[str]:
    keys = [f.shot_key] if f.shot_key else []
    keys += [k for k in f.related_shot_keys if k not in keys]
    return keys


def _count(findings) -> dict:
    counts = _empty_counts()
    for f in findings:
        counts[f.level] += 1
    return counts


def _sort_key(item: tuple[int, Finding]) -> tuple:
    index, f = item
    return (_LEVEL_RANK[f.level], SOURCE_IDS.index(f.source) if f.source in SOURCE_IDS else 99,
            shot_sort_key(f.shot_key or (f.related_shot_keys[0] if f.related_shot_keys else "")),
            f.code, index)


def collect(project_id: str, verify_hashes: bool = False) -> ReportsSnapshot:
    """Снимок отчётов проекта: все 12 источников, находки отсортированы."""
    root = project_dir(project_id).resolve()  # нет проекта -> not_found
    entries: list[ReportEntry] = []
    findings: list[Finding] = []
    seen_render: set = set()
    per_source: dict[str, list[Finding]] = {}
    for spec in SOURCES:
        path = _locate(project_id, spec.file)
        if path is None:
            entries.append(ReportEntry(spec.id, REPORT_TITLES[spec.id], spec.step_ids, spec.file,
                                       "missing", _empty_counts(), None, {}, None))
            continue
        parsed = _load(project_id, spec, path)
        items = list(parsed.findings)
        for hashes, normal, alt in parsed.guarded:
            items.extend(normal if _guard_ok(hashes, verify_hashes, root) else alt)
        per_source[spec.id] = items
        entries.append(ReportEntry(spec.id, REPORT_TITLES[spec.id], spec.step_ids, spec.file,
                                   parsed.status, _empty_counts(), _utc_iso(path), dict(parsed.meta), parsed.error))
    # одна и та же строка сборки из render_report и final_review показывается один раз
    # (приоритет у final_review); сначала занимаем места за final_review
    for source in ("final_review", "render_report"):
        kept = []
        for f in per_source.get(source, []):
            if f.level != "info" and f.code.startswith(("render_", "final_video_")):
                mark = (f.code, f.message)
                if mark in seen_render:
                    continue
                seen_render.add(mark)
            kept.append(f)
        if source in per_source:
            per_source[source] = kept
    for entry in entries:
        items = per_source.get(entry.id, [])
        entry.counts = _count(items)
        findings.extend(items)
        if entry.status == "ok":
            entry.status = "error" if entry.counts["error"] else ("warning" if entry.counts["warning"] else "ok")
    # одинаковые id внутри источника (два одинаковых замечания) различаем порядковым номером
    taken: dict[str, int] = {}
    unique: list[Finding] = []
    for f in findings:
        n = taken.get(f.id, 0)
        taken[f.id] = n + 1
        if n:
            f = replace(f, id=hashlib.sha1(f"{f.id}#{n}".encode("utf-8")).hexdigest()[:12])
        unique.append(f)
    ordered = [f for _, f in sorted(enumerate(unique), key=_sort_key)]
    return ReportsSnapshot(project_id, entries, ordered)


def shot_counts(project_id: str, verify_hashes: bool = False) -> dict:
    """Счётчики находок по шотам (для списка шотов, дашборда и таймлайна)."""
    return collect(project_id, verify_hashes).by_shot


# --------------------------------------------------------------------------
# Возможности


def _normalize_levels(levels: Any) -> list[str]:
    if levels is None:
        return ["error", "warning"]
    if not isinstance(levels, list) or any(not isinstance(v, str) or v not in _LEVEL_RANK for v in levels):
        raise StudioError("invalid_argument", f"Уровни: допустимы {', '.join(LEVELS)}")
    return list(levels)


def _normalize_sources(sources: Any) -> list[str] | None:
    if sources is None:
        return None
    if not isinstance(sources, list) or any(not isinstance(v, str) or v not in SOURCE_IDS for v in sources):
        raise StudioError("invalid_argument", f"Источники: допустимы {', '.join(SOURCE_IDS)}")
    return list(sources)


def _require_shot(value: Any) -> str:
    key = parse_shot_key(value)
    if not key:
        raise StudioError("invalid_argument", "Не удалось разобрать номер шота")
    return key


# внутренняя функция: замечания блокировки берёт blockout; возможностью она не зарегистрирована
def reports_findings(project_id: str, shot_key: str = "", levels: list | None = None,
                     sources: list | None = None, limit: int = 200, offset: int = 0,
                     verify_hashes: bool = False) -> dict:
    level_list = _normalize_levels(levels)
    source_list = _normalize_sources(sources)
    key = _require_shot(shot_key) if isinstance(shot_key, str) and shot_key.strip() else None
    if shot_key and not isinstance(shot_key, str):
        raise StudioError("invalid_argument", "Номер шота должен быть строкой")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise StudioError("invalid_argument", "limit: число от 1 до 1000")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise StudioError("invalid_argument", "offset: число не меньше 0")
    snap = collect(project_id, bool(verify_hashes))
    picked = [
        f for f in snap.findings
        if f.level in level_list
        and (source_list is None or f.source in source_list)
        and (key is None or key in _finding_keys(f))
    ]
    page = picked[offset: offset + limit]
    return success(
        findings=[f.to_dict() for f in page],
        total=len(picked), offset=offset, limit=limit,
        truncated=offset + len(page) < len(picked),
        counts=_count(picked),
    )


@capability("reports_for_shot", title="Отчёты по шоту", access="read", category="reports")
def reports_for_shot(project_id: str, shot_key: str, verify_hashes: bool = False) -> dict:
    key = _require_shot(shot_key)
    snap = collect(project_id, bool(verify_hashes))
    mine = [f for f in snap.findings if key in _finding_keys(f)]
    status_of = {e.id: e.status for e in snap.entries}
    groups = []
    for source in SOURCE_IDS:
        items = [f for f in mine if f.source == source]
        if items:
            groups.append({"source": source, "title": REPORT_TITLES[source],
                           "status": status_of.get(source, "ok"),
                           "findings": [f.to_dict() for f in items]})
    return success(shot_key=key, counts=_count(mine), groups=groups)
