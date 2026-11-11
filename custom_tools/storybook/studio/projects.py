"""Проекты storybook (Ф2): список, карточка, создание, копия, бэкапы, восстановление, экспорт.

Тонкий слой над `project_store` и `jobs`: определяет папки из окружения и настроек,
долгие операции отдаёт фоновым заданиям. Параметры-подсказки называются `id_hint` и
`for_project_id`: параметр `project_id` в `invoke` всегда проверяется как готовый id.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from custom_tools.storybook.project_paths import storybook_projects_root
from StoryBookManager.config.settings import app_settings

from . import jobs, project_store
from .capabilities import capability
from .errors import StudioError, success
from .project_store import STAGES

logger = logging.getLogger(__name__)

STAGE_ORDER = {stage_id: index for index, (stage_id, _) in enumerate(STAGES, 1)}
SORTS = ("modified", "name", "stage")
KIND_CREATE = "create_from_prompt"


def _root() -> Path:
    return storybook_projects_root()


def _backup_dir() -> Path:
    return Path(app_settings.get_backup_directory())


def _max_backups() -> int:
    try:
        return max(int(app_settings.get("max_backup_files", 10)), 1)
    except (TypeError, ValueError):
        return 10


def _require_confirm(confirm: bool) -> None:
    if confirm is not True:
        raise StudioError("invalid_argument", "Подтвердите действие")


def _existing(project_id: str) -> Path:
    return project_store._project_path(_root(), project_id)


def _card(path: Path, creating: set[str] | None = None) -> dict:
    return project_store.build_card(path, creating=path.name in (creating or set()))


# ---------------------------------------------------------------- чтение

@capability("projects_list", title="Список проектов", access="read", project_scoped=False,
            supersedes=("project_inventory",))
def projects_list(*, search: str = "", sort: str = "modified", include_busy: bool = True) -> dict:
    if sort not in SORTS:
        raise StudioError("invalid_argument", f"Сортировка: {', '.join(SORTS)}")
    root = _root()
    if not root.is_dir():
        raise StudioError("not_found", f"Папка проектов не найдена: {root}", path=str(root))
    creating = jobs.active_project_ids(root, KIND_CREATE)
    try:
        cards = [_card(path, creating) for path in project_store.list_project_dirs(root)]
    except PermissionError as exc:
        raise StudioError("unavailable", "Нет прав на чтение папки проектов", path=str(root)) from exc
    total = len(cards)
    needle = (search or "").strip().casefold()
    if needle:
        cards = [c for c in cards if needle in c["title"].casefold() or needle in c["project_id"].casefold()]
    if not include_busy:
        cards = [c for c in cards if not c["busy"]]
    if sort == "name":
        cards.sort(key=lambda c: c["title"].casefold())
    elif sort == "stage":
        cards.sort(key=lambda c: (-c["stage"]["index"], c["title"].casefold()))
    else:
        cards.sort(key=lambda c: c["modified"], reverse=True)
    return success(projects_dir=str(root), total=total, count=len(cards), projects=cards)


@capability("projects_get", title="Карточка проекта", access="read")
def projects_get(project_id: str) -> dict:
    path = _existing(project_id)
    creating = jobs.active_project_ids(_root(), KIND_CREATE)
    return success(project=_card(path, creating), details=project_store.project_details(path))


@capability("projects_size", title="Размер проекта", access="read")
def projects_size(project_id: str) -> dict:
    return success(**project_store.dir_size(_existing(project_id)))


@capability("projects_suggest_id", title="Подсказка идентификатора", access="read", project_scoped=False)
def projects_suggest_id(*, id_hint: str) -> dict:
    if not isinstance(id_hint, str) or not id_hint.strip():
        raise StudioError("invalid_argument", "Введите название, из которого получится идентификатор")
    suggested = project_store.suggest_project_id(id_hint, _root())
    plain = re.sub(r"[^a-z0-9]+", "_", id_hint.strip().lower()).strip("_")
    return success(project_id=suggested, exists=False, changed=suggested != plain)


# ---------------------------------------------------------------- создание

@capability("projects_create", title="Создать проект", access="write", project_scoped=False,
            supersedes=("project_create_backup_export_delete",))
def projects_create(*, title: str, description: str, genre: str, target_age: str, language: str = "ru",
                    pages_min: int = 9, pages_max: int = 12, words_per_page_min: int = 400,
                    words_per_page_max: int = 450, id_hint: str = "") -> dict:
    root = _root()
    project_id = project_store.create_project(
        root, title=title, description=description, genre=genre, target_age=target_age, language=language,
        pages_min=pages_min, pages_max=pages_max, words_per_page_min=words_per_page_min,
        words_per_page_max=words_per_page_max, id_hint=id_hint if isinstance(id_hint, str) else "",
    )
    return success(project_id=project_id, project=_card(root / project_id))


def _prompt_hint(prompt: str) -> str:
    return " ".join(prompt.split()[:6])


@capability("projects_create_from_prompt", title="Создать проект из промпта", access="write", project_scoped=False)
def projects_create_from_prompt(*, prompt: str, id_hint: str = "", language: str = "ru", pages_min: int = 9,
                                pages_max: int = 12, words_per_page_min: int = 400,
                                words_per_page_max: int = 450) -> dict:
    raise StudioError("unsupported", "Создайте проект и добавьте материал при создании серии")


def _drop_if_empty(project_dir: Path) -> None:
    """Пустая папка (ни одного файла), созданная заданием, не должна остаться в списке."""
    if not project_dir.is_dir():
        return
    for _, _, files in os.walk(project_dir):
        if files:
            return
    shutil.rmtree(project_dir, ignore_errors=True)


# ---------------------------------------------------------------- правка

@capability("projects_duplicate", title="Копия проекта", access="write")
def projects_duplicate(project_id: str, *, id_hint: str = "") -> dict:
    root = _root()
    _existing(project_id)
    new_id = project_store.suggest_project_id(
        id_hint if isinstance(id_hint, str) and id_hint.strip() else f"{project_id}_copy", root, reserve=True)

    def work(progress) -> dict:
        return project_store.duplicate_project(root, project_id, new_id, progress)

    try:
        job_id = jobs.start_job(root, "duplicate", project_id, work)
    except BaseException:
        shutil.rmtree(root / new_id, ignore_errors=True)
        raise
    return success(job_id=job_id, new_project_id=new_id)


@capability("projects_rename", title="Переименовать проект", access="write")
def projects_rename(project_id: str, *, title: str) -> dict:
    root = _root()
    previous = project_store.rename_project(root, project_id, title)
    return success(project=_card(root / project_id), previous_title=previous)


@capability("projects_delete", title="Удалить проект", access="write")
def projects_delete(project_id: str, *, confirm: bool = False) -> dict:
    _require_confirm(confirm)
    root, backup_dir, limit = _root(), _backup_dir(), _max_backups()
    path = _existing(project_id)
    if project_store.is_busy(path):
        raise StudioError("locked", "Недоступно, пока идёт прогон")

    def work(progress) -> dict:
        return project_store.delete_project(root, backup_dir, project_id, max_backups=limit, progress=progress)

    return success(job_id=jobs.start_job(root, "delete", project_id, work))


# ---------------------------------------------------------------- бэкапы

@capability("projects_backups_list", title="Бэкапы проектов", access="read", project_scoped=False)
def projects_backups_list(*, for_project_id: str = "") -> dict:
    backup_dir = _backup_dir()
    rows = project_store.list_backups(_root(), backup_dir, for_project_id or "")
    return success(backup_dir=str(backup_dir), backups=rows)


@capability("projects_backup_create", title="Создать бэкап проекта", access="write")
def projects_backup_create(project_id: str, *, note: str = "") -> dict:
    root, backup_dir, limit = _root(), _backup_dir(), _max_backups()
    path = _existing(project_id)
    consistent = not project_store.is_busy(path)

    def work(progress) -> dict:
        free_before = not project_store.is_busy(path)
        info = project_store.create_backup(root, backup_dir, project_id, reason="manual", note=note or "",
                                           max_backups=limit, progress=progress)
        free_after = not project_store.is_busy(path)  # прогон мог начаться или закончиться во время копии
        return {"backup_id": info["backup_id"], "size_bytes": info["size_bytes"],
                "consistent": free_before and free_after}

    return success(job_id=jobs.start_job(root, "backup", project_id, work), consistent=consistent)


@capability("projects_backup_restore", title="Восстановить проект из бэкапа", access="write", project_scoped=False)
def projects_backup_restore(project_id: str, *, backup_id: str, confirm: bool = False) -> dict:
    _require_confirm(confirm)
    root, backup_dir, limit = _root(), _backup_dir(), _max_backups()
    project_store._find_backup(backup_dir, project_id, backup_id)  # ошибки id сразу, не в задании
    current = root / project_id
    if current.is_dir() and project_store.is_busy(current):
        raise StudioError("locked", "Недоступно, пока идёт прогон")

    def work(progress) -> dict:
        result = project_store.restore_backup(root, backup_dir, project_id, backup_id, max_backups=limit,
                                              progress=progress)
        return {**result, "project": _card(root / project_id)}

    return success(job_id=jobs.start_job(root, "restore", project_id, work))


# ---------------------------------------------------------------- экспорт и задания

@capability("projects_export", title="Экспорт проекта в ZIP", access="write")
def projects_export(project_id: str) -> dict:
    root = _root()
    path = _existing(project_id)

    def work(progress) -> dict:
        return project_store.export_for_download(path, progress)

    return success(job_id=jobs.start_job(root, "export", project_id, work))


@capability("projects_job_status", title="Состояние фонового задания", access="read", project_scoped=False)
def projects_job_status(*, job_id: str) -> dict:
    return success(**jobs.read_job(_root(), job_id))


@capability("projects_update", title="Изменить проект", access="write")
def projects_update(project_id: str, title: str, description: str, language: str, defaults: dict) -> dict:
    if not isinstance(title, str) or not title.strip() or not isinstance(description, str) or not isinstance(language, str) or not isinstance(defaults, dict):
        raise StudioError("invalid_argument", "Проверьте данные проекта")
    path = _existing(project_id) / "project.json"
    data = project_store._read_json(path)
    data.update(title=title.strip(), description=description, language=language, defaults=defaults)
    project_store.write_json_atomic(path, data)
    return success(project_id=project_id, project=data)
