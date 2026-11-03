"""Адаптеры разделов: оболочка не знает внутренностей панелей, только пять действий."""

import logging
from typing import Callable, Optional

from StoryBookManager.gui.shell.run_status import RunView
from StoryBookManager.gui.shell.widgets import WrapRow

logger = logging.getLogger(__name__)


class SectionAdapter:
    """Протокол раздела. По умолчанию все действия ничего не делают."""

    @property
    def widget(self):
        return self

    def on_show(self) -> None:
        pass

    def on_refresh(self) -> None:
        pass

    def on_save(self) -> None:
        pass

    def can_leave(self) -> bool:
        return True

    def on_project_changed(self, project) -> None:
        pass


class PanelAdapter(SectionAdapter):
    """Существующая панель в разделе: действия собираются из функций."""

    def __init__(self, panel, *, show: Optional[Callable] = None, refresh: Optional[Callable] = None,
                 save: Optional[Callable] = None, can_leave: Optional[Callable[[], bool]] = None,
                 project_changed: Optional[Callable] = None):
        self._panel = panel
        self._show = show
        self._refresh = refresh
        self._save = save
        self._can_leave = can_leave
        self._project_changed = project_changed

    @property
    def widget(self):
        return self._panel

    def on_show(self) -> None:
        self._call(self._show)

    def on_refresh(self) -> None:
        self._call(self._refresh)

    def on_save(self) -> None:
        self._call(self._save)

    def can_leave(self) -> bool:
        if self._can_leave is None:
            return True
        try:
            return bool(self._can_leave())
        except Exception:
            logger.exception("can_leave раздела упал")
            return True

    def on_project_changed(self, project) -> None:
        self._call(self._project_changed, project)

    @staticmethod
    def _call(func, *args) -> None:
        if func is None:
            return
        try:
            func(*args)
        except Exception:
            logger.exception("Действие раздела завершилось ошибкой")
