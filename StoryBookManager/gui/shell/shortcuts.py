"""Горячие клавиши оболочки: таблица, развёртка в последовательности Tk (без tkinter)."""

import sys
from dataclasses import dataclass
from typing import List, Optional

# Та же клавиша на русской раскладке: латиница -> имя keysym (строчная, прописная)
CYRILLIC_KEYSYMS = {
    "f": ("Cyrillic_a", "Cyrillic_A"),
    "k": ("Cyrillic_el", "Cyrillic_EL"),
    "n": ("Cyrillic_te", "Cyrillic_TE"),
    "o": ("Cyrillic_shcha", "Cyrillic_SHCHA"),
    "s": ("Cyrillic_yeru", "Cyrillic_YERU"),
}

# Сочетания, которые у Text/Entry уже заняты встроенными привязками класса
CLASS_CONFLICTS = ("k", "n", "f")
TEXT_INPUT_CLASSES = ("Text", "Entry", "TEntry", "TCombobox", "Spinbox", "TSpinbox")


@dataclass(frozen=True)
class Shortcut:
    action: str     # section:N | quick_project | refresh | new_project | open_project | save | close_toast
    label: str
    display: str    # как показывать пользователю
    key: str        # без модификатора: "1", "k", "F5", "Escape"
    ctrl: bool = True


SHORTCUTS = tuple(
    [Shortcut(f"section:{n}", f"Перейти к разделу {n}", f"Ctrl+{n}", str(n)) for n in range(1, 9)]
    + [
        Shortcut("quick_project", "Быстрый выбор проекта", "Ctrl+K", "k"),
        Shortcut("refresh", "Обновить данные раздела", "F5", "F5", ctrl=False),
        Shortcut("new_project", "Новый проект", "Ctrl+N", "n"),
        Shortcut("open_project", "Открыть проект (раздел «Проекты»)", "Ctrl+O", "o"),
        Shortcut("save", "Сохранить в текущем разделе", "Ctrl+S", "s"),
        Shortcut("find", "Поиск в списке проектов", "Ctrl+F", "f"),
        Shortcut("find_slash", "Поиск в списке проектов", "/", "slash", ctrl=False),
        Shortcut("close_toast", "Закрыть всплывающее сообщение", "Esc", "Escape", ctrl=False),
    ]
)

# Правка: действуют внутри полей ввода сами, в справке только перечислены
EDIT_HELP = (
    ("Ctrl+Z", "Отменить"),
    ("Ctrl+Y", "Повторить"),
    ("Ctrl+C", "Копировать"),
    ("Ctrl+V", "Вставить"),
)


def accelerator_for(action: str) -> str:
    for shortcut in SHORTCUTS:
        if shortcut.action == action:
            return shortcut.display
    return ""


def expand_sequences(key: str, platform: Optional[str] = None, ctrl: bool = True) -> List[str]:
    """Все последовательности Tk для одной клавиши: регистры, русская раскладка, Command на macOS."""
    platform = platform or sys.platform
    keys = [key]
    if len(key) == 1 and key.isalpha():
        keys = [key.lower(), key.upper()]
        keys += list(CYRILLIC_KEYSYMS.get(key.lower(), ()))
    if not ctrl:
        return [f"<{k}>" for k in keys]
    mods = ["Control"] + (["Command"] if platform == "darwin" else [])
    return [f"<{mod}-Key-{k}>" for mod in mods for k in keys]


def should_skip_in_widget(widget_class: str, key: str) -> bool:
    """Простую клавишу (без Ctrl/Alt/Command и не F-клавишу) в поле ввода не перехватываем."""
    if widget_class not in TEXT_INPUT_CLASSES:
        return False
    lowered = key.lower()
    if any(mod in lowered for mod in ("control", "command", "alt")):
        return False
    if len(key) > 1 and key[0] in "Ff" and key[1:].isdigit():
        return False
    return True


def help_text() -> str:
    """Справка «Горячие клавиши» из той же таблицы."""
    lines = ["Горячие клавиши:", ""]
    lines += [f"{s.display} - {s.label}" for s in SHORTCUTS]
    lines += [""] + [f"{keys} - {label}" for keys, label in EDIT_HELP]
    return "\n".join(lines)
