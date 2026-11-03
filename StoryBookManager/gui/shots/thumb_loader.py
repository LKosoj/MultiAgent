"""Загрузка миниатюр в потоках: PIL-объекты готовят воркеры, PhotoImage строит только главный поток.

Воркеры tkinter не трогают. Результаты лежат в queue.Queue, главный поток забирает их
по after(30) и сам превращает в PhotoImage.
"""

import logging
import queue
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from StoryBookManager.gui.shots.thumb_queue import LruCache, ThumbRequest, ThumbScheduler

logger = logging.getLogger(__name__)

POLL_MS = 30
LIST_CACHE_SIZE = 400   # PhotoImage размера 128: около 15 МБ
CARD_CACHE_SIZE = 16    # изображения размера 512 для карточки
VIDEO_EXT = (".mp4", ".webm", ".mov")


def load_pil(project_id: str, request: ThumbRequest):
    """Воркер: миниатюра из кэша studio в PIL-объект (None - не получилось)."""
    from PIL import Image

    from custom_tools.storybook.studio.thumbs import make_thumb

    result, placeholder = make_thumb(project_id, request.rel, request.size)
    if placeholder:
        return None
    with Image.open(result) as image:
        image.load()
        pil = image.copy()
    if request.fit:
        pil.thumbnail(request.fit)
    return pil


class ThumbLoader:
    """Планировщик + пулы потоков + кэш PhotoImage. Создаётся и опрашивается в главном потоке."""

    def __init__(self, widget, get_project_id: Callable[[], Optional[str]],
                 on_ready: Callable[[ThumbRequest, object], None],
                 worker: Callable = load_pil, photo_factory: Optional[Callable] = None):
        self._widget = widget
        self._get_project_id = get_project_id
        self._on_ready = on_ready
        self._worker = worker
        self._photo_factory = photo_factory or self._default_photo
        self.scheduler = ThumbScheduler()
        self.cache = LruCache(LIST_CACHE_SIZE)
        self.card_cache = LruCache(CARD_CACHE_SIZE)
        self._images = ThreadPoolExecutor(max_workers=2, thread_name_prefix="shots-thumb")
        self._videos = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shots-poster")
        self._results: "queue.Queue" = queue.Queue()
        self._polling = False
        self._closed = False
        self._project_id: Optional[str] = None

    @staticmethod
    def _default_photo(pil):
        from PIL import ImageTk
        return ImageTk.PhotoImage(pil)

    def cache_for(self, request: ThumbRequest) -> LruCache:
        return self.card_cache if request.size >= 512 else self.cache

    def cached(self, request: ThumbRequest):
        """Готовая картинка (PhotoImage) или None. «Нет картинки» хранится как False."""
        return self.cache_for(request).get(request.cache_key)

    def request(self, wanted) -> None:
        """Заменить набор нужных картинок и запустить недостающие."""
        if self._closed:
            return
        project_id = self._get_project_id()
        if project_id != self._project_id:
            self._project_id = project_id
            self.cache.clear()
            self.card_cache.clear()
        todo = [r for r in wanted if self.cached(r) is None]
        self.scheduler.set_wanted(todo)
        self._launch()

    def _launch(self) -> None:
        for request in self.scheduler.pop(8 - self.scheduler.in_flight_count()):
            pool = self._videos if request.rel.lower().endswith(VIDEO_EXT) else self._images
            pool.submit(self._run, self._project_id, request)
        self._start_poll()

    def _run(self, project_id, request: ThumbRequest) -> None:
        pil = None
        cancelled = False
        try:
            if self.scheduler.still_wanted(request) and project_id:
                pil = self._worker(project_id, request)
            else:
                cancelled = True   # запрос отменён до начала: в кэш ничего не кладём
        except Exception:
            logger.debug("Миниатюра %s не получилась", request.rel, exc_info=True)
        self._results.put((request, pil, project_id, cancelled))

    def _start_poll(self) -> None:
        if self._polling or self._closed:
            return
        self._polling = True
        try:
            self._widget.after(POLL_MS, self._poll)
        except Exception:
            self._polling = False

    def _poll(self) -> None:
        self._polling = False
        if self._closed:
            return
        while True:
            try:
                request, pil, project_id, cancelled = self._results.get_nowait()
            except queue.Empty:
                break
            self.scheduler.done(request)
            if cancelled or project_id != self._project_id:
                continue
            photo = None
            if pil is not None:
                try:
                    photo = self._photo_factory(pil)  # только здесь, в главном потоке
                except Exception:
                    logger.debug("PhotoImage не создан", exc_info=True)
            self.cache_for(request).put(request.cache_key, photo if photo is not None else False)
            try:
                self._on_ready(request, photo)
            except Exception:
                logger.exception("Обработчик миниатюры упал")
        self._launch()
        if self.scheduler.pending_count() or self.scheduler.in_flight_count() or not self._results.empty():
            self._start_poll()

    def invalidate(self, rel: str) -> None:
        """Файл перегенерирован: старые записи этого пути больше не нужны."""
        for cache in (self.cache, self.card_cache):
            cache.discard_where(lambda k, r=rel: k[0] == r)

    def close(self) -> None:
        self._closed = True
        self.scheduler.clear()
        self._images.shutdown(wait=False, cancel_futures=True)
        self._videos.shutdown(wait=False, cancel_futures=True)
