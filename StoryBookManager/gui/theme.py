"""
Тёплая издательская ttk-тема для StoryBookManager
===================================================

Палитра, шрифты и масштаб берутся из theme_tokens (раздел 1 UX-спеки).
apply_theme(root, ui_scale) безопасно вызывать повторно: именованные шрифты
не пересоздаются, стили перезаписываются.
"""

import tkinter as tk
from tkinter import ttk, font as tkfont

from StoryBookManager.gui import theme_tokens as tokens
from StoryBookManager.gui.theme_tokens import COLORS, px

# Совместимое имя; реальные семейства подставляет apply_theme
FONTS: dict = {
    "title":    ("TkDefaultFont", 14, "bold"),
    "subtitle": ("TkDefaultFont", 11, "bold"),
    "body":     ("TkDefaultFont", 10),
    "caption":  ("TkDefaultFont", 9),
    "mono":     ("TkFixedFont", 10),
    "mono_sm":  ("TkFixedFont", 9),
}


def init_scaling(root: tk.Tk, ui_scale: int = 100) -> float:
    """Задаёт tk scaling по dpi экрана и пользовательскому масштабу; возвращает множитель px()."""
    ui_scale = tokens.normalize_ui_scale(ui_scale)
    try:
        base = root.winfo_fpixels("1i") / 72.0
        if base <= 0:
            base = 96.0 / 72.0
        root.tk.call("tk", "scaling", base * ui_scale / 100.0)
        factor = tokens.scale_factor(base, ui_scale)
    except tk.TclError:
        factor = ui_scale / 100.0
    tokens.set_scale(factor)
    return factor


def _ensure_font(root, name: str, **opts) -> None:
    """Создаёт именованный шрифт или правит существующий (повторный вызов не падает)."""
    # Объект Font удаляет шрифт при сборке мусора, поэтому держим ссылки на самом окне
    fonts = root.__dict__.setdefault("_sbm_fonts", {})
    if name in fonts:
        fonts[name].configure(**opts)
        return
    fonts[name] = tkfont.Font(root=root, name=name, **opts)


def _setup_fonts(root) -> None:
    try:
        families = tkfont.families(root)
    except tk.TclError:
        families = ()
    sans = tokens.pick_font("sans", families) or tkfont.nametofont("TkDefaultFont", root=root).actual("family")
    mono = tokens.pick_font("mono", families) or tkfont.nametofont("TkFixedFont", root=root).actual("family")
    names = {
        "title": "SBM.Title", "subtitle": "SBM.Subtitle", "body": "SBM.Body",
        "caption": "SBM.Caption", "mono": "SBM.Mono", "mono_sm": "SBM.MonoSm",
    }
    for role, name in names.items():
        kind, size, bold = tokens.FONT_ROLES[role]
        family = sans if kind == "sans" else mono
        _ensure_font(root, name, family=family, size=size, weight="bold" if bold else "normal")
        FONTS[role] = (family, size, "bold") if bold else (family, size)


def apply_theme(root: tk.Tk, ui_scale: int = 100) -> None:
    """Применяет тему к корневому окну Tkinter."""
    init_scaling(root, ui_scale)
    style = ttk.Style(root)

    try:
        style.theme_use("clam")
    except tk.TclError:
        pass  # clam недоступен - продолжаем с текущей темой

    _setup_fonts(root)
    root.configure(background=COLORS["window_bg"])
    c = COLORS

    # --- Базовые виджеты ---------------------------------------------------
    style.configure("TFrame", background=c["window_bg"])
    style.configure("TLabel", background=c["window_bg"], foreground=c["text_primary"], font="SBM.Body")
    style.configure("Title.TLabel", background=c["window_bg"], foreground=c["text_primary"], font="SBM.Title")
    style.configure("PanelTitle.TLabel", background=c["window_bg"], foreground=c["text_primary"], font="SBM.Title")
    style.configure("Subtitle.TLabel", background=c["window_bg"], foreground=c["text_primary"], font="SBM.Subtitle")
    style.configure("Caption.TLabel", background=c["window_bg"], foreground=c["text_muted"], font="SBM.Caption")

    style.configure("TLabelframe", background=c["window_bg"], bordercolor=c["border_strong"], relief="groove")
    style.configure("TLabelframe.Label", background=c["window_bg"], foreground=c["text_primary"], font="SBM.Subtitle")

    # --- Кнопки ------------------------------------------------------------
    style.configure(
        "TButton",
        background=c["surface"], foreground=c["text_primary"], font="SBM.Body",
        padding=(px(8), px(4)), relief="raised", borderwidth=1,  # clam рисует рамку только так
        bordercolor=c["border_strong"], lightcolor=c["surface"], darkcolor=c["surface"],
        focuscolor=c["running"], focusthickness=2,
    )
    style.map(
        "TButton",
        background=[("disabled", c["surface"]), ("active", c["accent_soft"])],
        foreground=[("disabled", c["text_disabled"])],
        bordercolor=[("disabled", c["border"]), ("focus", c["running"])],
    )

    style.configure(
        "Accent.TButton",
        background=c["accent"], foreground=c["accent_text"], font="SBM.Body",
        padding=(px(12), px(5)), relief="flat", borderwidth=0, focuscolor=c["running"],
        focusthickness=2,
    )
    style.map(
        "Accent.TButton",
        background=[("disabled", c["surface"]), ("active", c["accent_hover"])],
        foreground=[("disabled", c["text_disabled"])],
    )

    # --- Навигация ---------------------------------------------------------
    nav_base = dict(
        font="SBM.Body", anchor="w", relief="flat", borderwidth=0,
        padding=(px(12), px(10)), focuscolor=c["nav_text"], focusthickness=2,
    )
    style.configure("Nav.TButton", background=c["nav_bg"], foreground=c["nav_text"], **nav_base)
    style.map("Nav.TButton", background=[("active", c["nav_selected_bg"])],
              foreground=[("active", c["accent_text"])])
    style.configure("NavSelected.TButton", background=c["nav_selected_bg"], foreground=c["accent_text"], **nav_base)
    style.map("NavSelected.TButton", background=[("active", c["nav_selected_bg"])])
    style.configure("NavDisabled.TButton", background=c["nav_bg"], foreground=c["nav_muted"], **nav_base)
    style.map("NavDisabled.TButton", background=[("active", c["nav_bg"])],
              foreground=[("active", c["nav_muted"])])

    # --- Верхняя и нижняя строки ------------------------------------------
    style.configure("TopBar.TFrame", background=c["surface"])
    style.configure("TopBar.TLabel", background=c["surface"], foreground=c["text_primary"], font="SBM.Body")
    style.configure("TopBarTitle.TLabel", background=c["surface"], foreground=c["text_primary"], font="SBM.Subtitle")
    style.configure("TopBarMuted.TLabel", background=c["surface"], foreground=c["text_muted"], font="SBM.Body")
    style.configure("StatusBar.TFrame", background=c["surface"])
    style.configure("StatusBar.TLabel", background=c["surface"], foreground=c["text_primary"], font="SBM.Caption")
    style.configure("Panel.TFrame", background=c["window_bg"])
    style.configure("Header.TFrame", background=c["surface"], relief="flat", borderwidth=0)

    # --- Внутренние вкладки (редактор) -------------------------------------
    style.configure("TNotebook", background=c["window_bg"], bordercolor=c["border_strong"],
                    tabmargins=(2, 2, 2, 0))
    style.configure("TNotebook.Tab", background=c["surface"], foreground=c["text_primary"],
                    font="SBM.Body", padding=(px(10), px(4)))
    style.map("TNotebook.Tab",
              background=[("selected", c["card"]), ("active", c["accent_soft"])],
              foreground=[("selected", c["accent"])])

    # --- Таблицы -----------------------------------------------------------
    style.configure("Treeview", background=c["card"], fieldbackground=c["card"],
                    foreground=c["text_primary"], font="SBM.Body", rowheight=px(28),
                    bordercolor=c["border_strong"])
    style.configure("Treeview.Heading", background=c["surface"], foreground=c["text_primary"],
                    font="SBM.Subtitle", relief="flat")
    style.map("Treeview", background=[("selected", c["accent_soft"])],
              foreground=[("selected", c["text_primary"])])

    # --- Поля ввода --------------------------------------------------------
    for name in ("TEntry", "TCombobox"):
        style.configure(name, fieldbackground=c["card"], foreground=c["text_primary"],
                        bordercolor=c["border_strong"], font="SBM.Body", padding=(px(4), px(2)))
    style.map("TEntry", bordercolor=[("focus", c["running"])])
    style.map("TCombobox", bordercolor=[("focus", c["running"])],
              fieldbackground=[("readonly", c["card"])])
    style.configure("TSpinbox", fieldbackground=c["card"], foreground=c["text_primary"],
                    bordercolor=c["border_strong"], font="SBM.Body")
    style.map("TSpinbox", bordercolor=[("focus", c["running"])])

    # --- Остальное ---------------------------------------------------------
    style.configure("TProgressbar", troughcolor=c["surface"], background=c["accent"],
                    bordercolor=c["border_strong"])
    style.configure("TScrollbar", background=c["surface"], troughcolor=c["window_bg"],
                    bordercolor=c["border"], arrowcolor=c["text_muted"])
    style.configure("TSeparator", background=c["border"])
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=c["window_bg"], foreground=c["text_primary"],
                        font="SBM.Body", focuscolor=c["running"])
        style.map(name, background=[("active", c["window_bg"])],
                  foreground=[("disabled", c["text_disabled"])])
