"""
Виджет визуального трекера шагов pipeline
=========================================

Отображает список шагов с иконками статуса.
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional
import logging

logger = logging.getLogger(__name__)

STATUS_ICONS = {
    "pending": "\u2b1c",        # ⬜
    "running": "\U0001f504",    # 🔄
    "completed": "\u2705",      # ✅
    "failed": "\u274c",         # ❌
    "skipped": "\u23ed\ufe0f",  # ⏭️
    "cancelled": "\U0001f6ab",  # 🚫
}

STATUS_COLORS = {
    "pending": "#666666",
    "running": "#0066CC",
    "completed": "#008000",
    "failed": "#CC0000",
    "skipped": "#999999",
    "cancelled": "#CC6600",
}

FONT_NORMAL = ("Arial", 9)
FONT_RUNNING = ("Arial", 9, "bold")


class StepTracker(ttk.Frame):
    """Визуальный трекер шагов pipeline"""

    def __init__(self, parent, on_restart_requested: Optional[Callable[[str], None]] = None):
        super().__init__(parent)
        self._step_ids: List[str] = []
        self._labels: dict = {}
        self._time_labels: dict = {}
        self._on_restart_requested = on_restart_requested
        self._create_ui()

    def _create_ui(self):
        ttk.Label(self, text="Шаги pipeline", style="Subtitle.TLabel").pack(anchor="w")

        canvas = tk.Canvas(self, height=180)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self._steps_frame = ttk.Frame(canvas)

        self._steps_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self._steps_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def set_steps(self, step_ids: List[str]):
        """Устанавливает список шагов. Все отображаются как pending."""
        for widget in self._steps_frame.winfo_children():
            widget.destroy()
        self._labels.clear()
        self._time_labels.clear()
        self._step_ids = list(step_ids)

        for step_id in step_ids:
            row = ttk.Frame(self._steps_frame)
            row.pack(fill="x", pady=1)

            icon = STATUS_ICONS["pending"]
            label = tk.Label(
                row, text=f"{icon} {step_id}",
                font=FONT_NORMAL, fg=STATUS_COLORS["pending"], anchor="w"
            )
            label.pack(side="left", padx=5)

            time_label = tk.Label(row, text="", fg="gray", font=FONT_NORMAL)
            time_label.pack(side="right", padx=5)

            self._bind_context_menu(row, step_id)
            self._bind_context_menu(label, step_id)
            self._bind_context_menu(time_label, step_id)

            self._labels[step_id] = label
            self._time_labels[step_id] = time_label

    def _bind_context_menu(self, widget, step_id: str):
        """Привязывает показ контекстного меню к виджету шага."""
        widget.bind(
            "<Button-3>",
            lambda event, current_step=step_id: self._show_context_menu(event, current_step)
        )
        widget.bind(
            "<Button-2>",
            lambda event, current_step=step_id: self._show_context_menu(event, current_step)
        )

    def _show_context_menu(self, event, step_id: str):
        """Показывает контекстное меню для шага."""
        context_menu = tk.Menu(self, tearoff=0)
        context_menu.add_command(
            label=f"Перезапустить шаг {step_id}",
            command=lambda current_step=step_id: self._restart_step(current_step)
        )

        try:
            context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            context_menu.grab_release()

    def _restart_step(self, step_id: str):
        """Запрашивает перезапуск указанного шага через callback панели."""
        if self._on_restart_requested:
            self._on_restart_requested(step_id)

    def update_step(self, step_id: str, status: str,
                    duration_sec: Optional[float] = None):
        """Обновляет статус шага (thread-safe через self.after)."""
        if step_id not in self._labels:
            return

        def _do_update():
            icon = STATUS_ICONS.get(status, "\u2753")  # ❓
            color = STATUS_COLORS.get(status, "#000000")
            font = FONT_RUNNING if status == "running" else FONT_NORMAL
            self._labels[step_id].config(
                text=f"{icon} {step_id}", fg=color, font=font
            )
            if duration_sec is not None:
                if duration_sec < 60:
                    text = f"{duration_sec:.1f}s"
                else:
                    m, s = divmod(int(duration_sec), 60)
                    text = f"{m}m {s}s"
                self._time_labels[step_id].config(text=text)
            elif status == "running":
                self._time_labels[step_id].config(text="...")

        self.after(0, _do_update)

    def reset(self):
        """Сбрасывает все шаги в pending."""
        for step_id in self._step_ids:
            self.update_step(step_id, "pending")

    def get_step_ids(self) -> List[str]:
        """Возвращает текущий список ID шагов."""
        return list(self._step_ids)
