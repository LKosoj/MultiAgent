"""
Тёплая издательская ttk-тема для StoryBookManager
===================================================

Модуль предоставляет палитру цветов (COLORS), шрифты (FONTS) и функцию
apply_theme(root), которая однократно применяет тему к корневому окну Tkinter.

Палитра: тёплые издательские тона на базе clam-темы ttk (stdlib-only).
"""

import tkinter as tk
from tkinter import ttk, font as tkfont

# ---------------------------------------------------------------------------
# Палитра цветов
# ---------------------------------------------------------------------------

COLORS: dict = {
    "window_bg":    "#F5F0EB",  # фон главного окна
    "surface":      "#EDE8E2",  # поверхность виджетов (кнопки, фреймы)
    "accent":       "#A0522D",  # акцентный цвет (sienna)
    "accent_hover": "#C46A3A",  # акцент при наведении
    "text_primary": "#2C2018",  # основной текст
    "text_muted":   "#7A6A5A",  # вторичный/заглушённый текст
    "border":       "#C8BFB4",  # цвет границ
    "success":      "#4A7C59",  # успех
    "error":        "#9B2335",  # ошибка
    "warning":      "#8B6914",  # предупреждение
    "info":         "#2B5797",  # информация
    "running":      "#1A5276",  # выполняется
}

# ---------------------------------------------------------------------------
# Шрифты
# ---------------------------------------------------------------------------

FONTS: dict = {
    "title":    ("Segoe UI", 13, "bold"),
    "subtitle": ("Segoe UI", 11, "bold"),
    "body":     ("Segoe UI", 10),
    "caption":  ("Segoe UI",  9),
    "mono":     ("Consolas",  10),
    "mono_sm":  ("Consolas",   9),
}


def apply_theme(root: tk.Tk) -> None:
    """Применяет тёплую издательскую тему к корневому окну Tkinter.

    Использует только stdlib ttk (никаких внешних зависимостей).
    Безопасен для повторного вызова — стили перезаписываются.

    Аргументы:
        root: корневое окно tk.Tk.
    """
    style = ttk.Style(root)

    # --- базовая тема clam -------------------------------------------------
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass  # clam недоступен — продолжаем с текущей темой

    # --- именованные шрифты ------------------------------------------------
    _make_font = tkfont.Font
    _make_font(root, name="SBM.Title",    family="Segoe UI", size=13, weight="bold")
    _make_font(root, name="SBM.Subtitle", family="Segoe UI", size=11, weight="bold")
    _make_font(root, name="SBM.Body",     family="Segoe UI", size=10)
    _make_font(root, name="SBM.Caption",  family="Segoe UI", size=9)
    _make_font(root, name="SBM.Mono",     family="Consolas", size=10)
    _make_font(root, name="SBM.MonoSm",   family="Consolas", size=9)

    # --- фон корневого окна ------------------------------------------------
    root.configure(background=COLORS["window_bg"])

    c = COLORS  # краткий псевдоним

    # --- TFrame ------------------------------------------------------------
    style.configure("TFrame", background=c["window_bg"])

    # --- TLabel ------------------------------------------------------------
    style.configure(
        "TLabel",
        background=c["window_bg"],
        foreground=c["text_primary"],
        font="SBM.Body",
    )
    style.configure(
        "Title.TLabel",
        background=c["window_bg"],
        foreground=c["text_primary"],
        font="SBM.Title",
    )
    style.configure(
        "Subtitle.TLabel",
        background=c["window_bg"],
        foreground=c["text_primary"],
        font="SBM.Subtitle",
    )
    style.configure(
        "Caption.TLabel",
        background=c["window_bg"],
        foreground=c["text_muted"],
        font="SBM.Caption",
    )

    # --- TLabelframe -------------------------------------------------------
    style.configure(
        "TLabelframe",
        background=c["window_bg"],
        bordercolor=c["border"],
        relief="groove",
    )
    style.configure(
        "TLabelframe.Label",
        background=c["window_bg"],
        foreground=c["text_primary"],
        font="SBM.Subtitle",
    )

    # --- TButton -----------------------------------------------------------
    style.configure(
        "TButton",
        background=c["surface"],
        foreground=c["text_primary"],
        font="SBM.Body",
        padding=(8, 4),
        relief="flat",
        borderwidth=1,
        focuscolor=c["accent"],
    )
    style.map(
        "TButton",
        background=[("active", c["border"]), ("disabled", c["surface"])],
        foreground=[("disabled", c["text_muted"])],
    )

    # --- Accent.TButton ----------------------------------------------------
    style.configure(
        "Accent.TButton",
        background=c["accent"],
        foreground="#FFFFFF",
        font="SBM.Body",
        padding=(12, 5),
        relief="flat",
        borderwidth=0,
    )
    style.map(
        "Accent.TButton",
        background=[("active", c["accent_hover"]), ("disabled", c["border"])],
        foreground=[("disabled", "#FFFFFF")],
    )

    # --- TNotebook ---------------------------------------------------------
    style.configure(
        "TNotebook",
        background=c["window_bg"],
        bordercolor=c["border"],
        tabmargins=(2, 2, 2, 0),
    )
    style.configure(
        "TNotebook.Tab",
        background="#D8D0C8",
        foreground=c["text_primary"],
        font="SBM.Body",
        padding=(10, 4),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", c["window_bg"]), ("active", c["surface"])],
        foreground=[("selected", c["accent"])],
    )

    # --- Treeview ----------------------------------------------------------
    style.configure(
        "Treeview",
        background=c["window_bg"],
        fieldbackground=c["window_bg"],
        foreground=c["text_primary"],
        font="SBM.Body",
        rowheight=22,
        bordercolor=c["border"],
    )
    style.configure(
        "Treeview.Heading",
        background=c["surface"],
        foreground=c["text_primary"],
        font="SBM.Subtitle",
        relief="flat",
    )
    style.map(
        "Treeview",
        background=[("selected", c["accent"])],
        foreground=[("selected", "#FFFFFF")],
    )

    # --- TEntry ------------------------------------------------------------
    style.configure(
        "TEntry",
        fieldbackground="#FFFFFF",
        foreground=c["text_primary"],
        bordercolor=c["border"],
        font="SBM.Body",
        padding=(4, 2),
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", c["accent"])],
    )

    # --- TCombobox ---------------------------------------------------------
    style.configure(
        "TCombobox",
        fieldbackground="#FFFFFF",
        foreground=c["text_primary"],
        bordercolor=c["border"],
        font="SBM.Body",
    )
    style.map(
        "TCombobox",
        bordercolor=[("focus", c["accent"])],
        fieldbackground=[("readonly", "#FFFFFF")],
    )

    # --- TProgressbar ------------------------------------------------------
    style.configure(
        "TProgressbar",
        troughcolor=c["surface"],
        background=c["accent"],
        bordercolor=c["border"],
    )

    # --- TScrollbar --------------------------------------------------------
    style.configure(
        "TScrollbar",
        background=c["surface"],
        troughcolor=c["window_bg"],
        bordercolor=c["border"],
        arrowcolor=c["text_muted"],
    )

    # --- TSeparator --------------------------------------------------------
    style.configure(
        "TSeparator",
        background=c["border"],
    )

    # --- TCheckbutton ------------------------------------------------------
    style.configure(
        "TCheckbutton",
        background=c["window_bg"],
        foreground=c["text_primary"],
        font="SBM.Body",
        focuscolor=c["accent"],
    )
    style.map(
        "TCheckbutton",
        background=[("active", c["window_bg"])],
    )

    # --- TRadiobutton ------------------------------------------------------
    style.configure(
        "TRadiobutton",
        background=c["window_bg"],
        foreground=c["text_primary"],
        font="SBM.Body",
        focuscolor=c["accent"],
    )
    style.map(
        "TRadiobutton",
        background=[("active", c["window_bg"])],
    )

    # --- Header.TFrame (кастомный стиль, использовался в main_window) ------
    style.configure(
        "Header.TFrame",
        background=c["surface"],
        relief="raised",
        borderwidth=1,
    )
