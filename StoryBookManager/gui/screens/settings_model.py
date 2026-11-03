"""Чистая модель экрана «Настройки»: форма, изменённые поля, payload (без tkinter)."""

import re

from .settings_texts import CHECK_TEXT, LOG_LEVELS, SETTINGS_TEXTS

PATH_FIELDS = (("projects_directory", "projects"), ("backup_directory", "backups"), ("logs_directory", "logs"))
SERVER_FIELDS = ("projects_directory", "backup_directory", "logs_directory", "max_backup_files", "log_level")
TK_FIELDS = ("ui_scale", "auto_save_interval")
LEVEL_LABELS = dict(LOG_LEVELS)
LABEL_LEVELS = {v: k for k, v in LOG_LEVELS}


def form_from_snapshot(snapshot):
    """Значения полей формы (всё строками) из ответа app_settings_get."""
    s = snapshot["settings"]
    form = {key: str(s[key]["value"]) for key in SERVER_FIELDS}
    form["log_level"] = log_level_label(form["log_level"])
    return form


def _norm(key, value):
    value = str(value).strip()
    return value.upper() if key == "log_level" else value


def changed_fields(form, base):
    """Какие поля формы отличаются от загруженных значений (после обрезки пробелов; уровень без учёта регистра)."""
    return [key for key in form if _norm(key, form[key]) != _norm(key, base.get(key, ""))]


def tk_changes(form, base):
    out = {}
    for key in changed_fields(form, base):
        if key in TK_FIELDS:
            try:
                out[key] = int(str(form[key]).strip())
            except ValueError:
                name = SETTINGS_TEXTS["ui_scale"] if key == "ui_scale" else SETTINGS_TEXTS["autosave"]
                raise ValueError(SETTINGS_TEXTS["bad_number"].format(name=name.split(",")[0])) from None
    return out


def check_text(state):
    return SETTINGS_TEXTS[CHECK_TEXT.get(state, "check_unknown")]


# --- то же, что в settingsView.ts (эталон golden_settings_vectors.json) ---

_FIELD_TITLE = {"projects_directory": "projects", "backup_directory": "backups", "logs_directory": "logs"}


def log_level_label(level):
    return LEVEL_LABELS.get(str(level).upper(), level)


def default_mark(entry):
    return SETTINGS_TEXTS["default_mark"] if entry.get("source") == "default" else None


def field_lock(snapshot, field):
    """Папка проектов закрыта окружением сервера или идущим прогоном; остальные поля открыты."""
    if field != "projects_directory":
        return {"disabled": False, "note": None}
    if snapshot["settings"]["projects_directory"].get("source") == "env":
        return {"disabled": True, "note": SETTINGS_TEXTS["env_hint"]}
    if snapshot.get("projects_directory_locked_reason"):
        return {"disabled": True, "note": snapshot["projects_directory_locked_reason"]}
    return {"disabled": False, "note": None}


def build_update(form, base, snapshot):
    """Запрос на сохранение: только изменённые поля и expected_digest; формы с кодами уровня (DEBUG...)."""
    errors, payload = {}, {}
    for field in changed_fields(form, base):
        if field_lock(snapshot, field)["disabled"]:
            continue
        value = _norm(field, form[field])
        if field == "max_backup_files":
            n = int(value) if re.fullmatch(r"[0-9]+", value) else None
            if n is None or not 1 <= n <= 100:
                errors[field] = SETTINGS_TEXTS["max_backups_invalid"]
            else:
                payload[field] = n
        elif field == "log_level":
            payload[field] = value
        elif value == "":
            errors[field] = SETTINGS_TEXTS["path_empty"].format(name=SETTINGS_TEXTS[_FIELD_TITLE[field]])
        else:
            payload[field] = value
    if errors:
        return {"ok": False, "errors": errors}
    return {"ok": True, "payload": {**payload, "expected_digest": snapshot["digest"]}}


def field_for_server_error(message):
    """Куда повесить ошибку сервера: по началу сообщения."""
    if message.startswith("Папка проектов"):
        return "projects_directory"
    if message.startswith("Папка бэкапов"):
        return "backup_directory"
    if message.startswith("Папка логов"):
        return "logs_directory"
    if "Число бэкапов" in message:
        return "max_backup_files"
    if "Уровень логов" in message:
        return "log_level"
    return "paths"


def should_check_path(value):
    return str(value).strip() != ""
