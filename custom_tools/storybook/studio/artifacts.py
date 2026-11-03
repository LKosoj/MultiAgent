"""Артефакты проекта storybook: список, чтение, проверка, сохранение с историей версий.

«Артефакт» - JSON-файл данных проекта (бриф, сценарий, шоты и т.д.). Модуль даёт
одну реализацию для Tk и React:
- реестр 22 файлов (ARTIFACTS) с подписями, признаком «можно править» и шагами-писателями;
- отпечаток файла (digest): если при сохранении он не тот, что был при открытии, файл
  менялся - сохранение отклоняется (conflict), чужие правки не затираются;
- история версий внутри проекта: <проект>/.studio/artifact_history/<файл>/;
- настоящая проверка по схемам (studio/schemas), а не по схеме, выведенной из самих данных.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import stat
import re
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from custom_tools.storybook.storybook_surface import JSON_ARTIFACTS

from . import schemas
from .capabilities import capability
from .errors import StudioError, success
from .labels import step_title
from .paths import project_dir, resolve_in_project, studio_dir

logger = logging.getLogger(__name__)

# В ответах поле называется artifact_key, а не key: редактор секретов в бэкенде
# закрашивает любые поля "key" (запрос с параметром key не затрагивается).

MAX_BYTES = 20 * 1024 * 1024  # больше файл в редактор не берём
HISTORY_KEEP = 30  # версий на артефакт
MAX_FINDINGS = 200
MAX_DIFF_CHANGES = 300
CLIP_LEN = 200
_SNAPSHOT_CACHE_SIZE = 8

CONFLICT_MESSAGE = "Файл изменён после того, как вы его открыли. Обновите и повторите правку."
TOO_BIG_MESSAGE = "Файл слишком большой для редактора"

_VERSION_RE = re.compile(r"^(\d{8}T\d{12})_(save|restore|external)_([0-9a-f]{8})$")
_REASON_TITLES = {"save": "Сохранение", "restore": "Откат", "external": "Внешнее изменение"}


# --- Реестр --------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactSpec:
    key: str
    title: str
    root: str  # "object" | "array"
    editable: bool
    read_only_reason: str
    careful: bool  # пайплайн читает файл как вход: неверная правка сломает следующие шаги
    writers: tuple[str, ...]  # id шагов пайплайна, которые пишут файл
    merge_safe: bool = False  # только shots: правки сливаются по полям

    @property
    def path(self) -> str:
        return JSON_ARTIFACTS[self.key]


_REASON_REDO = "Файл пересоздаётся шагом целиком, правка пропадёт."
_REASON_JOBS = "Журнал платных задач видео: правка может привести к повторной оплате."
_REASON_FINAL = "Файл пересоздаётся при сборке фильма, правка пропадёт."


def _spec(key, title, root, writers, *, careful=False, reason="", merge_safe=False) -> ArtifactSpec:
    return ArtifactSpec(key, title, root, not reason, reason, careful, tuple(writers), merge_safe)


# порядок как в экране «Файлы» (UX-спецификация 3.7); пути берутся из JSON_ARTIFACTS
ARTIFACTS: dict[str, ArtifactSpec] = {
    spec.key: spec for spec in (
        _spec("brief", "Бриф", "object", ("brief_from_prompt", "init_project"), careful=True),
        _spec("synopsis", "Синопсис", "object", ("story_planner",), careful=True),
        _spec("beats", "Сюжетные точки", "array", ("story_planner",), careful=True),
        _spec("characters", "Персонажи", "array", ("bible_builder", "screenplay_generator"), careful=True),
        _spec("locations", "Места", "array", ("bible_builder", "screenplay_generator"), careful=True),
        _spec("consistency_rules", "Правила согласованности", "array", ("bible_builder",)),
        _spec("story", "История", "object", ("story_writer", "story_editor"), careful=True),
        _spec("style_text", "Текстовый стиль", "object", ("style_keeper",)),
        _spec("style_images", "Стиль изображений", "object", ("style_keeper",), careful=True),
        _spec("screenplay", "Сценарий", "object", ("screenplay_generator",), careful=True),
        _spec("shots", "Шоты", "object",
              ("screenplay_shots_generator", "video_generator", "artist_batch_shots"),
              careful=True, merge_safe=True),
        _spec("provider_jobs", "Задачи провайдера", "object", ("video_generator",), reason=_REASON_JOBS),
        _spec("chains", "Цепочки болванки", "object", ("blockout_scene_builder",),
              reason="Файл только пишется шагом и нигде не читается как вход, правка ничего не изменит."),
        _spec("scene_spec", "Описание сцены болванки", "object", ("blockout_scene_builder",), careful=True),
        _spec("asset_map", "Карта ресурсов болванки", "object", ("blockout_scene_builder",), careful=True),
        _spec("audio_manifest", "Манифест озвучки", "object",
              ("storybook_audio_subtitle", "storybook_music_generator", "storybook_sfx_generator"), careful=True),
        _spec("music_manifest", "Манифест музыки", "object", ("storybook_music_generator",), careful=True),
        _spec("final_manifest", "Манифест фильма", "object", ("montage_assembler",), reason=_REASON_FINAL),
        _spec("asset_manifest", "Манифест ресурсов", "object", ("montage_assembler",), reason=_REASON_FINAL),
        _spec("edit_decisions", "Решения монтажа", "object", ("montage_assembler",), reason=_REASON_FINAL),
        _spec("render_report", "Отчёт сборки", "object", ("montage_assembler",), reason=_REASON_FINAL),
        _spec("final_review", "Итоговая проверка", "object", ("montage_assembler",), reason=_REASON_FINAL),
    )
}


def _get_spec(key: Any) -> ArtifactSpec:
    spec = ARTIFACTS.get(key) if isinstance(key, str) else None
    if spec is None:
        raise StudioError("invalid_argument", f"Неизвестный файл: {key}")
    return spec


def _require_editable(spec: ArtifactSpec) -> None:
    if not spec.editable:
        raise StudioError("unsupported", f"Файл «{spec.title}» нельзя править: {spec.read_only_reason}",
                          reason=spec.read_only_reason)


# --- Отпечаток ---------------------------------------------------------------------


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass")


def obj_digest(obj: Any) -> str:
    """Отпечаток разобранного JSON: не зависит от отступов и порядка ключей."""
    return "j1:" + hashlib.sha256(_canonical(obj)).hexdigest()


def file_digest(raw: bytes | None) -> str | None:
    """Отпечаток содержимого файла: j1 (канонический JSON), b1 (сырые байты, если не JSON), None (файла нет)."""
    if raw is None:
        return None
    try:
        return obj_digest(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        return "b1:" + hashlib.sha256(raw).hexdigest()


def _digest8(raw: bytes) -> str:
    return (file_digest(raw) or "").split(":", 1)[-1][:8]


def _dump(obj: Any) -> bytes:
    # формат писателей пайплайна: indent=2, кириллица читаемая, порядок ключей сохраняется
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StudioError("invalid_argument", "В тексте есть недопустимые символы (одиночный суррогат), "
                                              "сохранить такой файл нельзя") from exc


# --- Файлы ---------------------------------------------------------------------------


def _artifact_path(project_id: str, spec: ArtifactSpec) -> Path:
    project_dir(project_id)  # нет проекта -> not_found
    return resolve_in_project(project_id, spec.path, must_exist=False)


def _read_bytes(path: Path) -> bytes | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            raise StudioError("unsupported", TOO_BIG_MESSAGE)
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except IsADirectoryError as exc:
        raise StudioError("invalid_argument", "На месте файла находится папка") from exc


def read_artifact_raw(project_id: str, key: str) -> bytes | None:
    """Сырые байты артефакта (None, если файла нет)."""
    return _read_bytes(_artifact_path(project_id, _get_spec(key)))


def _parse(raw: bytes) -> tuple[Any, dict | None]:
    """(значение, ошибка разбора {line, column, message})."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, {"line": 1, "column": 1, "message": "Файл не в кодировке UTF-8"}
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, {"line": exc.lineno, "column": exc.colno, "message": f"Некорректный JSON: {exc.msg}"}
    except ValueError as exc:
        return None, {"line": 1, "column": 1, "message": f"Некорректный JSON: {exc}"}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _commit(path: Path, data: bytes, before_replace=None) -> None:
    """Записать tmp рядом, при необходимости выполнить before_replace, затем атомарно заменить файл."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))  # права исходного файла сохраняем
        except OSError:
            pass  # исходного файла нет - остаются права по умолчанию
        if before_replace is not None:
            before_replace()
        os.replace(tmp, path)
    except StudioError:
        _unlink(tmp)
        raise
    except OSError as exc:
        _unlink(tmp)
        logger.warning("artifact write failed: %s", exc)
        raise StudioError("internal", "Не удалось записать файл") from exc
    except BaseException:
        _unlink(tmp)
        raise


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# --- Замки ---------------------------------------------------------------------------

_THREAD_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(project_id: str, key: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault((str(project_dir(project_id)), key), threading.Lock())


@contextmanager
def _write_lock(project_id: str, spec: ArtifactSpec) -> Iterator[None]:
    """Замок записи артефакта.

    Обычные файлы: потоковый замок + flock на <проект>/.studio/locks/<key>.lock (flock на сам
    файл бесполезен: при замене через os.replace меняется inode). shots.json: тот же sidecar-замок
    {shots}.lock, что у писателей пайплайна и слияния Tk, - так откат не пересекается с ними."""
    if spec.merge_safe:
        from StoryBookManager.core.file_manager import _SHOTS_WRITE_LOCK

        thread_lock: Any = _SHOTS_WRITE_LOCK
        lock_path = Path(f"{_artifact_path(project_id, spec)}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags, mode = os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o666
    else:
        thread_lock = _thread_lock(project_id, spec.key)
        locks = studio_dir(project_id, create=True) / "locks"
        locks.mkdir(mode=0o700, exist_ok=True)
        lock_path = locks / f"{spec.key}.lock"
        flags, mode = os.O_RDWR | os.O_CREAT, 0o600
    with thread_lock:
        fd = os.open(lock_path, flags, mode)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


# --- Прогон --------------------------------------------------------------------------


def _probe_pipeline_lock(project_id: str) -> bool:
    """Занят ли <проект>/.pipeline.lock (держат и Tk-раннер, и бэкенд). Только чтение состояния."""
    try:  # общая проверка Ф3 (если уже есть) - без собственного захвата замка
        from . import status as _status
    except ImportError:
        _status = None
    held = getattr(_status, "pipeline_lock_held", None)
    if callable(held):
        return bool(held(project_id))
    # повтор захвата - на стороне Ф4
    path = project_dir(project_id) / ".pipeline.lock"
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return False
    finally:
        os.close(fd)


def _run_info(project_id: str) -> dict:
    """{"active": bool, "current_steps": list[str] | None}.

    Если появится studio.runs / studio.status с active_run_info, берём честный ответ оттуда,
    иначе - зонд замка (шаг неизвестен)."""
    import importlib

    for name in ("runs", "status"):
        full = f"{__package__}.{name}"
        try:
            module = importlib.import_module(full)
        except ModuleNotFoundError as exc:
            if exc.name != full:
                raise
            continue
        func = getattr(module, "active_run_info", None)
        if not callable(func):
            continue
        try:
            info = func(project_id)
        except Exception:
            logger.exception("active_run_info failed in %s", full)
            continue
        if isinstance(info, dict) and "active" in info:
            steps = info.get("current_steps")
            return {"active": bool(info["active"]), "current_steps": list(steps) if steps is not None else None}
    return {"active": _probe_pipeline_lock(project_id), "current_steps": None}


def _quote(step_ids) -> str:
    return ", ".join(f"«{step_title(step)}»" for step in step_ids)


def _run_state(spec: ArtifactSpec, run: dict) -> tuple[str, str]:
    """(free|warn|busy, сообщение) для артефакта при текущем прогоне."""
    if not run["active"]:
        return "free", ""
    if spec.merge_safe:
        return "warn", "Идёт прогон: ваши правки шотов сливаются с изменениями пайплайна по полям."
    steps = run["current_steps"]
    if steps is None:
        if spec.writers:
            return "busy", (f"Идёт прогон: файл могут перезаписать шаги {_quote(spec.writers)}. "
                            "Дождитесь окончания.")
        return "warn", "Идёт прогон: следующие шаги возьмут новые данные."
    current = [step for step in spec.writers if step in steps]
    if current:
        return "busy", f"Файл занят прогоном: его сейчас пишет шаг {_quote(current)}."
    return "warn", "Прогон идёт: следующие шаги возьмут новые данные."


def _ensure_not_busy(project_id: str, spec: ArtifactSpec) -> list[dict]:
    """locked, если файл сейчас пишет прогон; иначе список предупреждений."""
    state, message = _run_state(spec, _run_info(project_id))
    if state == "busy":
        raise StudioError("locked", message)
    return [{"code": "run_active", "message": message}] if state == "warn" else []


# --- История -------------------------------------------------------------------------


def _history_dir(project_id: str, key: str, *, create: bool = False) -> Path:
    path = studio_dir(project_id, create=create) / "artifact_history" / key
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _history_names(directory: Path) -> list[str]:
    """Имена версий (без .json), новые первыми; чужие и повреждённые имена игнорируются."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    names = []
    for entry in entries:
        stem = entry.name[: -len(".json")] if entry.name.endswith(".json") else ""
        if stem and _VERSION_RE.match(stem) and not entry.is_symlink() and entry.is_file(follow_symlinks=False):
            names.append(stem)
    return sorted(names, reverse=True)


def _snapshot(project_id: str, key: str, raw: bytes | None, reason: str) -> str | None:
    """Положить копию байтов перед перезаписью. Совпадает с новейшей версией - снимок не плодится."""
    if raw is None:
        return None
    directory = _history_dir(project_id, key, create=True)
    names = _history_names(directory)
    if names:
        try:
            if (directory / f"{names[0]}.json").read_bytes() == raw:
                return names[0]
        except OSError:
            pass
    moment, digest8 = _now(), _digest8(raw)
    while True:
        version_id = f"{moment.strftime('%Y%m%dT%H%M%S%f')}_{reason}_{digest8}"
        target = directory / f"{version_id}.json"
        if not target.exists() and not any(n.startswith(version_id[:21]) for n in names):
            break
        moment += timedelta(microseconds=1)
    try:
        with open(target, "xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        _unlink(target)
        raise StudioError("internal", "Не удалось сохранить версию в истории") from exc
    for old in _history_names(directory)[HISTORY_KEEP:]:
        _unlink(directory / f"{old}.json")
    return version_id


def _version_bytes(project_id: str, key: str, version_id: Any) -> bytes:
    if not isinstance(version_id, str) or not _VERSION_RE.match(version_id):
        raise StudioError("invalid_argument", "Недопустимый идентификатор версии")
    directory = _history_dir(project_id, key)
    path = directory / f"{version_id}.json"
    if path.is_symlink():
        raise StudioError("forbidden_path", "Недопустимый путь")
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError as exc:
        raise StudioError("forbidden_path", "Недопустимый путь") from exc
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise StudioError("not_found", "Версия не найдена") from exc


# --- Снимки shots для слияния -----------------------------------------------------------

_SNAPSHOTS: "OrderedDict[tuple, Any]" = OrderedDict()
_SNAPSHOTS_GUARD = threading.Lock()


def _remember_snapshot(project_id: str, key: str, digest: str | None, content: Any) -> None:
    if digest is None or key != "shots":
        return
    import copy

    with _SNAPSHOTS_GUARD:
        _SNAPSHOTS[(project_id, key, digest)] = copy.deepcopy(content)
        _SNAPSHOTS.move_to_end((project_id, key, digest))
        while len(_SNAPSHOTS) > _SNAPSHOT_CACHE_SIZE:
            _SNAPSHOTS.popitem(last=False)


def _recall_snapshot(project_id: str, key: str, digest: str | None) -> Any:
    import copy

    with _SNAPSHOTS_GUARD:
        value = _SNAPSHOTS.get((project_id, key, digest))
        if value is not None:
            _SNAPSHOTS.move_to_end((project_id, key, digest))
            return copy.deepcopy(value)
    return None


# --- Проверка ------------------------------------------------------------------------


def _check_context(project_id: str) -> schemas.CheckContext:
    def read(other_key: str) -> Any:
        raw = read_artifact_raw(project_id, other_key)
        return None if raw is None else _parse(raw)[0]

    return schemas.CheckContext(project_dir(project_id), read)


def _syntax_finding(key: str, error: dict) -> dict:
    return schemas.make_finding(key, "error", "syntax", [], error["message"], error["line"], error["column"])


def _validate_value(project_id: str, spec: ArtifactSpec, value: Any) -> list[dict]:
    return schemas.validate_all(spec.key, value, _check_context(project_id))


def _counts(findings: list[dict]) -> dict:
    errors = sum(1 for f in findings if f["severity"] == "error")
    return {"errors": errors, "warnings": len(findings) - errors}


def _validation_block(findings: list[dict]) -> dict:
    return {"counts": _counts(findings), "findings": findings[:MAX_FINDINGS]}


# --- Возможности: список и чтение -------------------------------------------------------


def _produced_by(spec: ArtifactSpec) -> str:
    if not spec.writers:
        return ""
    verb = "Создаёт шаг" if len(spec.writers) == 1 else "Создают шаги"
    return f"{verb} {_quote(spec.writers)}"


def _list_item(project_id: str, spec: ArtifactSpec, run: dict) -> dict:
    path = _artifact_path(project_id, spec)
    item: dict[str, Any] = {
        "artifact_key": spec.key, "title": spec.title, "path": spec.path, "exists": False, "status": "missing",
        "size": 0, "modified": None, "editable": spec.editable, "read_only_reason": spec.read_only_reason,
        "careful": spec.careful, "root": spec.root,
        "writers": [{"step_id": step, "title": step_title(step)} for step in spec.writers],
        "produced_by": _produced_by(spec), "error": None,
    }
    item["run_state"], item["run_state_message"] = _run_state(spec, run)
    item["history_count"] = len(_history_names(_history_dir(project_id, spec.key)))
    try:
        stat = path.stat()
    except FileNotFoundError:
        return item
    except OSError:
        item.update(exists=True, status="invalid",
                    error={"line": None, "column": None, "message": "Не удалось прочитать файл"})
        return item
    item.update(exists=True, size=stat.st_size, modified=_iso(stat.st_mtime))
    if stat.st_size > MAX_BYTES:
        item.update(status="invalid", error={"line": None, "column": None, "message": TOO_BIG_MESSAGE})
        return item
    try:
        _, error = _parse(path.read_bytes())
    except OSError:
        error = {"line": None, "column": None, "message": "Не удалось прочитать файл"}
    item.update(status="invalid" if error else "present", error=error)
    return item


@capability("artifacts_list", title="Список артефактов", access="read", supersedes=("artifact_inventory",))
def artifacts_list(project_id: str) -> dict:
    project_dir(project_id)
    run = _run_info(project_id)
    return success(project_id=project_id, run=run,
                   items=[_list_item(project_id, spec, run) for spec in ARTIFACTS.values()])


@capability("artifact_read", title="Прочитать артефакт", access="read")
def artifact_read(project_id: str, key: str, version_id: str | None = None) -> dict:
    spec = _get_spec(key)
    run_state, run_message = _run_state(spec, _run_info(project_id))
    is_version = version_id is not None
    if is_version:
        raw: bytes | None = _version_bytes(project_id, spec.key, version_id)
    else:
        raw = _read_bytes(_artifact_path(project_id, spec))
    result: dict[str, Any] = {
        "artifact_key": spec.key, "title": spec.title, "path": spec.path, "exists": raw is not None,
        "digest": file_digest(raw), "size": len(raw) if raw is not None else 0, "modified": None,
        "editable": spec.editable and not is_version,
        "read_only_reason": ("Это старая версия: чтобы вернуть её, используйте откат."
                             if is_version else spec.read_only_reason),
        "content": None, "text": None, "parse_error": None,
        "is_version": is_version, "version_id": version_id if is_version else None,
        "run_state": run_state, "run_state_message": run_message,
    }
    if raw is None:
        return success(**result)
    if not is_version:
        try:
            result["modified"] = _iso(_artifact_path(project_id, spec).stat().st_mtime)
        except OSError:
            pass
    result["text"] = raw.decode("utf-8", errors="replace")
    result["content"], result["parse_error"] = _parse(raw)
    if not is_version and result["parse_error"] is None:
        _remember_snapshot(project_id, spec.key, result["digest"], result["content"])
    return success(**result)


# --- Возможности: проверка --------------------------------------------------------------


def _resolve_input(spec: ArtifactSpec, content: Any, text: str | None) -> tuple[Any, dict | None]:
    """(значение, ошибка разбора) для переданных content/text."""
    if text is not None:
        if not isinstance(text, str):
            raise StudioError("invalid_argument", "Параметр text должен быть строкой")
        return _parse(text.encode("utf-8", "surrogatepass"))
    return content, None


@capability("artifact_validate", title="Проверить артефакт", access="read")
def artifact_validate(project_id: str, key: str, content: Any = None, text: str | None = None) -> dict:
    spec = _get_spec(key)
    if content is not None and text is not None:
        raise StudioError("invalid_argument", "Передайте что-то одно: content или text")
    if content is None and text is None:
        raw = read_artifact_raw(project_id, key)
        if raw is None:
            raise StudioError("not_found", "Файл ещё не создан")
        value, error = _parse(raw)
    else:
        value, error = _resolve_input(spec, content, text)
    findings = [_syntax_finding(spec.key, error)] if error else _validate_value(project_id, spec, value)
    counts = _counts(findings)
    return success(artifact_key=spec.key, valid=counts["errors"] == 0, counts=counts,
                   findings=findings[:MAX_FINDINGS], truncated=len(findings) > MAX_FINDINGS)


# --- Возможности: сохранение ------------------------------------------------------------


def _conflict(current_digest: str | None) -> StudioError:
    return StudioError("conflict", CONFLICT_MESSAGE, current_digest=current_digest)


def _write_plain(project_id: str, spec: ArtifactSpec, data: bytes, base_digest: str | None, reason: str) -> str | None:
    """Под замком: сверить отпечаток, положить снимок в историю, атомарно записать. -> version_id."""
    if len(data) > MAX_BYTES:
        raise StudioError("invalid_argument", TOO_BIG_MESSAGE)
    path = _artifact_path(project_id, spec)
    with _write_lock(project_id, spec):
        raw = _read_bytes(path)
        current = file_digest(raw)
        if current != base_digest:
            raise _conflict(current)
        _ensure_not_busy(project_id, spec)  # прогон мог стартовать между проверками
        version: dict[str, str | None] = {}
        _commit(path, data, lambda: version.update(id=_snapshot(project_id, spec.key, raw, reason)))
    return version.get("id")


def write_artifact(project_id: str, key: str, obj: Any, base_digest: str | None, reason: str = "save") -> dict:
    """Общий путь записи JSON-артефакта для других модулей studio (замок, отпечаток, история).

    Без проверки схемой и без проверки прогона - это решает вызывающий. -> {digest, previous_digest, version_id}."""
    spec = _get_spec(key)
    if reason not in _REASON_TITLES:
        raise StudioError("invalid_argument", f"Недопустимая причина версии: {reason}")
    data = _dump(obj)
    if len(data) > MAX_BYTES:
        raise StudioError("invalid_argument", TOO_BIG_MESSAGE)
    path = _artifact_path(project_id, spec)
    with _write_lock(project_id, spec):
        raw = _read_bytes(path)
        current = file_digest(raw)
        if current != base_digest:
            raise _conflict(current)
        version: dict[str, str | None] = {}
        _commit(path, data, lambda: version.update(id=_snapshot(project_id, spec.key, raw, reason)))
    return {"digest": obj_digest(obj), "previous_digest": base_digest, "version_id": version.get("id")}


def _save_shots(project_id: str, spec: ArtifactSpec, content: dict, base_digest: str | None) -> dict:
    """shots.json: слияние по полям общим протоколом (чужие правки пайплайна не теряются)."""
    from StoryBookManager.core.file_manager import merge_write_shots_json

    if not isinstance(content, dict) or not isinstance(content.get("items"), list):
        raise StudioError("invalid_argument", "В файле шотов должен быть список items")
    _dump(content)  # одиночный суррогат -> понятная ошибка до записи, а не сырое исключение кодека
    path = _artifact_path(project_id, spec)
    disk_before = file_digest(_read_bytes(path))
    baseline = _recall_snapshot(project_id, spec.key, base_digest)
    from_disk = False
    if baseline is None:
        if disk_before != base_digest:
            raise _conflict(disk_before)  # снимка нет (другой процесс, перезапуск): безопасно просим перечитать
        from_disk = True
        raw0 = _read_bytes(path)
        baseline = _parse(raw0)[0] if raw0 is not None else None
        if not isinstance(baseline, dict):
            baseline = None  # файла нет или он не разобрался: сливать не с чем, писатель его перезапишет
    baseline_items = baseline.get("items") if isinstance(baseline, dict) else None
    try:  # Ф6: отпечатки шотов без записи фиксируем перед самой записью, когда конфликтов уже нет
        from .shots import capture_baseline_before_write

        capture_baseline_before_write(project_id)
    except Exception as exc:  # битый shots.json и т.п.: одна строка без traceback
        logger.warning("shots baseline capture skipped: %s", exc)
    state: dict[str, Any] = {}

    def precondition(raw: bytes | None) -> None:
        current = file_digest(raw)
        if from_disk and current != disk_before:
            raise _conflict(current)
        state["external"] = current != base_digest
        state["version_id"] = _snapshot(project_id, spec.key, raw, "external" if state["external"] else "save")

    try:
        on_disk = merge_write_shots_json(
            path, content["items"], baseline_items if isinstance(baseline_items, list) else None,
            fresh_root=content, baseline_root=baseline if isinstance(baseline, dict) else None,
            precondition=precondition,
        )
    except UnicodeEncodeError as exc:  # суррогат пришёл из дисковой части слияния
        raise StudioError("invalid_argument", "В файле шотов на диске есть недопустимые символы "
                                              "(одиночный суррогат), сохранить его нельзя") from exc
    except OSError as exc:
        logger.warning("shots write failed: %s", exc)
        raise StudioError("internal", "Не удалось записать файл") from exc
    digest = obj_digest(on_disk)
    # при следующем сохранении базой слияния служит то, что видит клиент (его content), а не диск
    _remember_snapshot(project_id, spec.key, digest, content)
    return {"digest": digest, "version_id": state.get("version_id"), "merged": bool(state.get("external")),
            "external_changes": bool(state.get("external"))}


def _decode_input(spec: ArtifactSpec, content: Any, text: str | None) -> Any:
    if (content is None) == (text is None):
        raise StudioError("invalid_argument", "Передайте ровно одно: content или text")
    value, error = _resolve_input(spec, content, text)
    if error:
        raise StudioError("invalid_argument", f"Текст не разобрался как JSON: {error['message']}",
                          errors=[_syntax_finding(spec.key, error)])
    return value


@capability("artifact_save", title="Сохранить артефакт", access="write", supersedes=("artifact_edit",))
def artifact_save(project_id: str, key: str, base_digest: str | None, content: Any = None,
                  text: str | None = None, allow_invalid: bool = False) -> dict:
    spec = _get_spec(key)
    _require_editable(spec)
    value = _decode_input(spec, content, text)
    findings = _validate_value(project_id, spec, value)
    root = [f for f in findings if f["code"] == "root"]
    errors = [f for f in findings if f["severity"] == "error"]
    if root or (errors and not allow_invalid):
        shown = root or errors
        raise StudioError("invalid_argument", f"Файл не прошёл проверку: ошибок {len(errors)}",
                          errors=shown[:MAX_FINDINGS])
    warnings = _ensure_not_busy(project_id, spec)
    if spec.merge_safe:
        done = _save_shots(project_id, spec, value, base_digest)
    else:
        version_id = _write_plain(project_id, spec, _dump(value), base_digest, "save")
        done = {"digest": obj_digest(value), "version_id": version_id, "merged": False, "external_changes": False}
    return success(saved=True, previous_digest=base_digest, validation=_validation_block(findings),
                   warnings=warnings, **done)


# --- Возможности: история, сравнение, откат -----------------------------------------------


@capability("artifact_history", title="История артефакта", access="read")
def artifact_history(project_id: str, key: str) -> dict:
    spec = _get_spec(key)
    directory = _history_dir(project_id, spec.key)
    items = []
    for name in _history_names(directory):
        match = _VERSION_RE.match(name)
        try:
            size = (directory / f"{name}.json").stat().st_size
            created = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%f").replace(tzinfo=timezone.utc)
        except (OSError, ValueError):
            continue
        items.append({"version_id": name, "created_at": created.isoformat(), "size": size,
                      "digest8": match.group(3), "reason": match.group(2),
                      "reason_title": _REASON_TITLES[match.group(2)]})
    return success(artifact_key=spec.key, items=items)


def _shot_key(item: dict) -> tuple:
    return (item.get("scene_number"), item.get("shot_number"), item.get("shot_type"))


# где списки сравниваются по ключу элемента, а не по индексу (иначе вставка шота даёт сотню «changed»)
_LIST_KEYS = {
    ("shots", ("items",)): _shot_key,
    ("beats", ()): lambda item: item.get("page_number"),
    ("characters", ()): lambda item: item.get("name"),
    ("locations", ()): lambda item: item.get("name"),
    ("provider_jobs", ("jobs",)): lambda item: item.get("shot_key"),
}


def _clip(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and len(value) > CLIP_LEN:
        return value[:CLIP_LEN] + "…"
    return value


class _Diff:
    def __init__(self, key: str):
        self.key = key
        self.changes: list[dict] = []
        self.truncated = False

    def add(self, op: str, path: list, before: Any, after: Any) -> None:
        if len(self.changes) >= MAX_DIFF_CHANGES:
            self.truncated = True
            return
        self.changes.append({"path": list(path), "pointer": schemas.pointer_of(path),
                             "label": schemas.label_of(self.key, path), "op": op,
                             "before": _clip(before), "after": _clip(after)})

    def walk(self, before: Any, after: Any, path: list) -> None:
        if self.truncated or before == after:
            return
        if isinstance(before, dict) and isinstance(after, dict):
            for name in before:
                if name not in after:
                    self.add("removed", path + [name], before[name], None)
                else:
                    self.walk(before[name], after[name], path + [name])
            for name in after:
                if name not in before:
                    self.add("added", path + [name], None, after[name])
        elif isinstance(before, list) and isinstance(after, list):
            self._walk_lists(before, after, path)
        else:
            self.add("changed", path, before, after)

    def _walk_lists(self, before: list, after: list, path: list) -> None:
        keyfn = _LIST_KEYS.get((self.key, tuple(path)))
        if keyfn is not None:
            keyed = self._keyed(before, after, keyfn)
            if keyed is not None:
                matched, added, removed = keyed
                for b_idx, a_idx in matched:
                    self.walk(before[b_idx], after[a_idx], path + [a_idx])
                for a_idx in added:
                    self.add("added", path + [a_idx], None, after[a_idx])
                for b_idx in removed:
                    self.add("removed", path + [b_idx], before[b_idx], None)
                return
        for idx in range(max(len(before), len(after))):
            if idx >= len(before):
                self.add("added", path + [idx], None, after[idx])
            elif idx >= len(after):
                self.add("removed", path + [idx], before[idx], None)
            else:
                self.walk(before[idx], after[idx], path + [idx])

    @staticmethod
    def _keyed(before: list, after: list, keyfn):
        def index(items):
            found: dict = {}
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    return None
                key = keyfn(item)
                if key is None or (isinstance(key, tuple) and None in key) or key in found:
                    return None  # нет ключа или он повторяется: честнее сравнить по индексам
                found[key] = idx
            return found

        b_map, a_map = index(before), index(after)
        if b_map is None or a_map is None:
            return None
        matched = [(b_map[k], a_map[k]) for k in a_map if k in b_map]
        return (matched, [a_map[k] for k in a_map if k not in b_map],
                [b_map[k] for k in b_map if k not in a_map])


@capability("artifact_diff", title="Сравнить версию с текущим", access="read")
def artifact_diff(project_id: str, key: str, version_id: str) -> dict:
    spec = _get_spec(key)
    old_raw = _version_bytes(project_id, spec.key, version_id)
    current_raw = read_artifact_raw(project_id, spec.key)
    if current_raw is None:
        raise StudioError("invalid_argument", "Текущего файла нет, сравнивать не с чем")
    before, old_error = _parse(old_raw)
    after, current_error = _parse(current_raw)
    if old_error:
        raise StudioError("invalid_argument", "Выбранная версия не JSON, сравнение недоступно")
    if current_error:
        raise StudioError("invalid_argument", "Текущий файл не JSON, сравнение недоступно")
    diff = _Diff(spec.key)
    diff.walk(before, after, [])
    return success(artifact_key=spec.key, version_id=version_id, identical=before == after,
                   changes=diff.changes, truncated=diff.truncated)


def _ensure_restorable(project_id: str, spec: ArtifactSpec) -> None:
    """Откат заменяет файл целиком: при прогоне он затёр бы поля, дописанные пайплайном (shots)."""
    if spec.merge_safe and _run_info(project_id)["active"]:
        raise StudioError("locked", "Идёт прогон: откат заменил бы файл целиком и стёр поля, "
                                    "которые дописывает пайплайн. Дождитесь окончания.")


@capability("artifact_restore", title="Откатить артефакт к версии", access="write")
def artifact_restore(project_id: str, key: str, version_id: str, base_digest: str | None,
                     allow_invalid: bool = False) -> dict:
    spec = _get_spec(key)
    _require_editable(spec)
    old_raw = _version_bytes(project_id, spec.key, version_id)
    value, error = _parse(old_raw)
    findings = [_syntax_finding(spec.key, error)] if error else _validate_value(project_id, spec, value)
    root = [f for f in findings if f["code"] == "root"]
    errors = [f for f in findings if f["severity"] == "error"]
    if error or root or (errors and not allow_invalid):
        raise StudioError("invalid_argument", f"Версия не проходит проверку: ошибок {len(errors)}",
                          errors=(root or errors or findings)[:MAX_FINDINGS])
    _ensure_restorable(project_id, spec)
    warnings = _ensure_not_busy(project_id, spec)
    path = _artifact_path(project_id, spec)
    with _write_lock(project_id, spec):
        raw = _read_bytes(path)
        current = file_digest(raw)
        if current != base_digest:
            raise _conflict(current)
        _ensure_not_busy(project_id, spec)
        _ensure_restorable(project_id, spec)
        snap: dict[str, str | None] = {}
        # снимок текущего файла: откат сам обратим
        _commit(path, old_raw, lambda: snap.update(id=_snapshot(project_id, spec.key, raw, "restore")))
    return success(saved=True, digest=file_digest(old_raw), previous_digest=base_digest,
                   version_id=snap.get("id"), restored_from=version_id, merged=False, external_changes=False,
                   validation=_validation_block(findings), warnings=warnings)
