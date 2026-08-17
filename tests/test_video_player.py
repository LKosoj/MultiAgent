"""Раздел 18.4 ТЗ (docs/tz-blockout-reference-pipeline.md): «вынос плеера в
общий класс» — StoryBookManager/gui/video_player.py.

До этого воспроизведение видео было прибито к единственному экземпляру
MediaPanel; VideoPlayer выносит его состояние (video_capture, is_playing,
is_paused, ...) в отдельный класс, чтобы у вкладки «Болванка» было два
независимых плеера одновременно. Проверяет ту часть поведения, что не
требует реального OpenCV-декодирования кадров (сам _playback_loop/
_update_frame не тестируются здесь — это I/O-цикл с реальным видеофайлом):
- load() не останавливает уже идущее воспроизведение;
- play() ведёт себя предсказуемо при отсутствующих зависимостях/пути;
- pause()/stop() переключают состояние и текст кнопки независимо на двух
  разных экземплярах (это и есть цель выноса — раньше было on self панели).

Headless — tkinter не установлен в этом окружении (образец подмены
sys.modules["tkinter"]: tests/test_generation_panel_pipeline_config.py).
"""

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class FakeWidget:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.options = dict(kwargs)
        self.packed = True

    def pack(self, *args, **kwargs):
        self.packed = True
        return self

    def pack_forget(self):
        self.packed = False

    def config(self, **kwargs):
        self.options.update(kwargs)

    configure = config

    def winfo_width(self):
        return 200

    def winfo_height(self):
        return 150

    def delete(self, *args, **kwargs):
        return None

    def create_image(self, *args, **kwargs):
        return None

    def after(self, delay, callback, *args, **kwargs):
        callback(*args, **kwargs)


def _import_video_player():
    sys.modules.pop("StoryBookManager.gui.video_player", None)

    tk_module = types.ModuleType("tkinter")
    ttk_module = types.ModuleType("tkinter.ttk")
    messagebox_module = types.ModuleType("tkinter.messagebox")

    tk_module.Canvas = FakeWidget
    tk_module.Widget = FakeWidget
    tk_module.ttk = ttk_module
    tk_module.messagebox = messagebox_module
    ttk_module.Button = FakeWidget

    messagebox_module.showerror = MagicMock()
    messagebox_module.showwarning = MagicMock()

    with patch.dict(
        sys.modules,
        {
            "tkinter": tk_module,
            "tkinter.ttk": ttk_module,
            "tkinter.messagebox": messagebox_module,
        },
    ):
        return importlib.import_module("StoryBookManager.gui.video_player")


class TestVideoPlayer(unittest.TestCase):
    def setUp(self):
        self.module = _import_video_player()
        self.module.messagebox.showerror.reset_mock()
        self.module.messagebox.showwarning.reset_mock()

    def _make_player(self):
        canvas = FakeWidget()
        controls = FakeWidget()
        return self.module.VideoPlayer(canvas, controls)

    def test_load_sets_path_without_touching_playback_state(self):
        player = self._make_player()
        player.is_playing = True
        player.load("/tmp/some_video.mp4")
        self.assertEqual(player.video_path, "/tmp/some_video.mp4")
        self.assertTrue(player.is_playing)  # load() сам ничего не останавливает

    def test_play_shows_error_when_opencv_or_pil_missing(self):
        player = self._make_player()
        player.load("/tmp/some_video.mp4")
        with patch.object(self.module, "HAS_OPENCV", False), \
             patch.object(self.module, "HAS_PIL", True):
            player.play()
        self.module.messagebox.showerror.assert_called_once()
        self.assertFalse(player.is_playing)

    def test_play_warns_when_no_video_path_selected(self):
        player = self._make_player()
        with patch.object(self.module, "HAS_OPENCV", True), \
             patch.object(self.module, "HAS_PIL", True):
            player.play()
        self.module.messagebox.showwarning.assert_called_once()
        self.assertFalse(player.is_playing)

    def test_pause_toggles_state_and_button_label(self):
        player = self._make_player()
        player.is_playing = True

        player.pause()
        self.assertTrue(player.is_paused)
        self.assertEqual(player.pause_btn.options["text"], "▶️ Продолжить")

        player.pause()
        self.assertFalse(player.is_paused)
        self.assertEqual(player.pause_btn.options["text"], "⏸️ Пауза")

    def test_pause_noop_when_not_playing(self):
        player = self._make_player()
        player.is_playing = False
        player.pause()
        self.assertFalse(player.is_paused)

    def test_stop_releases_capture_and_resets_flags(self):
        player = self._make_player()
        fake_capture = MagicMock()
        player.video_capture = fake_capture
        player.is_playing = True
        player.is_paused = True
        player.video_thread = None

        player.stop()

        fake_capture.release.assert_called_once()
        self.assertIsNone(player.video_capture)
        self.assertFalse(player.is_playing)
        self.assertFalse(player.is_paused)

    def test_two_independent_players_do_not_share_state(self):
        """Раздел 18.4: два одновременных плеера не должны задевать друг
        друга — именно ради этого состояние вынесено из MediaPanel."""
        player_a = self._make_player()
        player_b = self._make_player()

        player_a.is_playing = True
        player_a.is_paused = False
        player_a.pause()

        self.assertTrue(player_a.is_paused)
        self.assertFalse(player_b.is_paused)

        player_a.video_capture = MagicMock(name="capture_a")
        player_b.video_capture = MagicMock(name="capture_b")
        self.assertIsNot(player_a.video_capture, player_b.video_capture)

    def test_show_and_hide_controls(self):
        player = self._make_player()
        player.hide_controls()
        self.assertFalse(player.play_btn.packed)
        self.assertFalse(player.pause_btn.packed)
        self.assertFalse(player.stop_btn.packed)

        player.show_controls()
        self.assertTrue(player.play_btn.packed)
        self.assertTrue(player.pause_btn.packed)
        self.assertTrue(player.stop_btn.packed)


if __name__ == "__main__":
    unittest.main()
