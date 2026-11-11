"""Логика экрана «Проекты» без tkinter: фильтр, сортировка, тексты, правила кнопок, очередь размеров.

Тексты взяты из раздела 5 UX-спеки и совпадают с lib/storybook/texts.ts (React).
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from StoryBookManager.gui.shell.dialog_texts import ConfirmSpec

NBSP = " "

# --- Тексты (TXT_*) ------------------------------------------------------------
TXT_TITLE = "Проекты сказок"
TXT_CREATE_PROMPT = "Создать из промпта"
TXT_CREATE_FORM = "Создать проект"
TXT_REFRESH = "Обновить"
TXT_ALL_BACKUPS = "Бэкапы всех проектов"
TXT_SEARCH = "Поиск"
TXT_SEARCH_HINT = "Название или id проекта"
TXT_SORT = "Сортировка"
TXT_LOADING = "Загрузка проектов…"
TXT_EMPTY = "Проектов пока нет. Создайте проект и опишите его мир."
TXT_RESET_SEARCH = "Сбросить поиск"
TXT_RETRY = "Повторить"
TXT_DETAILS = "Подробности"
TXT_OPEN_SETTINGS = "Открыть настройки"
TXT_COUNTING_SIZES = "Считаем размер…"
TXT_CREATING = "Создаётся…"
TXT_RUN_BADGE = "Идёт прогон"
TXT_EMPTY_BADGE = "Пустой"
TXT_BUSY_HINT = "Недоступно, пока идёт прогон."
TXT_CREATING_HINT = "Проект ещё создаётся."
TXT_SELECTED = "Выбранный проект"
TXT_SELECT_NONE = "Выберите проект в списке."
TXT_OPEN = "Открыть проект"
TXT_DUPLICATE = "Дублировать"
TXT_RENAME = "Переименовать"
TXT_BACKUPS = "Бэкапы"
TXT_EXPORT = "Экспорт в ZIP"
TXT_SHOW_FOLDER = "Показать в папке"
TXT_REMOVE = "Удалить"
TXT_OPEN_OVERVIEW = "Открыть обзор"
TXT_CANCEL = "Отмена"
TXT_CLOSE = "Закрыть"
TXT_SAVE = "Сохранить"
TXT_PROMPT_TITLE = "Создать из промпта"
TXT_PROMPT_FIELD = "Идея сказки"
TXT_PROMPT_HINT = "Опишите сюжет, героев, настроение, возраст читателя"
TXT_PROMPT_LLM = "Будет вызвана языковая модель."
TXT_CREATE_AND_OPEN = "Создать и открыть"
TXT_TEXT_PARAMS = "Параметры текста"
TXT_LANGUAGE = "Язык"
TXT_PAGES_MIN = "Страниц от"
TXT_PAGES_MAX = "Страниц до"
TXT_WORDS_MIN = "Слов на страницу от"
TXT_WORDS_MAX = "Слов на страницу до"
TXT_FORM_TITLE = "Создать проект"
TXT_FIELD_TITLE = "Название"
TXT_FIELD_DESCRIPTION = "Описание мира"
TXT_FIELD_GENRE = "Жанр"
TXT_FIELD_AGE = "Возраст читателя"
TXT_ID_LABEL = "Идентификатор проекта"
TXT_ID_HINT = "Латиница, цифры, дефис и подчёркивание; создаётся автоматически из названия"
TXT_CREATE = "Создать"
TXT_RENAME_TITLE = "Переименовать проект"
TXT_DUPLICATE_TITLE = "Копия проекта"
TXT_DUPLICATE_ID = "Идентификатор копии (необязательно)"
TXT_DUPLICATE_ID_HINT = "Если оставить пустым, будет предложено «{id}_copy»."
TXT_BACKUPS_TITLE = "Бэкапы"
TXT_BACKUPS_ALL = "Бэкапы всех проектов (в том числе удалённых)"
TXT_BACKUP_DATE = "Дата"
TXT_BACKUP_SIZE = "Размер"
TXT_BACKUP_REASON = "Причина"
TXT_BACKUP_PROJECT = "Проект"
TXT_BACKUP_NOTE = "Заметка (необязательно)"
TXT_BACKUP_CREATE = "Создать бэкап"
TXT_BACKUP_RESTORE = "Восстановить"
TXT_BACKUPS_EMPTY = "Бэкапов пока нет."
TXT_BACKUP_INCONSISTENT = "Во время копирования шёл прогон: файлы могли измениться"
TXT_RESTORE_BODY = "Текущая версия проекта будет сохранена в новый бэкап."
TXT_INTERRUPTED = "Операция прервана"
TXT_NO_PROJECT_SELECTED = "Выберите проект в списке."
TXT_PROJECT_LOCKED = ("С этим проектом уже работает другое приложение или прогон. "
                      "Дождитесь конца или остановите прогон.")
TXT_BACKUPS_LOADING = "Загрузка бэкапов…"
TXT_SIZE = "Размер"
TXT_CREATING_PROGRESS = "Создаём проект…"

TOAST_CREATED = "Проект создан"
TOAST_DUPLICATED = "Проект продублирован"
TOAST_RENAMED = "Проект переименован"
TOAST_BACKUP_CREATED = "Бэкап создан"
TOAST_BACKUP_RESTORED = "Бэкап восстановлен"

PROMPT_MIN = 10
PROMPT_MAX = 8000

SORTS: Tuple[Tuple[str, str], ...] = (
    ("modified", "Изменён"),
    ("name", "Название"),
    ("size", "Размер"),
)
SORT_LABELS = {value: label for value, label in SORTS}
DEFAULT_SORT = "modified"

SIZE_QUEUE_PAUSE_MS = 150


def toast_exported(filename: str) -> str:
    return f"Экспорт готов: {filename}"


def toast_removed(size_text: str) -> str:
    return f"Проект удалён. Бэкап создан ({size_text})"


def text_nothing_found(query: str) -> str:
    return f"Ничего не найдено по запросу «{query}»."


def text_load_failed(reason: str) -> str:
    return f"Не удалось загрузить список проектов: {reason}."


def text_folder_missing(path: Optional[str]) -> str:
    return f"Папка проектов не найдена: {path}." if path else "Папка проектов не найдена."


def text_delete_title(title: str) -> str:
    return f"Удалить проект «{title}»?"


def text_delete_body(size_text: str) -> str:
    return (f"Перед удалением будет создан бэкап ({size_text}). Восстановить проект можно "
            "в разделе «Проекты», кнопка «Бэкапы всех проектов».")


def text_restore_title(date_text: str) -> str:
    return f"Восстановить бэкап от {date_text}?"


def text_job_progress(message: str, done: int, total: int) -> str:
    if total > 0:
        return f"{message} ({done} из {total})"
    return message


def text_job_failed(message: str) -> str:
    return f"Не получилось: {message}"


def delete_spec(title: str, size_text: str) -> ConfirmSpec:
    """Подтверждение удаления: «Отмена» по умолчанию, Enter не удаляет."""
    return ConfirmSpec(title=text_delete_title(title), body=text_delete_body(size_text), confirm="Удалить")


def restore_spec(date_text: str) -> ConfirmSpec:
    return ConfirmSpec(title=text_restore_title(date_text), body=TXT_RESTORE_BODY, confirm=TXT_BACKUP_RESTORE)


# --- Форматирование ---------------------------------------------------------------
def format_bytes(value) -> str:
    """«6.4 ГБ»; неизвестный размер - «—»."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return "—"
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    amount = float(value)
    unit = 0
    while amount >= 1024 and unit < len(units) - 1:
        amount /= 1024
        unit += 1
    if unit == 0:
        return f"{int(round(amount))}{NBSP}{units[0]}"
    text = str(int(round(amount))) if amount >= 100 else f"{amount:.1f}"
    return f"{text}{NBSP}{units[unit]}"


_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?")


def format_date(iso) -> str:
    """«27.10.2026»."""
    match = _ISO.match(iso) if isinstance(iso, str) else None
    return f"{match.group(3)}.{match.group(2)}.{match.group(1)}" if match else "—"


def format_datetime(iso) -> str:
    """«27.10.2026 14:05» (пояс не пересчитываем: сервер отдаёт местное время)."""
    match = _ISO.match(iso) if isinstance(iso, str) else None
    if not match:
        return "—"
    day = f"{match.group(3)}.{match.group(2)}.{match.group(1)}"
    return f"{day} {match.group(4)}:{match.group(5)}" if match.group(4) else day


# --- Фильтр и сортировка --------------------------------------------------------------
def filter_cards(cards: Iterable[dict], query: str) -> List[dict]:
    """Поиск без учёта регистра по названию и id."""
    needle = (query or "").strip().casefold()
    if not needle:
        return list(cards)
    return [c for c in cards
            if needle in str(c.get("title", "")).casefold() or needle in str(c.get("project_id", "")).casefold()]


def sort_cards(cards: Iterable[dict], sort: str, sizes: Optional[Dict[str, int]] = None) -> List[dict]:
    """Стабильная сортировка. «Размер»: неизвестные размеры в конец."""
    sizes = sizes or {}
    items = list(cards)

    def title_key(card):
        return str(card.get("title", "")).casefold()

    if sort == "name":
        return sorted(items, key=title_key)
    if sort == "stage":
        return sorted(items, key=lambda c: (-int((c.get("stage") or {}).get("index", 0)), title_key(c)))
    if sort == "size":
        known = sorted((c for c in items if c.get("project_id") in sizes),
                       key=lambda c: -sizes[c["project_id"]])
        return known + [c for c in items if c.get("project_id") not in sizes]
    return sorted(items, key=lambda c: str(c.get("modified", "")), reverse=True)


# --- Правила кнопок ------------------------------------------------------------------
@dataclass(frozen=True)
class Gate:
    allowed: bool
    reason: str = ""


def is_creating(card: dict) -> bool:
    return card.get("state") == "creating"


def gate_change(card: dict) -> Gate:
    """Удалить и переименовать нельзя, пока идёт прогон или проект создаётся."""
    if card.get("busy"):
        return Gate(False, TXT_BUSY_HINT)
    if is_creating(card):
        return Gate(False, TXT_CREATING_HINT)
    return Gate(True)


def gate_open(card: dict) -> Gate:
    return Gate(False, TXT_CREATING_HINT) if is_creating(card) else Gate(True)


def gate_copy(card: dict, job_running: bool = False) -> Gate:
    """Копия и экспорт разрешены и при прогоне, но не во время создания и не при втором задании."""
    if is_creating(card):
        return Gate(False, TXT_CREATING_HINT)
    if job_running:
        return Gate(False, "Для этого проекта уже идёт операция.")
    return Gate(True)


# --- Строки списка ------------------------------------------------------------------
@dataclass(frozen=True)
class RowModel:
    project_id: str
    title: str
    series: str
    modified: str
    size: str
    tags: Tuple[str, ...] = ()


def row_for(card: dict, size: Optional[int], job_text: str = "") -> RowModel:
    series = str(card.get("series_count", 0))
    tags: List[str] = []
    if is_creating(card):
        series, tags = TXT_CREATING, ["muted"]
    elif job_text:
        series = job_text
    elif card.get("busy"):
        series = f"{series} · {TXT_RUN_BADGE}"
        tags.append("busy")
    return RowModel(
        project_id=str(card.get("project_id", "")), title=str(card.get("title") or card.get("project_id", "")),
        series=series, modified=format_date(card.get("modified")),
        size=format_bytes(size) if size is not None else "—", tags=tuple(tags))


# --- Состояние списка --------------------------------------------------------------------
@dataclass(frozen=True)
class ListState:
    kind: str                  # loading | empty | nothing | error | rows
    text: str = ""
    actions: Tuple[str, ...] = ()   # prompt | form | reset | retry | details | settings


def list_state(*, loading: bool, error: Optional[dict], total: int, shown: int, query: str) -> ListState:
    if error:
        return ListState("error", error_list_text(error), error_list_actions(error))
    if loading:
        return ListState("loading", TXT_LOADING)
    if total == 0:
        return ListState("empty", TXT_EMPTY, ("form",))
    if shown == 0:
        return ListState("nothing", text_nothing_found(query.strip()), ("reset",))
    return ListState("rows")


def error_list_text(error: dict) -> str:
    code = error.get("code")
    if code == "not_found":
        path = (error.get("details") or {}).get("path")
        return text_folder_missing(path or _path_in_message(error.get("message", "")))
    if code == "unavailable":
        return "Нет прав на чтение папки проектов."
    return text_load_failed(str(error.get("message") or "ошибка").rstrip("."))


def _path_in_message(message: str) -> Optional[str]:
    prefix = "Папка проектов не найдена:"
    return message[len(prefix):].strip() if message.startswith(prefix) else None


def error_list_actions(error: dict) -> Tuple[str, ...]:
    if error.get("code") == "not_found":
        return ("retry", "settings", "details")
    return ("retry", "details")


def error_text(error: Optional[dict]) -> str:
    """Короткий текст ошибки возможности для тоста или поля диалога (спека 5.2)."""
    error = error or {}
    code = error.get("code")
    if code == "locked":
        return TXT_PROJECT_LOCKED
    if code == "interrupted":
        return TXT_INTERRUPTED
    return str(error.get("message") or "Что-то пошло не так. Повторите действие.")


# --- Размеры по очереди -----------------------------------------------------------------------
class SizeQueue:
    """Размеры считаются по одному проекту: диск не нагружаем.

    next_id() отдаёт очередной проект без известного размера; пока идёт запрос, второй не выдаётся.
    """

    def __init__(self):
        self.sizes: Dict[str, int] = {}
        self._pending: List[str] = []
        self._current: Optional[str] = None
        self._generation = 0

    def reset(self, project_ids: Iterable[str], keep: bool = True) -> None:
        """Новый список проектов: известные размеры оставляем, лишние убираем."""
        ids = list(project_ids)
        self._generation += 1
        self._current = None
        self.sizes = {k: v for k, v in self.sizes.items() if keep and k in ids}
        self._pending = [i for i in ids if i not in self.sizes]

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def busy(self) -> bool:
        return self._current is not None

    @property
    def remaining(self) -> int:
        return len(self._pending) + (1 if self._current else 0)

    def next_id(self) -> Optional[str]:
        if self._current is not None or not self._pending:
            return None
        self._current = self._pending.pop(0)
        return self._current

    def done(self, project_id: str, size: Optional[int], generation: int) -> bool:
        """Принять ответ; False, если он устарел."""
        if generation != self._generation or project_id != self._current:
            return False
        self._current = None
        if isinstance(size, int):
            self.sizes[project_id] = size
        return True


# --- Прочее ---------------------------------------------------------------------------------------
def validate_prompt(text: str) -> bool:
    return PROMPT_MIN <= len((text or "").strip()) <= PROMPT_MAX


def form_ready(title: str, description: str, genre: str, age: str) -> bool:
    return all((v or "").strip() for v in (title, description, genre, age))


def default_backup_scope(selected_id: Optional[str]) -> Tuple[bool, str]:
    """(все проекты, for_project_id): без выбранного проекта показываем бэкапы всех."""
    return (True, "") if not selected_id else (False, selected_id)


def int_or_none(text: str) -> Optional[int]:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def zip_target(projects_dir: str, project_id: str, relative: str) -> str:
    """Где сервер положил ZIP: <папка проектов>/<проект>/<относительный путь>."""
    return str(Path(projects_dir) / project_id / relative)
