"""
Встроенный видеоплеер (вынесен из media_panel.py, раздел 18.4 ТЗ)
===================================================================

До этого воспроизведение видео (OpenCV-декодирование в фоновом потоке +
отрисовка кадров в Tkinter Canvas) было прибито к единственному экземпляру
MediaPanel — состояние (video_capture, video_thread, is_playing и т.д.)
хранилось прямо на self панели. Вкладке «Болванка» нужны ОДНОВРЕМЕННО два
независимых плеера (финальное видео и опорный ролик болванки), поэтому
состояние и кнопки управления вынесены в отдельный класс: у каждого
экземпляра VideoPlayer — своя копия всего перечисленного, и play/pause/stop
одного не задевают другой.
"""

import logging
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional

try:
    import cv2
    HAS_OPENCV = True
except ImportError:
    HAS_OPENCV = False
    cv2 = None

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    Image = None
    ImageTk = None

logger = logging.getLogger(__name__)


class VideoPlayer:
    """Встроенный видеоплеер: canvas для кадров + кнопки play/pause/stop.

    canvas передаётся снаружи (может быть общим с показом статичных
    изображений, как в media_panel.py, либо выделенным под конкретный плеер,
    как в blockout_panel.py) — VideoPlayer его не создаёт, только рисует в нём.
    """

    def __init__(self, canvas: tk.Canvas, controls_parent: tk.Widget, *, visible: bool = True):
        self.canvas = canvas
        self.video_path: Optional[str] = None
        self.video_capture = None
        self.is_playing = False
        self.is_paused = False
        self.video_thread: Optional[threading.Thread] = None
        self.fps = 30
        self._stop_playback = threading.Event()
        self.current_photo = None

        self.play_btn: Optional[ttk.Button] = None
        self.pause_btn: Optional[ttk.Button] = None
        self.stop_btn: Optional[ttk.Button] = None
        self._create_controls(controls_parent)
        if not visible:
            self.hide_controls()

    def _create_controls(self, parent: tk.Widget):
        self.play_btn = ttk.Button(parent, text="▶️ Воспроизвести", command=self.play)
        self.play_btn.pack(side="left", padx=5)

        self.pause_btn = ttk.Button(parent, text="⏸️ Пауза", command=self.pause)
        self.pause_btn.pack(side="left", padx=2)

        self.stop_btn = ttk.Button(parent, text="⏹️ Стоп", command=self.stop)
        self.stop_btn.pack(side="left", padx=2)

    def show_controls(self):
        """Показывает ранее скрытые кнопки управления (например, при обнаружении видео)."""
        if self.play_btn:
            self.play_btn.pack(side="left", padx=5)
        if self.pause_btn:
            self.pause_btn.pack(side="left", padx=2)
        if self.stop_btn:
            self.stop_btn.pack(side="left", padx=2)

    def hide_controls(self):
        for btn in (self.play_btn, self.pause_btn, self.stop_btn):
            if btn:
                btn.pack_forget()

    def load(self, video_path: str):
        """Задаёт путь к видео для последующего play().

        Не останавливает уже идущее воспроизведение — этим, как и раньше,
        занимается play() перед открытием нового видео.
        """
        self.video_path = video_path

    def play(self):
        """Запускает встроенное воспроизведение self.video_path."""
        if not HAS_OPENCV or not HAS_PIL:
            missing = []
            if not HAS_OPENCV:
                missing.append("OpenCV")
            if not HAS_PIL:
                missing.append("Pillow")
            messagebox.showerror("Ошибка", f"Не установлены библиотеки: {', '.join(missing)}.\nИспользуйте внешний плеер.")
            return

        if not self.video_path:
            messagebox.showwarning("Предупреждение", "Видео файл не выбран")
            return

        try:
            # Останавливаем текущее воспроизведение если есть
            self.stop()

            # Открываем видео
            self.video_capture = cv2.VideoCapture(self.video_path)

            if not self.video_capture.isOpened():
                messagebox.showerror("Ошибка", "Не удалось открыть видео файл")
                return

            # Получаем FPS видео
            self.fps = self.video_capture.get(cv2.CAP_PROP_FPS)
            if self.fps <= 0:
                self.fps = 30  # По умолчанию

            self.is_playing = True
            self.is_paused = False

            self.video_thread = threading.Thread(target=self._playback_loop, daemon=True)
            self.video_thread.start()

            logger.info(f"Начато встроенное воспроизведение видео: {self.video_path}")

        except Exception as e:
            logger.error(f"Ошибка запуска встроенного видеоплеера: {e}")
            messagebox.showerror("Ошибка", f"Не удалось запустить видео:\n{e}")

    def pause(self):
        """Пауза/возобновление воспроизведения"""
        if self.is_playing:
            self.is_paused = not self.is_paused
            if self.pause_btn:
                self.pause_btn.config(text="▶️ Продолжить" if self.is_paused else "⏸️ Пауза")
            if self.is_paused:
                logger.info("Видео поставлено на паузу")
            else:
                logger.info("Воспроизведение видео возобновлено")

    def stop(self):
        """Остановка воспроизведения"""
        if self.is_playing:
            self.is_playing = False
            self.is_paused = False
            self._stop_playback.set()

            if self.video_thread and self.video_thread.is_alive():
                self.video_thread.join(timeout=1.0)

            self._stop_playback.clear()

            if self.video_capture:
                self.video_capture.release()
                self.video_capture = None

            if self.pause_btn:
                self.pause_btn.config(text="⏸️ Пауза")

            logger.info("Воспроизведение видео остановлено")

    def _playback_loop(self):
        """Основной цикл воспроизведения видео"""
        frame_delay = 1.0 / self.fps

        while self.is_playing and self.video_capture and not self._stop_playback.is_set():
            if not self.is_paused:
                ret, frame = self.video_capture.read()

                if not ret:
                    # Конец видео
                    self.is_playing = False
                    self.is_paused = False
                    break

                try:
                    canvas_width = self.canvas.winfo_width()
                    canvas_height = self.canvas.winfo_height()

                    if canvas_width > 1 and canvas_height > 1:
                        height, width = frame.shape[:2]

                        scale_x = canvas_width / width
                        scale_y = canvas_height / height
                        scale = min(scale_x, scale_y)

                        new_width = int(width * scale * 0.9)  # Небольшой отступ
                        new_height = int(height * scale * 0.9)

                        frame = cv2.resize(frame, (new_width, new_height))
                        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        pil_image = Image.fromarray(frame_rgb)

                        # Обновляем canvas в главном потоке (PhotoImage создаётся там)
                        self.canvas.after(0, self._update_frame, pil_image)

                except Exception as e:
                    logger.error(f"Ошибка обработки кадра видео: {e}")
                    break

            time.sleep(frame_delay)

        # Освобождаем ресурсы только если stop() не сделал это сам
        if self.video_capture and not self._stop_playback.is_set():
            self.video_capture.release()
            self.video_capture = None

    def _update_frame(self, pil_image):
        """Обновление кадра видео в canvas (вызывается в главном потоке)"""
        try:
            photo = ImageTk.PhotoImage(pil_image)
            # Сохраняем ссылку на фото чтобы оно не удалилось сборщиком мусора
            self.current_photo = photo

            self.canvas.delete("all")

            canvas_width = self.canvas.winfo_width()
            canvas_height = self.canvas.winfo_height()

            x = canvas_width // 2
            y = canvas_height // 2

            self.canvas.create_image(x, y, image=photo, anchor="center", tags="video_frame")

        except Exception as e:
            logger.error(f"Ошибка обновления кадра видео: {e}")
