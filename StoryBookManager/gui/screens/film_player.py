"""Встроенный плеер «Фильма» на OpenCV (кадры из файла, без звука)."""

import threading
import tkinter as tk
from tkinter import ttk

from StoryBookManager.gui.screens.film_model import clamp_time, format_position, time_from_x
from StoryBookManager.gui.screens.film_texts import FILM_TEXTS as T
from StoryBookManager.gui.theme_tokens import COLORS, PAD_INLINE, px

TICK_MS = 40


class FilmPlayer(ttk.Frame):
    """Кадры из файла: пуск/пауза, перемотка, позиция. Звук играет внешний плеер («Смотреть со звуком»)."""

    def __init__(self, parent, on_time=None, on_external=None):
        super().__init__(parent, style="Panel.TFrame")
        self.on_time = on_time
        self.cap = None
        self.duration = 0.0
        self.fps = 25.0
        self.time = 0.0
        self.playing = False
        self.size = (0, 0)
        self._job = None
        self._photo = None
        self._frame = None
        self._open_token = 0
        self._open_job = None
        self.canvas = tk.Canvas(self, background="#000000", highlightthickness=0, height=px(260))
        self.canvas.bind("<Configure>", lambda e: self._draw())
        bar = ttk.Frame(self, style="Panel.TFrame")
        # элементы управления упаковываются снизу первыми: при нехватке места сжимается только кадр
        self.play_btn = ttk.Button(bar, text=T["play"], command=self.toggle)
        self.play_btn.pack(side="left")
        self.pos_var = tk.StringVar(value=format_position(0, 0))
        ttk.Label(bar, textvariable=self.pos_var).pack(side="left", padx=px(PAD_INLINE))
        # подпись упаковывается первой, чтобы при нехватке места не пропадала
        ttk.Label(bar, text="Без звука", style="Caption.TLabel").pack(side="right", padx=px(PAD_INLINE))
        self.sound_btn = ttk.Button(bar, text="Смотреть со звуком", command=lambda: on_external and on_external())
        self.sound_btn.pack(side="right")
        self.error_var = tk.StringVar()
        err_label = ttk.Label(self, textvariable=self.error_var, foreground=COLORS["error"])
        self.scrub = ttk.Scale(self, from_=0, to=1, command=self._on_scrub)
        self._scrubbing = False
        self.scrub.pack(side="bottom", fill="x")
        err_label.pack(side="bottom", anchor="w")
        bar.pack(side="bottom", fill="x", pady=(px(PAD_INLINE), 0))
        self.canvas.pack(side="top", fill="both", expand=True)

    def load(self, path, duration_hint=None, on_loaded=None):
        """Открыть файл. Открытие идёт в потоке (на сетевом диске оно долгое), кадры рисуем только в главном потоке.

        on_loaded(размер) вызывается из главного потока: (ширина, высота) или None, если файл не открылся."""
        self.close()
        self.error_var.set("")
        self._open_token += 1
        token = self._open_token
        box = {}

        def work():
            try:
                import cv2

                cap = cv2.VideoCapture(str(path))
                if not cap.isOpened():
                    raise OSError("не открывается")
                if self._open_token != token:
                    # пока открывали, файл сменили или экран закрыли: освобождаем захват здесь же, без Tk
                    cap.release()
                    box["cap"] = None
                    return
                box["cap"] = cap
            except Exception:
                box["cap"] = None

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self._poll_open(token, thread, box, duration_hint, on_loaded)

    def _poll_open(self, token, thread, box, duration_hint, on_loaded):
        if token != self._open_token:
            # пока открывали, файл сменили или экран закрыли: освобождаем лишнее
            if not thread.is_alive() and box.get("cap") is not None:
                box["cap"].release()
            elif thread.is_alive():
                self._open_job = self.after(100, lambda: self._poll_open(token, thread, box, duration_hint, on_loaded))
            return
        if thread.is_alive():
            self._open_job = self.after(30, lambda: self._poll_open(token, thread, box, duration_hint, on_loaded))
            return
        self._open_job = None
        cap = box.get("cap")
        if cap is None:
            self.size = (0, 0)
            self.error_var.set(T["video_failed"])
            if on_loaded:
                on_loaded(None)
            return
        import cv2

        self.cap = cap
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        self.duration = float(duration_hint or (frames / self.fps if self.fps else 0))
        self.size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        self.scrub.configure(to=max(self.duration, 0.1))
        self.seek(0)
        if on_loaded:
            on_loaded(self.size)

    def close(self):
        self._open_token += 1
        if self._open_job is not None:
            try:
                self.after_cancel(self._open_job)
            except tk.TclError:
                pass
            self._open_job = None
        self._frame = None
        self.pause()
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        self.cap = None
        self._photo = None

    def toggle(self):
        self.pause() if self.playing else self.play()

    def play(self):
        if self.cap is None:
            return
        if self.time >= self.duration:
            self.seek(0)
        self.playing = True
        self.play_btn.configure(text=T["pause"])
        self._tick()

    def pause(self):
        self.playing = False
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        try:
            self.play_btn.configure(text=T["play"])
        except tk.TclError:
            pass

    def _tick(self):
        self._job = None
        if not self.playing or self.cap is None:
            return
        ok, frame = self.cap.read()
        if not ok:
            self.pause()
            return
        self.time = min(self.duration, self.time + 1.0 / self.fps)
        self._show(frame)
        self._report()
        self._job = self.after(int(1000 / self.fps) if self.fps else TICK_MS, self._tick)

    def seek(self, t):
        self.time = clamp_time(t, self.duration)
        if self.cap is not None:
            import cv2

            self.cap.set(cv2.CAP_PROP_POS_MSEC, self.time * 1000)
            ok, frame = self.cap.read()
            if ok:
                self._show(frame)
                self.cap.set(cv2.CAP_PROP_POS_MSEC, self.time * 1000)
        self._report()

    def seek_by(self, delta):
        self.seek(self.time + delta)

    def _report(self):
        self.pos_var.set(format_position(self.time, self.duration))
        self._scrubbing = True
        try:
            self.scrub.set(self.time)
        finally:
            self._scrubbing = False
        if self.on_time:
            self.on_time(self.time)

    def _on_scrub(self, value):
        if not self._scrubbing and self.cap is not None:
            self.seek(float(value))

    def _show(self, frame):
        self._frame = frame
        self._draw()

    def _draw(self):
        frame = getattr(self, "_frame", None)
        if frame is None:
            return
        try:
            import cv2
            from PIL import Image, ImageTk

            w, h = max(self.canvas.winfo_width(), 2), max(self.canvas.winfo_height(), 2)
            fh, fw = frame.shape[:2]
            scale = min(w / fw, h / fh)
            resized = cv2.resize(frame, (max(1, int(fw * scale)), max(1, int(fh * scale))))
            image = Image.fromarray(cv2.cvtColor(resized, cv2.COLOR_BGR2RGB))
            self._photo = ImageTk.PhotoImage(image)
            self.canvas.delete("all")
            self.canvas.create_image(w // 2, h // 2, image=self._photo)
        except Exception:
            self.error_var.set(T["video_failed"])

    def destroy(self):
        self.close()
        super().destroy()
