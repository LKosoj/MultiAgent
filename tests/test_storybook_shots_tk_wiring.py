"""Подключение экрана «Кадры» в главное окно (без настоящего Tk)."""

import unittest
from unittest.mock import MagicMock

from tests.test_main_window_blockout_wiring import _import_main_window


class TestShotsWiring(unittest.TestCase):
    def setUp(self):
        self.module = _import_main_window()
        self.cls = self.module.MainWindow

    def test_status_goes_to_shots(self):
        win = self.cls.__new__(self.cls)
        win._last_brief = {"project": {"id": "p"}}
        for name in ("topbar", "movie", "shots"):
            setattr(win, name, MagicMock())
        win._available_caps = MagicMock(return_value=set())
        self.cls._update_topbar(win)
        win.shots.on_status.assert_called_once_with({"project": {"id": "p"}})

    def test_shutdown_stops_shots_before_bridge(self):
        win = self.cls.__new__(self.cls)
        order = []
        for name, method in (("poller", "stop"), ("shots", "shutdown"), ("bridge", "close"), ("toasts", "close")):
            obj = MagicMock()
            getattr(obj, method).side_effect = lambda n=name: order.append(n)
            setattr(win, name, obj)
        self.cls._shutdown_shell(win)
        self.assertLess(order.index("shots"), order.index("bridge"))


if __name__ == "__main__":
    unittest.main()
