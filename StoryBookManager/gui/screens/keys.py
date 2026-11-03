"""Горячие клавиши экранов: привязка к окну с отпиской и проверка, что клавиша адресована экрану."""

import tkinter as tk

# фокус в этих виджетах сам обрабатывает пробел, стрелки и буквы
_OWN_KEYS = {"TButton", "Button", "TCheckbutton", "Checkbutton", "TRadiobutton", "Radiobutton", "TEntry", "Entry",
             "Text", "TCombobox", "Spinbox", "TSpinbox", "Treeview", "Listbox", "TScale", "Scale", "TScrollbar"}


def key_is_for_screen(screen):
    """True: экран на виду, фокус не в кнопке или поле и нет окна, которое держит ввод (модального)."""
    if not screen.winfo_viewable():
        return False
    try:
        grab = screen.grab_current()
        if grab is not None and grab.winfo_toplevel() is not screen.winfo_toplevel():
            return False
        focus = screen.focus_get()
    except (tk.TclError, KeyError):
        return False
    if focus is not None and focus.winfo_toplevel() is not screen.winfo_toplevel():
        return False
    return focus is None or focus.winfo_class() not in _OWN_KEYS


class KeyBindings:
    """Привязки к окну приложения; снимаются одним вызовом (в destroy экрана)."""

    def __init__(self, widget):
        self.widget = widget
        self.items = []

    def bind(self, sequence, handler):
        top = self.widget.winfo_toplevel()
        self.items.append((top, sequence, top.bind(sequence, handler, add="+")))

    def clear(self):
        for top, sequence, func_id in self.items:
            try:
                top.unbind(sequence, func_id)
            except tk.TclError:
                pass
        self.items = []
