"""Модальные окна оболочки: подтверждение по таблице 5.3, быстрый выбор проекта (Ctrl+K), подробности ошибки."""

import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional, Tuple

from StoryBookManager.gui.shell.dialog_texts import ConfirmSpec
from StoryBookManager.gui.theme_tokens import COLORS, PAD_BLOCK, PAD_INLINE, px


def _center_on(win: tk.Toplevel, parent) -> None:
    win.update_idletasks()
    try:
        x = parent.winfo_rootx() + (parent.winfo_width() - win.winfo_reqwidth()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
    except tk.TclError:
        pass


class ConfirmDialog(tk.Toplevel):
    """Заголовок-вопрос, строка последствий, «Отмена» слева, действие справа.

    Enter нажимает кнопку в фокусе; по умолчанию фокус на «Отмене», поэтому
    дорогое или необратимое действие Enter не подтверждает. Esc закрывает.
    """

    def __init__(self, parent, spec: ConfirmSpec):
        super().__init__(parent)
        self.result = False
        self.title(spec.title)
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.configure(background=COLORS["window_bg"])
        box = ttk.Frame(self, padding=px(PAD_BLOCK))
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=spec.title, style="Subtitle.TLabel").pack(anchor="w")
        ttk.Label(box, text=spec.body, wraplength=px(420), justify="left").pack(anchor="w", pady=(px(PAD_INLINE), px(PAD_BLOCK)))
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.cancel_btn = ttk.Button(row, text=spec.cancel, command=self._cancel)
        self.cancel_btn.pack(side="left")
        self.confirm_btn = ttk.Button(row, text=spec.confirm, style="Accent.TButton", command=self._confirm)
        self.confirm_btn.pack(side="right")
        self.bind("<Escape>", lambda e: self._cancel())
        self.bind("<Return>", self._press_focused)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        _center_on(self, parent)
        try:
            self.grab_set()
        except tk.TclError:
            pass
        (self.cancel_btn if spec.default_cancel else self.confirm_btn).focus_set()

    def _press_focused(self, _event):
        widget = self.focus_get()
        if isinstance(widget, ttk.Button):
            widget.invoke()
        return "break"

    def _confirm(self):
        self.result = True
        self.destroy()

    def _cancel(self):
        self.result = False
        self.destroy()

    @classmethod
    def ask(cls, parent, spec: ConfirmSpec) -> bool:
        dialog = cls(parent, spec)
        parent.wait_window(dialog)
        return dialog.result


def show_details(parent, text: str, title: str = "Подробности") -> tk.Toplevel:
    """Окно с полным текстом ошибки (ссылка «Подробнее» у тоста-ошибки)."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent.winfo_toplevel())
    box = ttk.Frame(win, padding=px(PAD_INLINE))
    box.pack(fill="both", expand=True)
    body = tk.Text(box, wrap="word", width=70, height=14, background=COLORS["card"],
                   foreground=COLORS["text_primary"], font="SBM.Mono")
    body.insert("1.0", text)
    body.configure(state="disabled")
    body.pack(fill="both", expand=True)
    ttk.Button(box, text="Закрыть", command=win.destroy).pack(anchor="e", pady=(px(PAD_INLINE), 0))
    win.bind("<Escape>", lambda e: win.destroy())
    _center_on(win, parent)
    return win


class QuickProjectPicker(tk.Toplevel):
    """Ctrl+K: поле поиска и список проектов; Enter открывает, Esc закрывает, стрелки двигают выбор."""

    def __init__(self, parent, projects: List[Tuple[str, str]], on_pick: Callable[[str], None]):
        super().__init__(parent)
        self._projects = list(projects)  # (project_id, название)
        self._shown: List[str] = []
        self._on_pick = on_pick
        self.title("Быстрый выбор проекта")
        self.transient(parent.winfo_toplevel())
        self.configure(background=COLORS["window_bg"])
        box = ttk.Frame(self, padding=px(PAD_BLOCK))
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="Название проекта", style="Caption.TLabel").pack(anchor="w")
        self.query = tk.StringVar()
        self.entry = ttk.Entry(box, textvariable=self.query, width=48)
        self.entry.pack(fill="x", pady=(px(4), px(PAD_INLINE)))
        self.listbox = tk.Listbox(box, height=10, activestyle="none", exportselection=False,
                                  background=COLORS["card"], foreground=COLORS["text_primary"],
                                  selectbackground=COLORS["accent_soft"], selectforeground=COLORS["text_primary"],
                                  highlightcolor=COLORS["running"], highlightthickness=2, font="SBM.Body")
        self.listbox.pack(fill="both", expand=True)
        self.empty_label = ttk.Label(box, text="", style="Caption.TLabel")
        self.empty_label.pack(anchor="w", pady=(px(4), 0))
        self.query.trace_add("write", lambda *a: self._filter())
        for widget in (self.entry, self.listbox):
            widget.bind("<Down>", lambda e: self._move(1) or "break")
            widget.bind("<Up>", lambda e: self._move(-1) or "break")
            widget.bind("<Return>", lambda e: self._pick() or "break")
        self.listbox.bind("<Double-Button-1>", lambda e: self._pick())
        self.bind("<Escape>", lambda e: self.destroy())
        self._filter()
        _center_on(self, parent)
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.entry.focus_set()

    def _filter(self):
        needle = self.query.get().strip().casefold()
        self.listbox.delete(0, "end")
        self._shown = []
        for project_id, title in self._projects:
            if needle in title.casefold() or needle in project_id.casefold():
                self._shown.append(project_id)
                self.listbox.insert("end", title if title == project_id else f"{title}  ({project_id})")
        if self._shown:
            self.listbox.selection_set(0)
            self.empty_label.configure(text="")
        else:
            self.empty_label.configure(text="Проектов не найдено")

    def _move(self, step: int):
        if not self._shown:
            return
        current = self.listbox.curselection()
        index = (current[0] if current else -1) + step
        index = max(0, min(len(self._shown) - 1, index))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(index)
        self.listbox.see(index)

    def _pick(self):
        current = self.listbox.curselection()
        if not current:
            return
        project_id = self._shown[current[0]]
        self.destroy()
        self._on_pick(project_id)
