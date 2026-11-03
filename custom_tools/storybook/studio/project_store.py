"""Логика проектов storybook без реестра и без Tk: папки передаются параметрами.

Одна и та же логика нужна возможностям studio (React и Tk через мост) и самому
`ProjectManager`, поэтому корень проектов и папка бэкапов всегда явные аргументы.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from custom_tools.storybook.project_paths import require_safe_storybook_project_id

from .errors import StudioError

logger = logging.getLogger(__name__)

Progress = Callable[[int, int, str], None]

PROJECT_STRUCTURE = (
    "10_synopsis",
    "20_bible/references/characters",
    "20_bible/references/locations",
    "30_style",
    "40_prompts",
    "50_images",
    "60_layout/preview",
)

LOCK_NAME = ".pipeline.lock"
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
BACKUP_RE = re.compile(r"^(?P<id>.+)_backup_(?P<ts>\d{8}_\d{6})(?:_(?P<n>\d+))?$")
MAX_ID_LEN = 48

_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
     "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"],
))

# этапы по возрастанию; самый поздний достигнутый и есть этап проекта
STAGES = (
    ("brief", "Бриф"), ("plot", "Сюжет"), ("world", "Мир и стиль"), ("text", "Текст"),
    ("illustrations", "Иллюстрации"), ("book", "Книга"), ("screenplay", "Сценарий"),
    ("shots", "Кадры"), ("video", "Видео"), ("audio", "Звук"), ("film", "Фильм готов"),
)

BACKUP_REASONS = {
    "manual": "Вручную",
    "before_delete": "Перед удалением",
    "before_restore": "Перед восстановлением",
    "unknown": "Неизвестно",
}


# ---------------------------------------------------------------- общие мелочи

def is_service_dir(name: str) -> bool:
    """Служебная папка корня проектов: имя на `.` или `_`."""
    return name.startswith((".", "_"))


def _check_id(project_id: Any) -> str:
    try:
        return require_safe_storybook_project_id(project_id)
    except ValueError as exc:
        raise StudioError("invalid_argument", str(exc)) from exc


def _read_json(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _project_path(root: Path, project_id: str) -> Path:
    """Папка проекта строго внутри корня; симлинк наружу не пропускаем."""
    pid = _check_id(project_id)
    path = root / pid
    if path.is_symlink() or not path.is_dir():
        raise StudioError("not_found", f"Проект не найден: {pid}")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise StudioError("forbidden_path", "Проект вне папки проектов") from exc
    return path


def _inside(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------- блокировка

def try_lock(project_dir: Path):
    """Неблокирующая блокировка прогона; None - занято. Папку проекта не создаёт."""
    handle = open(project_dir / LOCK_NAME, "a+", encoding="utf-8")
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        pass
    except BlockingIOError:
        handle.close()
        return None
    return handle


def unlock(handle) -> None:
    if handle is None:
        return
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    finally:
        try:
            handle.close()
        except Exception:
            pass


def is_busy(project_dir: Path) -> bool:
    """Идёт прогон: файл блокировки есть и взять его не удалось."""
    if not (project_dir / LOCK_NAME).exists():
        return False
    try:
        handle = try_lock(project_dir)
    except OSError:
        return False
    if handle is None:
        return True
    unlock(handle)
    return False


def _lock_or_raise(project_dir: Path):
    handle = try_lock(project_dir)
    if handle is None:
        raise StudioError("locked", "С этим проектом уже работает другое приложение или прогон. Дождитесь завершения.")
    return handle


# ---------------------------------------------------------------- список и карточка

def list_project_dirs(root: Path) -> list[Path]:
    """Папки проектов: без служебных, файлов и симлинков за пределы корня."""
    if not root.is_dir():
        return []
    result = []
    for item in root.iterdir():
        if is_service_dir(item.name):
            continue
        try:
            if not item.is_dir() or (item.is_symlink() and not _inside(item, root)):
                continue
        except OSError:
            continue
        result.append(item)
    return result


def _safe_file(path: Path, project_dir: Path) -> bool:
    try:
        return path.is_file() and _inside(path, project_dir)
    except OSError:
        return False


def _page_dirs(images: Path) -> list[Path]:
    pages = []
    try:
        for item in images.iterdir():
            match = re.fullmatch(r"page_(\d+)", item.name)
            if match and item.is_dir():
                pages.append((int(match.group(1)), item))
    except OSError:
        return []
    return [item for _, item in sorted(pages)]


def _images_cover(project_dir: Path) -> str | None:
    images = project_dir / "50_images"
    pages = _page_dirs(images)
    for page in pages:
        for ext in IMAGE_EXTS:
            candidate = page / f"img_final{ext}"
            if _safe_file(candidate, project_dir):
                return candidate.relative_to(project_dir).as_posix()
    for page in pages:
        for candidate in _sorted_images(page, project_dir):
            return candidate.relative_to(project_dir).as_posix()
    for candidate in _sorted_images(images, project_dir):
        return candidate.relative_to(project_dir).as_posix()
    return None


def _sorted_images(folder: Path, project_dir: Path) -> list[Path]:
    try:
        names = sorted(item for item in folder.iterdir() if item.suffix.lower() in IMAGE_EXTS)
    except OSError:
        return []
    return [item for item in names if not item.name.startswith(".") and _safe_file(item, project_dir)]


def pick_cover(project_dir: Path) -> str | None:
    """Обложка проекта: путь относительно проекта (POSIX) или None."""
    cover = _images_cover(project_dir)
    if cover:
        return cover
    refs = project_dir / "20_bible" / "references" / "characters"
    for candidate in _sorted_images(refs, project_dir):
        if "_backup_" not in candidate.name:
            return candidate.relative_to(project_dir).as_posix()
    return None


def _has_mp4(folder: Path, depth: int = 3) -> bool:
    try:
        for item in folder.iterdir():
            if item.is_file() and item.suffix.lower() == ".mp4":
                return True
            if depth > 1 and item.is_dir() and not item.is_symlink() and _has_mp4(item, depth - 1):
                return True
    except OSError:
        pass
    return False


def detect_stage(project_dir: Path, cover_from_images: bool | None = None) -> dict:
    """Грубая оценка этапа по файлам; самый поздний достигнутый этап."""
    p = project_dir
    story = _read_json(p / "20_story" / "story.json")
    checks = {
        "brief": (p / "00_brief.json").is_file(),
        "plot": (p / "10_synopsis" / "synopsis.json").is_file(),
        "world": (p / "20_bible" / "characters.json").is_file() or (p / "30_style" / "style_text.json").is_file(),
        "text": isinstance(story, dict) and isinstance(story.get("pages"), list),
        "illustrations": _images_cover(p) is not None if cover_from_images is None else cover_from_images,
        "book": (p / "95_pdf" / "book.pdf").is_file(),
        "screenplay": (p / "91_screenplay" / "screenplay.json").is_file(),
        "shots": (p / "97_shots" / "shots.json").is_file(),
        "video": _has_mp4(p / "97_shots"),
        "audio": (p / "98_audio" / "audio_manifest.json").is_file() or (p / "98_audio" / "music_manifest.json").is_file(),
        "film": (p / "99_final" / "final_video.mp4").is_file(),
    }
    reached = None
    for index, (stage_id, title) in enumerate(STAGES, 1):
        if checks[stage_id]:
            reached = {"id": stage_id, "title": title, "index": index, "source": "artifacts"}
    return reached or {"id": "empty", "title": "Пусто", "index": 0, "source": "artifacts"}


def modified_time(project_dir: Path) -> float:
    """Папка проекта почти не меняется при записи вглубь, поэтому берём и прямые вложения."""
    latest = 0.0
    try:
        latest = project_dir.stat().st_mtime
        for item in os.scandir(project_dir):
            try:
                latest = max(latest, item.stat(follow_symlinks=False).st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return latest


def build_card(project_dir: Path, *, creating: bool = False) -> dict:
    project_id = project_dir.name
    brief = _read_json(project_dir / "00_brief.json")
    brief = brief if isinstance(brief, dict) else {}
    story = _read_json(project_dir / "20_story" / "story.json")
    pages = story.get("pages") if isinstance(story, dict) else None
    characters = brief.get("main_characters")
    images_cover = _images_cover(project_dir)
    cover = images_cover or pick_cover(project_dir)
    has_brief = (project_dir / "00_brief.json").is_file()
    state = "creating" if creating else ("ready" if has_brief else "empty")
    try:
        created = _iso(project_dir.stat().st_ctime)
    except OSError:
        created = ""
    return {
        "project_id": project_id,
        "title": str(brief.get("title") or project_id),
        "description": str(brief.get("description") or "")[:200],
        "language": str(brief.get("language") or ""),
        "state": state,
        "stage": detect_stage(project_dir, images_cover is not None),
        "busy": is_busy(project_dir),
        "pages_count": len(pages) if isinstance(pages, list) else 0,
        "characters_count": len(characters) if isinstance(characters, list) else 0,
        "has_pdf": (project_dir / "95_pdf" / "book.pdf").is_file(),
        "has_final_video": (project_dir / "99_final" / "final_video.mp4").is_file(),
        "cover": cover,
        "created": created,
        "modified": _iso(modified_time(project_dir)),
    }


def project_details(project_dir: Path) -> dict:
    brief = _read_json(project_dir / "00_brief.json")
    brief = brief if isinstance(brief, dict) else {}
    shots = _read_json(project_dir / "97_shots" / "shots.json")
    items = shots.get("items") if isinstance(shots, dict) else None
    characters = brief.get("main_characters")
    return {
        "genre": str(brief.get("genre") or ""),
        "target_age": str(brief.get("target_age") or ""),
        "description_full": str(brief.get("description") or "")[:2000],
        "main_characters": [str(x) for x in characters] if isinstance(characters, list) else [],
        "pages_min": brief.get("pages_min"),
        "pages_max": brief.get("pages_max"),
        "shots_total": len(items) if isinstance(items, list) else None,
    }


# ---------------------------------------------------------------- размер

_SIZE_TTL = 120.0
_size_cache: dict[str, tuple[float, int, int]] = {}
_size_lock = threading.Lock()


def invalidate_size_cache() -> None:
    with _size_lock:
        _size_cache.clear()


def _scan_size(path: Path) -> tuple[int, int]:
    total = count = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, count


def dir_size(path: Path, *, use_cache: bool = True) -> dict:
    """Размер и число файлов без разыменования симлинков; кэш на 120 с."""
    key = str(path)
    now = time.monotonic()
    if use_cache:
        with _size_lock:
            hit = _size_cache.get(key)
        if hit and now - hit[0] < _SIZE_TTL:
            return {"size_bytes": hit[1], "files_count": hit[2], "cached": True}
    size, count = _scan_size(path)
    with _size_lock:
        _size_cache[key] = (now, size, count)
    return {"size_bytes": size, "files_count": count, "cached": False}


# ---------------------------------------------------------------- идентификаторы и создание

def transliterate(text: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in text.lower())


def suggest_project_id(hint: str, root: Path, *, reserve: bool = False) -> str:
    """Свободный id по подсказке; reserve=True занимает папку (mkdir без exist_ok)."""
    normalized = re.sub(r"[^a-z0-9]+", "_", transliterate((hint or "").strip())).strip("_")
    normalized = normalized[:MAX_ID_LEN].strip("_") or "storybook"
    if reserve:
        root.mkdir(parents=True, exist_ok=True)
    candidate = normalized
    for attempt in range(6):
        if attempt:
            candidate = f"{normalized}_{uuid.uuid4().hex[:8]}"
        target = root / candidate
        if reserve:
            try:
                target.mkdir(exist_ok=False)
                return candidate
            except FileExistsError:
                continue
        if not (target.exists() or target.is_symlink()):
            return candidate
    raise StudioError("conflict", "Не удалось подобрать свободный идентификатор проекта")


def _field_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StudioError("invalid_argument", f"Поле «{field}» должно быть целым числом")
    return value


def validate_new_project(*, title, description, genre, target_age, language="ru", pages_min=9,
                         pages_max=12, words_per_page_min=400, words_per_page_max=450) -> dict:
    """Проверки формы нового проекта; возвращает очищенные значения."""
    strings = {"Название": title, "Описание": description, "Жанр": genre, "Возраст": target_age}
    for field, value in strings.items():
        if not isinstance(value, str):
            raise StudioError("invalid_argument", f"Поле «{field}» должно быть строкой")
    title, description = title.strip(), description.strip()
    genre, target_age = genre.strip(), target_age.strip()
    if len(title) < 2:
        raise StudioError("invalid_argument", "Поле «Название» должно содержать минимум 2 символа")
    if len(description) < 10:
        raise StudioError("invalid_argument", "Поле «Описание» должно содержать минимум 10 символов")
    if not genre:
        raise StudioError("invalid_argument", "Поле «Жанр» не должно быть пустым")
    if not target_age:
        raise StudioError("invalid_argument", "Поле «Возраст» не должно быть пустым")
    if language not in ("ru", "en"):
        raise StudioError("invalid_argument", "Поле «Язык» должно быть «ru» или «en»")
    pmin, pmax = _field_int(pages_min, "Страниц от"), _field_int(pages_max, "Страниц до")
    wmin, wmax = _field_int(words_per_page_min, "Слов на страницу от"), _field_int(words_per_page_max, "Слов на страницу до")
    if pmin < 1:
        raise StudioError("invalid_argument", "Поле «Страниц от» должно быть не меньше 1")
    if pmax < pmin:
        raise StudioError("invalid_argument", "Поле «Страниц до» не может быть меньше «Страниц от»")
    if wmin < 50:
        raise StudioError("invalid_argument", "Поле «Слов на страницу от» должно быть не меньше 50")
    if wmax < wmin:
        raise StudioError("invalid_argument", "Поле «Слов на страницу до» не может быть меньше «Слов на страницу от»")
    return {
        "title": title, "description": description, "genre": genre, "target_age": target_age,
        "language": language, "pages_min": pmin, "pages_max": pmax,
        "words_per_page_min": wmin, "words_per_page_max": wmax,
    }


def create_project(root: Path, *, title, description, genre, target_age, language="ru", pages_min=9,
                   pages_max=12, words_per_page_min=400, words_per_page_max=450, id_hint="") -> str:
    """Папки проекта и 00_brief.json; возвращает project_id."""
    values = validate_new_project(
        title=title, description=description, genre=genre, target_age=target_age, language=language,
        pages_min=pages_min, pages_max=pages_max,
        words_per_page_min=words_per_page_min, words_per_page_max=words_per_page_max,
    )
    project_id = suggest_project_id(id_hint or values["title"], root, reserve=True)
    project_path = root / project_id
    try:
        for relative in PROJECT_STRUCTURE:
            (project_path / relative).mkdir(parents=True, exist_ok=True)
        brief = {
            "title": values["title"],
            "genre": values["genre"],
            "target_age": values["target_age"],
            "language": values["language"],
            "description": values["description"],
            "main_characters": [],
            "main_locations": [],
            "pages_min": values["pages_min"],
            "pages_max": values["pages_max"],
            "words_per_page_min": values["words_per_page_min"],
            "words_per_page_max": values["words_per_page_max"],
            "moral": "",
            "storybook_prompt": (
                f"Название: {values['title']}\nЖанр: {values['genre']}\n"
                f"Возраст: {values['target_age']}\nОписание: {values['description']}"
            ),
            "seed": random.randint(1, 1000000),
        }
        write_json_atomic(project_path / "00_brief.json", brief)
    except BaseException:
        shutil.rmtree(project_path, ignore_errors=True)
        raise
    invalidate_size_cache()
    logger.info("Создан новый проект %s", project_id)
    return project_id


def rename_project(root: Path, project_id: str, title: str) -> str:
    """Меняет только title в брифе (папка и id остаются). Возвращает прежнее название."""
    if not isinstance(title, str) or not 2 <= len(title.strip()) <= 120:
        raise StudioError("invalid_argument", "Поле «Название» должно содержать от 2 до 120 символов")
    path = _project_path(root, project_id)
    brief_path = path / "00_brief.json"
    brief = _read_json(brief_path)
    if not isinstance(brief, dict):
        raise StudioError("not_found", "У проекта нет брифа")
    handle = _lock_or_raise(path)
    try:
        previous = str(brief.get("title") or "")
        brief["title"] = title.strip()
        write_json_atomic(brief_path, brief)
    finally:
        unlock(handle)
    return previous


# ---------------------------------------------------------------- копирование дерева

def _excluded(parts: tuple[str, ...]) -> bool:
    """Не копируем блокировку, миниатюры, прежние выгрузки и состояние прогона."""
    if parts[-1] == LOCK_NAME:
        return True
    return parts[0] == ".studio" and len(parts) >= 2 and parts[1] in {"thumbs", "exports", "control.json"}


def _scan_tree(src: Path) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]], list[tuple[str, ...]], int]:
    """(папки, файлы, симлинки, байт файлов) относительно src, с учётом исключений."""
    dirs: list[tuple[str, ...]] = []
    files: list[tuple[str, ...]] = []
    links: list[tuple[str, ...]] = []
    size = 0
    stack: list[tuple[Path, tuple[str, ...]]] = [(src, ())]
    while stack:
        folder, prefix = stack.pop()
        with os.scandir(folder) as it:
            for entry in it:
                parts = prefix + (entry.name,)
                if _excluded(parts):
                    continue
                if entry.is_symlink():
                    links.append(parts)
                elif entry.is_dir(follow_symlinks=False):
                    dirs.append(parts)
                    stack.append((Path(entry.path), parts))
                elif entry.is_file(follow_symlinks=False):
                    files.append(parts)
                    size += entry.stat(follow_symlinks=False).st_size
    return dirs, files, links, size


def _copy_tree(src: Path, dst: Path, progress: Progress | None, label: str,
               scanned=None, locked: bool = False) -> tuple[int, int]:
    """Копия дерева в новую папку dst; симлинки копируются как симлинки. Возвращает (файлов, байт)."""
    dirs, files, links, size = scanned or _scan_tree(src)
    dst.mkdir(parents=True, exist_ok=False)
    for parts in sorted(dirs):
        (dst.joinpath(*parts)).mkdir(parents=True, exist_ok=True)
    total = len(files)
    copied_bytes = 0
    copied_files = 0
    for index, parts in enumerate(files, 1):
        source = src.joinpath(*parts)
        target = dst.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            expected = source.stat().st_size  # размер до копирования: прогон может дописывать файл
            shutil.copy2(source, target)
        except FileNotFoundError:
            target.unlink(missing_ok=True)  # файл удалили во время копирования - пропускаем
            if progress:
                progress(index, total, label)
            continue
        actual = target.stat().st_size
        if not locked:
            # без замка прогон мог перезаписать файл: сверяем с размером источника после копирования
            try:
                expected = min(expected, source.stat().st_size)
            except FileNotFoundError:
                pass
        if actual < expected:
            raise StudioError("internal", "Копия получилась неполной")
        copied_bytes += actual
        copied_files += 1
        if progress:
            progress(index, total, label)
    for parts in links:
        target = dst.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(os.readlink(src.joinpath(*parts)), target)
        except FileNotFoundError:
            continue
    return copied_files, copied_bytes


def _tmp_name(prefix: str, name: str) -> str:
    return f".{prefix}_{name}_{uuid.uuid4().hex[:8]}"


def _check_space(target_dir: Path, need: int) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target_dir).free
    if free < need:
        raise StudioError("unavailable", "Недостаточно места на диске для копии", need_bytes=need, free_bytes=free)


# ---------------------------------------------------------------- бэкапы

def _meta_dir(backup_dir: Path) -> Path:
    return backup_dir / ".meta"


def _meta_path(backup_dir: Path, backup_id: str) -> Path:
    return _meta_dir(backup_dir) / f"{backup_id}.json"


def _backup_sort_key(match: re.Match) -> tuple[str, int]:
    return match.group("ts"), int(match.group("n") or 1)


def _iter_backups(backup_dir: Path, project_id: str | None = None):
    """(имя, разбор имени) для папок-бэкапов; чужие имена, файлы и симлинки пропускаются."""
    if not backup_dir.is_dir():
        return
    for item in backup_dir.iterdir():
        match = BACKUP_RE.match(item.name)
        if not match or item.is_symlink() or not item.is_dir():
            continue
        if project_id is not None and match.group("id") != project_id:
            continue
        yield item, match


def create_backup(root: Path, backup_dir: Path, project_id: str, *, reason: str = "manual", note: str = "",
                  max_backups: int = 10, protect: tuple[str, ...] = (), progress: Progress | None = None,
                  locked: bool = False, now: Callable[[], datetime] = datetime.now) -> dict:
    """Полная копия проекта в `<backup_dir>/<id>_backup_<дата>[_N]` + метаданные."""
    path = _project_path(root, project_id)
    if reason not in BACKUP_REASONS:
        raise StudioError("invalid_argument", "Неизвестная причина бэкапа")
    scanned = _scan_tree(path)
    _check_space(backup_dir, int(scanned[3] * 1.1) + 1)
    stamp = now().strftime("%Y%m%d_%H%M%S")
    name = f"{project_id}_backup_{stamp}"
    counter = 1
    while (backup_dir / name).exists() or (backup_dir / name).is_symlink():
        counter += 1
        name = f"{project_id}_backup_{stamp}_{counter}"
    staging = backup_dir / _tmp_name("tmp", name)
    try:
        files_count, size = _copy_tree(path, staging, progress, "Копирование проекта", scanned, locked)
        os.rename(staging, backup_dir / name)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    meta = {
        "reason": reason, "created_at": _iso(time.time()), "size_bytes": size, "files_count": files_count,
        "note": str(note or "")[:200], "source": "studio",
    }
    try:
        _meta_dir(backup_dir).mkdir(exist_ok=True)
        write_json_atomic(_meta_path(backup_dir, name), meta)
    except OSError:
        logger.warning("Не удалось записать метаданные бэкапа %s", name)
    cleanup_backups(backup_dir, project_id, max_backups, protect=(name, *protect))
    logger.info("Создан бэкап проекта %s: %s", project_id, name)
    return {"backup_id": name, "path": str(backup_dir / name), "size_bytes": size, "files_count": files_count}


def _backup_meta(backup_dir: Path, name: str) -> dict:
    meta = _read_json(_meta_path(backup_dir, name))
    return meta if isinstance(meta, dict) else {}


def cleanup_backups(backup_dir: Path, project_id: str, max_backups: int, protect: tuple[str, ...] = ()) -> None:
    """Оставляет max_backups новейших; защищённые и «перед удалением» не трогаем."""
    try:
        found = sorted(_iter_backups(backup_dir, project_id), key=lambda pair: _backup_sort_key(pair[1]), reverse=True)
        for item, _ in found[max(int(max_backups), 1):]:
            if item.name in protect or _backup_meta(backup_dir, item.name).get("reason") == "before_delete":
                continue
            shutil.rmtree(item)
            _meta_path(backup_dir, item.name).unlink(missing_ok=True)
            logger.info("Удалён старый бэкап: %s", item)
    except Exception as exc:
        logger.warning("Ошибка очистки старых бэкапов: %s", exc)


def list_backups(root: Path, backup_dir: Path, for_project_id: str = "") -> list[dict]:
    """Бэкапы проектов, новые сверху. Старые без метаданных получают причину unknown."""
    if for_project_id:
        for_project_id = _check_id(for_project_id)
    result = []
    for item, match in _iter_backups(backup_dir, for_project_id or None):
        meta = _backup_meta(backup_dir, item.name)
        reason = meta.get("reason") if meta.get("reason") in BACKUP_REASONS else "unknown"
        size = meta.get("size_bytes")
        if not isinstance(size, int):
            size = dir_size(item)["size_bytes"]
        try:
            created = datetime.strptime(match.group("ts"), "%Y%m%d_%H%M%S").isoformat(timespec="seconds")
        except ValueError:
            created = str(meta.get("created_at") or "")
        pid = match.group("id")
        result.append({
            "backup_id": item.name, "project_id": pid, "created_at": created, "size_bytes": size,
            "reason": reason, "reason_title": BACKUP_REASONS[reason], "note": str(meta.get("note") or ""),
            "project_exists": (root / pid).is_dir() and not is_service_dir(pid),
            "_key": _backup_sort_key(match),
        })
    result.sort(key=lambda row: (row["_key"]), reverse=True)
    for row in result:
        row.pop("_key")
    return result


def _find_backup(backup_dir: Path, project_id: str, backup_id: Any) -> tuple[Path, re.Match]:
    if not isinstance(backup_id, str) or not backup_id or "/" in backup_id or "\\" in backup_id or "\0" in backup_id:
        raise StudioError("invalid_argument", "Недопустимый идентификатор бэкапа")
    match = BACKUP_RE.match(backup_id)
    if not match:
        raise StudioError("invalid_argument", "Недопустимый идентификатор бэкапа")
    if match.group("id") != project_id:
        raise StudioError("invalid_argument", "Этот бэкап относится к другому проекту")
    path = backup_dir / backup_id
    if path.is_symlink() or not path.is_dir():
        raise StudioError("not_found", "Бэкап не найден")
    return path, match


def restore_backup(root: Path, backup_dir: Path, project_id: str, backup_id: str, *, max_backups: int = 10,
                   progress: Progress | None = None) -> dict:
    """Восстановление: бэкап текущего состояния, копия во временную папку, обмен через rename."""
    pid = _check_id(project_id)
    source, _ = _find_backup(backup_dir, pid, backup_id)
    scanned = _scan_tree(source)
    current = root / pid
    exists = current.is_dir() and not current.is_symlink()
    current_size = _scan_tree(current)[3] if exists else 0
    _check_space(root, int(scanned[3] * 1.1) + current_size)
    root.mkdir(parents=True, exist_ok=True)
    handle = _lock_or_raise(current) if exists else None
    staging = root / _tmp_name("restore", pid)
    old = root / _tmp_name("old", pid)
    safety_id = None
    try:
        if exists:
            safety_id = create_backup(root, backup_dir, pid, reason="before_restore", max_backups=max_backups,
                                      protect=(backup_id,), progress=progress, locked=True)["backup_id"]
        try:
            _copy_tree(source, staging, progress, "Восстановление проекта", scanned, locked=True)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        try:
            if exists:
                os.rename(current, old)
            try:
                os.rename(staging, current)
            except BaseException:
                if exists:
                    os.rename(old, current)
                raise
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if exists:
            shutil.rmtree(old, ignore_errors=True)
    finally:
        unlock(handle)
    cleanup_backups(backup_dir, pid, max_backups, protect=(backup_id, safety_id or ""))
    invalidate_size_cache()
    return {"project_id": pid, "restored_backup_id": backup_id, "safety_backup_id": safety_id}


def delete_project(root: Path, backup_dir: Path, project_id: str, *, max_backups: int = 10,
                   progress: Progress | None = None) -> dict:
    """Удаление: блокировка, бэкап `before_delete`, затем rmtree. Сбой бэкапа - проект цел."""
    path = _project_path(root, project_id)
    handle = _lock_or_raise(path)
    try:
        backup = create_backup(root, backup_dir, project_id, reason="before_delete",
                               max_backups=max_backups, progress=progress, locked=True)
    finally:
        unlock(handle)
    shutil.rmtree(path)
    invalidate_size_cache()
    logger.info("Проект %s удалён", project_id)
    return {"project_id": project_id, "backup_id": backup["backup_id"],
            "backup_size_bytes": backup["size_bytes"]}


def duplicate_project(root: Path, project_id: str, new_id: str, progress: Progress | None = None) -> dict:
    """Копия проекта в заранее занятую (пустую) папку new_id; исходный может быть занят."""
    path = _project_path(root, project_id)
    source_busy = is_busy(path)
    staging = root / _tmp_name("dup", new_id)
    try:
        _copy_tree(path, staging, progress, "Копирование проекта")
        brief_path = staging / "00_brief.json"
        brief = _read_json(brief_path)
        if isinstance(brief, dict):
            brief["title"] = f"{brief.get('title') or project_id} (копия)"
            write_json_atomic(brief_path, brief)
        os.rmdir(root / new_id)  # снять резерв и поставить копию на его место
        os.rename(staging, root / new_id)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(root / new_id, ignore_errors=True)
        raise
    invalidate_size_cache()
    return {"new_project_id": new_id, "source_busy": source_busy}


# ---------------------------------------------------------------- экспорт

def export_zip(project_dir: Path, output_path: Path, progress: Progress | None = None) -> dict:
    """ZIP проекта (без блокировки, миниатюр и прежних выгрузок, симлинки не кладём)."""
    _, files, _, _ = _scan_tree(project_dir)
    if not files:
        raise StudioError("invalid_argument", "Проект пуст, нечего экспортировать")
    output_path = Path(output_path)
    part = output_path.with_name(output_path.name + ".part")
    total = len(files)
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as archive:
            for index, parts in enumerate(sorted(files), 1):
                archive.write(project_dir.joinpath(*parts), "/".join(parts))
                if progress:
                    progress(index, total, "Упаковка проекта")
        os.replace(part, output_path)
    finally:
        part.unlink(missing_ok=True)
    return {"files_count": total, "size_bytes": output_path.stat().st_size}


def export_for_download(project_dir: Path, progress: Progress | None = None,
                        now: Callable[[], datetime] = datetime.now) -> dict:
    """ZIP внутрь проекта (`.studio/exports`), хранятся две последние выгрузки."""
    folder = project_dir / ".studio" / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{project_dir.name}_{now().strftime('%Y%m%d_%H%M%S')}.zip"
    info = export_zip(project_dir, folder / filename, progress)
    old = sorted(folder.glob("*.zip"), key=lambda item: item.name, reverse=True)
    for item in old[2:]:
        item.unlink(missing_ok=True)
    return {"path": f".studio/exports/{filename}", "filename": filename, **info}
