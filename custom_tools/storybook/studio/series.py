"""Постоянные общие ресурсы и самостоятельные серии проекта."""
from __future__ import annotations

import base64
import binascii
import copy
import shutil
import uuid
from pathlib import Path

from custom_tools.storybook.project_paths import require_safe_storybook_project_id, storybook_projects_root
from . import project_store
from .capabilities import capability
from .errors import StudioError, success

KINDS = ("characters", "locations", "items", "style", "narrators")


def _id(value, field):
    return require_safe_storybook_project_id(value, field)


def _project(project_id):
    return project_store._project_path(storybook_projects_root(), project_id)


def series_dir(project_id, series_id):
    root = _project(project_id)
    sid = _id(series_id, "series_id")
    path = root / "series" / sid
    if (root / "series").is_symlink() or path.is_symlink() or not path.is_dir() or not project_store._inside(path, root):
        raise StudioError("not_found", f"Серия не найдена: {sid}")
    return path


def _read(path):
    value = project_store._read_json(path)
    if not isinstance(value, dict):
        raise StudioError("not_found", f"Данные не найдены: {path.name}")
    return value


def _resource_path(root, kind, entity_id, version_id):
    if kind not in KINDS:
        raise StudioError("invalid_argument", "Неизвестный тип ресурса")
    entity_id = _id(entity_id, "entity_id")
    version_id = _id(version_id, "version_id")
    path = root / "shared" / kind / entity_id / version_id / "data.json"
    if not project_store._inside(path, root / "shared"):
        raise StudioError("forbidden_path", "Ресурс вне проекта")
    return path


def resolve_snapshot(project_id, series_id):
    root = _project(project_id)
    data = _read(series_dir(project_id, series_id) / "series.json")
    resolved = {}
    for kind, entries in data["snapshot"].items():
        resolved[kind] = {}
        for entity, version in entries.items():
            path = _resource_path(root, kind, entity, version)
            value = _read(path)
            reference = value.get("reference_image_path")
            if reference:
                image = (path.parent / reference).resolve()
                if not project_store._inside(image, path.parent) or not image.is_file():
                    raise StudioError("not_found", f"Образец версии не найден: {kind}/{entity}/{version}")
                value["reference_image_path"] = str(image)
            resolved[kind][entity] = {"version_id": version, "data": value}
    return resolved


def mark_series_stale(root: Path, areas: list[str], *, sound_only: bool = False) -> None:
    path = root / "series.json"
    if not path.is_file():
        return
    data = _read(path)
    existing = [area for area in areas if (root / area).exists()]
    data["stale_results"] = list(dict.fromkeys(data.get("stale_results", []) + existing))
    revision = "sound_revision" if sound_only else "input_revision"
    data[revision] = data.get(revision, 0) + 1
    project_store.write_json_atomic(path, data)


@capability("series_list", title="Серии проекта", access="read")
def series_list(project_id: str) -> dict:
    root = _project(project_id) / "series"
    return success(project_id=project_id, series=[_read(p / "series.json") for p in sorted(root.iterdir())
                                                   if p.is_dir() and not p.is_symlink() and (p / "series.json").is_file()])


@capability("series_create", title="Создать серию", access="write")
def series_create(project_id: str, title: str, source_material: str, participant_ids: list, settings: dict) -> dict:
    root = _project(project_id)
    if not isinstance(title, str) or not title.strip() or not isinstance(source_material, str) or not isinstance(participant_ids, list) or not isinstance(settings, dict):
        raise StudioError("invalid_argument", "Проверьте данные серии")
    project = _read(root / "project.json")
    snapshot = copy.deepcopy(project.get("resources", {}))
    characters = snapshot.get("characters", {})
    if any(not isinstance(item, str) or item not in characters for item in participant_ids):
        raise StudioError("invalid_argument", "Выбран неизвестный персонаж")
    if "characters" in snapshot:
        snapshot["characters"] = {key: characters[key] for key in participant_ids}
    sid = project_store.suggest_project_id(title, root / "series", reserve=True)
    path = root / "series" / sid
    data = {"series_id": sid, "title": title.strip(), "source_material": source_material,
            "participant_ids": participant_ids, "settings": copy.deepcopy(settings), "snapshot": snapshot}
    from ..reference_catalog import initialize_series_references, unbind_series
    try:
        data["snapshot"] = initialize_series_references(root, sid, source_material, participant_ids, snapshot)
        for folder in project_store.PROJECT_STRUCTURE:
            (path / folder).mkdir(parents=True, exist_ok=True)
        project_store.write_json_atomic(path / "series.json", data)
        project_store.write_json_atomic(path / "00_brief.json", {"title": title, "description": source_material,
            "language": project.get("language", "ru"), **project.get("defaults", {}), **settings,
            "main_characters": [], "main_locations": [], "storybook_prompt": source_material})
    except BaseException:
        unbind_series(root, sid)
        shutil.rmtree(path, ignore_errors=True)
        raise
    return success(project_id=project_id, series_id=sid, series=data)


@capability("series_get", title="Открыть серию", access="read")
def series_get(project_id: str, series_id: str) -> dict:
    return success(project_id=project_id, series_id=series_id,
                   series=_read(series_dir(project_id, series_id) / "series.json"), resources=resolve_snapshot(project_id, series_id))


@capability("series_update", title="Изменить серию", access="write")
def series_update(project_id: str, series_id: str, title: str, source_material: str, settings: dict,
                  participant_ids: list | None = None) -> dict:
    path = series_dir(project_id, series_id) / "series.json"
    data = _read(path)
    if not isinstance(title, str) or not title.strip() or not isinstance(source_material, str) or not isinstance(settings, dict):
        raise StudioError("invalid_argument", "Проверьте данные серии")
    speech_settings = ("language", "voice", "narrator", "speech_style")
    old_settings = data.get("settings") or {}
    speech_changed = (source_material != data.get("source_material")
                      or (participant_ids is not None and participant_ids != data.get("participant_ids"))
                      or any(settings.get(key) != old_settings.get(key) for key in speech_settings))
    changed = speech_changed or settings != old_settings
    if participant_ids is not None:
        if not isinstance(participant_ids, list):
            raise StudioError("invalid_argument", "Проверьте участников")
        available = _read(_project(project_id) / "project.json").get("resources", {}).get("characters", {})
        if any(not isinstance(item, str) or item not in available for item in participant_ids):
            raise StudioError("invalid_argument", "Выбран неизвестный персонаж")
        selected = data["snapshot"].get("characters", {})
        data["snapshot"]["characters"] = {item: selected.get(item, available[item]) for item in participant_ids}
        data["participant_ids"] = participant_ids
    data.update(title=title.strip(), source_material=source_material, settings=settings)
    if changed:
        areas = (("10_synopsis", "20_bible", "30_style", "20_story", "40_prompts", "50_items", "50_images",
                  "91_screenplay", "93_blockout", "97_shots", "98_audio", "99_final") if speech_changed else
                 ("30_style", "40_prompts", "50_items", "50_images", "93_blockout", "97_shots", "99_final"))
        existing = [area for area in areas if (area != "98_audio" or speech_changed)
                    and (path.parent / area).is_dir() and any((path.parent / area).iterdir())]
        data["stale_results"] = list(dict.fromkeys(data.get("stale_results", []) + existing))
        data["input_revision"] = data.get("input_revision", 0) + 1
    project_store.write_json_atomic(path, data)
    brief_path = path.parent / "00_brief.json"
    brief = _read(brief_path)
    brief.update(settings)
    brief.update(title=title.strip(), description=source_material, storybook_prompt=source_material)
    project_store.write_json_atomic(brief_path, brief)
    if speech_changed:
        speech_path = path.parent / "98_audio/speech.json"
        speech = project_store._read_json(speech_path)
        if isinstance(speech, dict):
            speech["status"] = "stale"
            project_store.write_json_atomic(speech_path, speech)
    return success(project_id=project_id, series_id=series_id, series=data)


@capability("series_delete", title="Удалить серию", access="write")
def series_delete(project_id: str, series_id: str, confirm: bool) -> dict:
    import fcntl
    from custom_tools.storybook.project_paths import (
        acquire_storybook_project_lock, release_storybook_project_lock, series_scope,
    )
    if confirm is not True:
        raise StudioError("invalid_argument", "Подтвердите действие")
    path = series_dir(project_id, series_id)
    with series_scope(project_id, series_id):
        handle = acquire_storybook_project_lock(project_id)
    if handle is None:
        raise StudioError("locked", "Проект или серия заняты. Дождитесь завершения операции")
    settings_lock = None
    try:
        from .run_settings import SETTINGS_RELPATH
        settings_path = path / SETTINGS_RELPATH
        settings_path.parent.mkdir(exist_ok=True)
        settings_lock = open(str(settings_path) + ".lock", "a+", encoding="utf-8")
        fcntl.flock(settings_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        deleting = path.parent.parent / project_store._tmp_name("delete_series", series_id)
        path.rename(deleting)
        shutil.rmtree(deleting)
        from ..reference_catalog import unbind_series
        unbind_series(path.parent.parent, series_id)
    except BlockingIOError as exc:
        raise StudioError("locked", "Сохраняются настройки серии. Дождитесь завершения") from exc
    finally:
        release_storybook_project_lock(settings_lock)
        release_storybook_project_lock(handle)
    return success(project_id=project_id, series_id=series_id)


@capability("shared_resource_save", title="Сохранить общий ресурс", access="write")
def shared_resource_save(project_id: str, kind: str, entity_id: str, data: dict,
                         reference_base64: str = "", reference_filename: str = "") -> dict:
    root = _project(project_id)
    if not isinstance(data, dict):
        raise StudioError("invalid_argument", "Ресурс должен быть объектом")
    if kind == "style" and any(key != entity_id for key in _read(root / "project.json").get("resources", {}).get("style", {})):
        raise StudioError("invalid_argument", "В проекте используется один общий стиль. Создайте новую версию существующего стиля")
    version_id = uuid.uuid4().hex
    path = _resource_path(root, kind, entity_id, version_id)
    data = copy.deepcopy(data)
    reference = None
    if reference_base64:
        suffix = Path(reference_filename).suffix.lower()
        if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
            raise StudioError("invalid_argument", "Неподдерживаемый формат изображения")
        try:
            reference = base64.b64decode(reference_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise StudioError("invalid_argument", "Неверные данные изображения") from exc
        if not reference:
            raise StudioError("invalid_argument", "Пустое изображение")
        data["reference_image_path"] = "reference" + suffix
    elif data.get("reference_image_path"):
        project = _read(root / "project.json")
        current = project.get("resources", {}).get(kind, {}).get(entity_id)
        if not current:
            raise StudioError("invalid_argument", "Загрузите образец в ресурс")
        old_path = _resource_path(root, kind, entity_id, current)
        old_data = _read(old_path)
        old_ref = old_data.get("reference_image_path")
        source = (old_path.parent / str(old_ref or "")).resolve()
        supplied = data["reference_image_path"]
        if not old_ref or supplied not in (old_ref, str(source)) or not project_store._inside(source, old_path.parent) or not source.is_file():
            raise StudioError("not_found", f"Образец версии не найден: {kind}/{entity_id}/{current}")
        reference = source.read_bytes()
        data["reference_image_path"] = Path(old_ref).name
    path.parent.mkdir(parents=True)
    if reference is not None:
        (path.parent / data["reference_image_path"]).write_bytes(reference)
    project_store.write_json_atomic(path, data)
    project = _read(root / "project.json")
    project.setdefault("resources", {}).setdefault(kind, {})[entity_id] = version_id
    project_store.write_json_atomic(root / "project.json", project)
    from ..reference_catalog import _shared_reference
    _shared_reference(root, kind, entity_id, version_id)
    return success(project_id=project_id, kind=kind, entity_id=entity_id, version_id=version_id)


def _resource_update(project_id, series_id, kind, entity_id, version_id):
    root = _project(project_id)
    path = series_dir(project_id, series_id)
    data = _read(path / "series.json")
    current = data.get("snapshot", {}).get(kind, {}).get(entity_id)
    if current is None:
        raise StudioError("not_found", "Ресурс не выбран в серии")
    old = _read(_resource_path(root, kind, entity_id, current))
    new = _read(_resource_path(root, kind, entity_id, version_id))
    voice_changed = kind in ("characters", "narrators") and any(
        old.get(field) != new.get(field) for field in ("voice", "language", "speech_style"))
    # Показываем существующие выходы, которые могут зависеть от общего ресурса.
    canon = {"characters": "20_bible/characters.json", "locations": "20_bible/locations.json"}.get(kind)
    affected = [canon] if canon and (path / canon).is_file() else []
    folders = ("30_style", "40_prompts", "50_items", "50_images", "91_screenplay", "97_shots", "98_audio", "99_final")
    affected.extend(folder for folder in folders if (folder != "98_audio" or voice_changed)
                    and (path / folder).is_dir() and any((path / folder).iterdir()))
    if voice_changed and (path / "98_audio/speech.json").is_file():
        affected.append("98_audio/speech.json")
    return path, data, current, affected


@capability("series_resource_update_preview", title="Последствия обновления ресурса", access="read")
def series_resource_update_preview(project_id: str, series_id: str, kind: str, entity_id: str, version_id: str) -> dict:
    _, _, current, affected = _resource_update(project_id, series_id, kind, entity_id, version_id)
    return success(project_id=project_id, series_id=series_id, current_version_id=current,
                   version_id=version_id, affected_results=affected)


@capability("series_resource_update_apply", title="Применить версию ресурса", access="write")
def series_resource_update_apply(project_id: str, series_id: str, kind: str, entity_id: str, version_id: str) -> dict:
    path, data, current, affected = _resource_update(project_id, series_id, kind, entity_id, version_id)
    if "98_audio/speech.json" in affected:
        speech_path = path / "98_audio/speech.json"
        speech = _read(speech_path)
        speech["status"] = "stale"
        project_store.write_json_atomic(speech_path, speech)
    data["snapshot"][kind][entity_id] = version_id
    data["input_revision"] = data.get("input_revision", 0) + 1
    data["stale_results"] = list(dict.fromkeys(data.get("stale_results", []) + affected))
    project_store.write_json_atomic(path / "series.json", data)
    from ..reference_catalog import sync_snapshot_references
    sync_snapshot_references(path)
    return success(project_id=project_id, series_id=series_id, previous_version_id=current,
                   version_id=version_id, affected_results=affected)


def materialize_snapshot(project_id: str, series_id: str) -> None:
    """Подготовить входы существующих инструментов из закреплённых версий."""
    path = series_dir(project_id, series_id)
    resources = resolve_snapshot(project_id, series_id)
    from ..reference_catalog import bootstrap_series_references, sync_snapshot_references
    sync_snapshot_references(path)
    bootstrap_series_references(path)
    for kind in ("characters", "locations"):
        target = path / "20_bible" / f"{kind}.json"
        previous = project_store._read_json(target)
        local = [item for item in previous if isinstance(item, dict) and not item.get("_shared_id")] if isinstance(previous, list) else []
        shared = []
        existing = {item.get("_shared_id"): item for item in previous if isinstance(item, dict)} if isinstance(previous, list) else {}
        for entity_id, item in resources.get(kind, {}).items():
            current = existing.get(entity_id)
            unchanged_character = kind == "characters" and current and current.get("_shared_version_id") == item["version_id"]
            value = copy.deepcopy(current if unchanged_character else item["data"])
            value["_shared_id"] = entity_id
            value["_shared_version_id"] = item["version_id"]
            if not value.get("reference_image_path"):
                value["reference_image_path"] = f"/references/{kind}/{entity_id}.png"
            if kind == "locations":
                value["_passport_version"] = 3
            shared.append(value)
        if (shared or isinstance(previous, list)) and previous != shared + local:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file():
                from .artifacts import _snapshot
                from custom_tools.storybook.project_paths import series_scope
                with series_scope(project_id, series_id):
                    _snapshot(project_id, kind, target.read_bytes(), "external")
            project_store.write_json_atomic(target, shared + local)
    styles = resources.get("style", {})
    if styles:
        if len(styles) != 1:
            raise StudioError("invalid_argument", "В серии должен быть выбран один стиль")
        style = next(iter(styles.values()))["data"]
        target = path / "30_style"
        target.mkdir(parents=True, exist_ok=True)
        for key in ("style_text", "style_images"):
            output = target / f"{key}.json"
            value = style.get(key, {})
            if project_store._read_json(output) != value:
                if output.is_file():
                    from .artifacts import _snapshot
                    from custom_tools.storybook.project_paths import series_scope
                    with series_scope(project_id, series_id):
                        _snapshot(project_id, key, output.read_bytes(), "external")
                project_store.write_json_atomic(output, value)
        (target / "negative_prompt_list.txt").write_text(style.get("negative_list", ""), encoding="utf-8")


@capability("shared_resource_get", title="Версия общего ресурса", access="read")
def shared_resource_get(project_id: str, kind: str, entity_id: str, version_id: str) -> dict:
    root = _project(project_id)
    data = _read(_resource_path(root, kind, entity_id, version_id))
    reference = data.get("reference_image_path")
    reference_path = (f"shared/{kind}/{entity_id}/{version_id}/{reference}"
                      if reference and not Path(reference).is_absolute() else None)
    return success(project_id=project_id, kind=kind, entity_id=entity_id, version_id=version_id,
                   data=data, reference_path=reference_path)


@capability("shared_resource_versions", title="Версии общего ресурса", access="read")
def shared_resource_versions(project_id: str, kind: str, entity_id: str) -> dict:
    root = _project(project_id)
    sample = _resource_path(root, kind, entity_id, "version")
    directory = sample.parent.parent
    versions = sorted(p.name for p in directory.iterdir() if p.is_dir() and not p.is_symlink() and (p / "data.json").is_file()) if directory.is_dir() else []
    return success(project_id=project_id, kind=kind, entity_id=entity_id, versions=versions)
