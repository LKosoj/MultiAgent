"""Горячие клавиши экрана «Кадры» (без tkinter).

Ctrl+S идёт через оболочку (SectionAdapter.on_save); здесь остальные клавиши экрана.
Русская раскладка задаётся именами keysym, как в shell/shortcuts.py.
"""

from typing import Dict, List, Tuple

from StoryBookManager.gui.shell.shortcuts import TEXT_INPUT_CLASSES

# латинская буква -> (строчный, прописной keysym той же клавиши в русской раскладке)
LETTER_KEYSYMS: Dict[str, Tuple[str, str]] = {
    "a": ("Cyrillic_ef", "Cyrillic_EF"),
    "r": ("Cyrillic_ka", "Cyrillic_KA"),
    "x": ("Cyrillic_che", "Cyrillic_CHE"),
}

# действие -> клавиша без модификатора
ACTION_KEYS: Dict[str, str] = {
    "prev": "Up",
    "next": "Down",
    "approve": "a",
    "redo": "r",
    "reject": "x",
    "mark": "space",
    "open": "Return",
}
CTRL_ACTIONS: Dict[str, str] = {"mark_all": "a"}

# letter -> статус ревью (A/R/X)
STATUS_BY_LETTER = {"a": "approved", "r": "redo", "x": "rejected"}
STATUS_ACTIONS = {"approve": "a", "redo": "r", "reject": "x"}

# В этих виджетах простые клавиши нужны самому виджету
OWN_KEYS_CLASSES = TEXT_INPUT_CLASSES + ("Treeview", "Listbox", "TNotebook")
# Пробел и Enter нажимают кнопку, а не отмечают шот
PRESS_CLASSES = ("TButton", "Button", "TCheckbutton", "Checkbutton", "TRadiobutton", "Radiobutton")


def sequences_for(action: str) -> List[str]:
    """Все последовательности Tk для действия: регистры и русская раскладка."""
    if action in CTRL_ACTIONS:
        letter = CTRL_ACTIONS[action]
        names = [letter, letter.upper()] + list(LETTER_KEYSYMS.get(letter, ()))
        return [f"<Control-Key-{n}>" for n in names]
    key = ACTION_KEYS[action]
    if len(key) == 1 and key.isalpha():
        names = [key, key.upper()] + list(LETTER_KEYSYMS.get(key, ()))
        return [f"<Key-{n}>" for n in names]
    return [f"<Key-{key}>"]


def all_sequences() -> Dict[str, List[str]]:
    return {action: sequences_for(action) for action in list(ACTION_KEYS) + list(CTRL_ACTIONS)}


def should_handle(widget_class: str, action: str, modal_open: bool, mapped: bool) -> bool:
    """Обрабатывать ли клавишу экрана: экран виден, окон поверх нет, фокус не в поле ввода."""
    if modal_open or not mapped:
        return False
    if widget_class in OWN_KEYS_CLASSES:
        return False
    if action in ("mark", "open") and widget_class in PRESS_CLASSES:
        return False
    return True
