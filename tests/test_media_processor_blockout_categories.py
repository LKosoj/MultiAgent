"""Раздел 18.5 ТЗ (docs/tz-blockout-reference-pipeline.md): категории медиа болванки
и флажок «показывать отдельные фреймы болванки».

Проверяет:
- MediaProcessor.get_project_images() находит blockout_ref/blockout_sheet всегда,
  а blockout_frame — только при include_blockout_frames=True (у файлов фреймов
  на реальном проекте порядок десятков тысяч, обход списка по умолчанию выключен).
- MediaProcessor.get_project_videos() находит blockout_video/blockout_preview/shot_video.
- media_panel.py: комбобокс фильтра содержит новые категории, есть чекбокс
  show_blockout_frames_var, refresh_media() читает его в главном потоке и
  передаёт в get_project_images().

Headless — tkinter не установлен в этом окружении. Образец подмены
sys.modules["tkinter"] — tests/test_storybook_manager_project_paths.py
(MediaProcessor) и tests/test_generation_panel_pipeline_config.py (панели GUI).
"""

import importlib
import logging
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

tk_mock = MagicMock()
sys.modules.setdefault("tkinter", tk_mock)

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "StoryBookManager"))

from StoryBookManager.config.settings import app_settings  # noqa: E402
from StoryBookManager.core.media_processor import MediaProcessor  # noqa: E402
import config.settings as _legacy_config_settings  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_backup_dir(tmp_path, monkeypatch):
    """Изолирует backup_directory: MediaProcessor.__init__ создаёт
    <backup_directory>/media_cache/<project_id>. Образец —
    tests/test_storybook_manager_project_paths.py::_isolated_backup_dir."""
    backups = tmp_path / "isolated_backups"
    for settings_obj in (app_settings, _legacy_config_settings.app_settings):
        monkeypatch.setattr(settings_obj, "get_backup_directory", lambda: backups)


def _make_processor(tmp_path, monkeypatch, project_id="proj1"):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path / "projects"))
    processor = MediaProcessor(project_id)
    return processor, processor.project_path


# ---------------------------------------------------------------------------
# MediaProcessor.get_project_images / get_project_videos
# ---------------------------------------------------------------------------

def test_get_project_images_finds_blockout_ref_without_frames_flag(tmp_path, monkeypatch):
    processor, project_path = _make_processor(tmp_path, monkeypatch)
    blockout_dir = project_path / "93_blockout" / "scene_01_shot_01"
    blockout_dir.mkdir(parents=True)
    (blockout_dir / "ref_start.png").write_bytes(b"png")
    (blockout_dir / "ref_end.png").write_bytes(b"png")
    frames_dir = blockout_dir / "frames"
    frames_dir.mkdir()
    (frames_dir / "0001.png").write_bytes(b"png")

    images = processor.get_project_images()

    categories = {img["category"] for img in images}
    assert "blockout_ref" in categories
    assert "blockout_frame" not in categories
    assert sum(1 for img in images if img["category"] == "blockout_ref") == 2


def test_get_project_images_includes_frames_only_when_flag_true(tmp_path, monkeypatch):
    processor, project_path = _make_processor(tmp_path, monkeypatch)
    blockout_dir = project_path / "93_blockout" / "scene_01_shot_01"
    frames_dir = blockout_dir / "frames"
    frames_dir.mkdir(parents=True)
    (frames_dir / "0001.png").write_bytes(b"png")
    (frames_dir / "0002.png").write_bytes(b"png")

    without_flag = processor.get_project_images(include_blockout_frames=False)
    with_flag = processor.get_project_images(include_blockout_frames=True)

    assert not any(img["category"] == "blockout_frame" for img in without_flag)
    frame_images = [img for img in with_flag if img["category"] == "blockout_frame"]
    assert len(frame_images) == 2


def test_get_project_images_logs_skipped_blockout_dirs_when_frames_flag_false(tmp_path, monkeypatch, caplog):
    """Критерий A15 (раздел 22 ТЗ): когда фреймы не сканируются, в журнал
    уровнем info уходит запись с перечнем пропущенных каталогов и
    затраченным временем — по ней и проверяется норматив (не более 2с на 54
    шотах), поскольку именно сканирование, а не показ, гасит флажок."""
    processor, project_path = _make_processor(tmp_path, monkeypatch)
    blockout_dir = project_path / "93_blockout" / "scene_01_shot_01"
    frames_dir = blockout_dir / "frames"
    frames_dir.mkdir(parents=True)
    (frames_dir / "0001.png").write_bytes(b"png")

    with caplog.at_level(logging.INFO, logger="StoryBookManager.core.media_processor"):
        processor.get_project_images(include_blockout_frames=False)

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    messages = [r.getMessage() for r in info_records]
    assert any(str(frames_dir) in msg for msg in messages), messages
    assert any("сканирование заняло" in msg for msg in messages), messages


def test_get_project_images_does_not_log_skip_notice_when_frames_included(tmp_path, monkeypatch, caplog):
    processor, project_path = _make_processor(tmp_path, monkeypatch)
    blockout_dir = project_path / "93_blockout" / "scene_01_shot_01"
    frames_dir = blockout_dir / "frames"
    frames_dir.mkdir(parents=True)
    (frames_dir / "0001.png").write_bytes(b"png")

    with caplog.at_level(logging.INFO, logger="StoryBookManager.core.media_processor"):
        processor.get_project_images(include_blockout_frames=True)

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert not any("сканирование заняло" in msg for msg in messages), messages


def test_get_project_images_finds_blockout_sheet(tmp_path, monkeypatch):
    processor, project_path = _make_processor(tmp_path, monkeypatch)
    preview_dir = project_path / "93_blockout" / "preview"
    preview_dir.mkdir(parents=True)
    (preview_dir / "contact_sheet_01.png").write_bytes(b"png")

    images = processor.get_project_images()

    sheet_images = [img for img in images if img["category"] == "blockout_sheet"]
    assert len(sheet_images) == 1
    assert sheet_images[0]["type"] == "blockout_sheet"


def test_get_project_images_without_blockout_dir_returns_only_regular_categories(tmp_path, monkeypatch):
    processor, project_path = _make_processor(tmp_path, monkeypatch)
    images_dir = project_path / "50_images" / "page_01"
    images_dir.mkdir(parents=True)
    (images_dir / "illustration.png").write_bytes(b"png")

    images = processor.get_project_images()

    assert len(images) == 1
    assert images[0]["category"] == "book_page"


def test_get_project_videos_finds_blockout_video_and_preview(tmp_path, monkeypatch):
    processor, project_path = _make_processor(tmp_path, monkeypatch)
    blockout_dir = project_path / "93_blockout" / "scene_01_shot_01"
    blockout_dir.mkdir(parents=True)
    (blockout_dir / "blockout_ref.mp4").write_bytes(b"mp4")
    preview_dir = project_path / "93_blockout" / "preview"
    preview_dir.mkdir(parents=True)
    (preview_dir / "reel.mp4").write_bytes(b"mp4")

    with patch.object(MediaProcessor, "_get_video_duration", return_value=1.0):
        videos = processor.get_project_videos()

    categories = {vid["category"] for vid in videos}
    assert categories == {"blockout_video", "blockout_preview"}
    preview_video = next(v for v in videos if v["category"] == "blockout_preview")
    assert preview_video["scene"] == "zz_preview"


def test_get_project_videos_shot_video_category(tmp_path, monkeypatch):
    processor, project_path = _make_processor(tmp_path, monkeypatch)
    shot_dir = project_path / "97_shots" / "scene_01"
    shot_dir.mkdir(parents=True)
    (shot_dir / "clip.mp4").write_bytes(b"mp4")

    with patch.object(MediaProcessor, "_get_video_duration", return_value=1.0):
        videos = processor.get_project_videos()

    assert len(videos) == 1
    assert videos[0]["category"] == "shot_video"


# ---------------------------------------------------------------------------
# media_panel.py: комбобокс фильтра + чекбокс + refresh_media()
# ---------------------------------------------------------------------------

class _FakeVar:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def trace_add(self, *args, **kwargs):
        return None


class _FakeWidget:
    """Mock-виджет Tk/ttk с явным списком методов.

    Без __getattr__-заглушки: MediaPanel наследует ttk.Frame (= этот класс в
    фейковом модуле), и hasattr(panel, 'show_blockout_frames_var') в
    refresh_media() должен возвращать False для непроставленных атрибутов —
    catch-all __getattr__ ловил бы и это имя тоже, ломая проверку."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.options = {}

    def pack(self, *args, **kwargs):
        return self

    def grid(self, *args, **kwargs):
        return self

    def grid_rowconfigure(self, *args, **kwargs):
        return None

    def grid_columnconfigure(self, *args, **kwargs):
        return None

    def config(self, **kwargs):
        self.options.update(kwargs)

    configure = config

    def __setitem__(self, key, value):
        self.options[key] = value

    def __getitem__(self, key):
        return self.options[key]

    def bind(self, *args, **kwargs):
        return None

    def heading(self, *args, **kwargs):
        return None

    def column(self, *args, **kwargs):
        return None

    def yview(self, *args, **kwargs):
        return None

    def xview(self, *args, **kwargs):
        return None

    def insert(self, *args, **kwargs):
        return None

    def delete(self, *args, **kwargs):
        return None

    def get_children(self):
        return []

    def winfo_children(self):
        return []

    def set(self, *args, **kwargs):
        return None


def _import_media_panel():
    """Свежий импорт media_panel с фейковым tkinter (headless).

    Образец: tests/test_generation_panel_pipeline_config.py::_import_generation_panel.
    """
    sys.modules.pop("StoryBookManager.gui.media_panel", None)

    tk_module = types.ModuleType("tkinter")
    ttk_module = types.ModuleType("tkinter.ttk")
    messagebox_module = types.ModuleType("tkinter.messagebox")

    tk_module.StringVar = _FakeVar
    tk_module.BooleanVar = _FakeVar
    tk_module.Canvas = _FakeWidget
    tk_module.Widget = _FakeWidget
    tk_module.ttk = ttk_module
    tk_module.messagebox = messagebox_module

    for name in ("Frame", "LabelFrame", "Label", "Button", "Combobox", "Checkbutton",
                 "Treeview", "Scrollbar", "Separator"):
        setattr(ttk_module, name, _FakeWidget)

    messagebox_module.showerror = lambda *args, **kwargs: None
    messagebox_module.showwarning = lambda *args, **kwargs: None
    messagebox_module.askyesno = lambda *args, **kwargs: True

    project_manager_module = types.ModuleType("StoryBookManager.core.project_manager")
    project_manager_module.Project = object

    media_processor_module = types.ModuleType("StoryBookManager.core.media_processor")
    media_processor_module.MediaProcessor = MagicMock

    scroll_utils_module = types.ModuleType("StoryBookManager.utils.scroll_utils")
    scroll_utils_module.bind_mousewheel_to_treeview = lambda *args, **kwargs: None
    scroll_utils_module.bind_mousewheel_to_text_with_scrollbar = lambda *args, **kwargs: None
    scroll_utils_module.bind_mousewheel_to_canvas_frame = lambda *args, **kwargs: None
    scroll_utils_module.bind_mousewheel_to_canvas_frame_advanced = lambda *args, **kwargs: None
    scroll_utils_module.bind_mousewheel_to_canvas_frame_ultimate = lambda *args, **kwargs: None

    with patch.dict(
        sys.modules,
        {
            "tkinter": tk_module,
            "tkinter.ttk": ttk_module,
            "tkinter.messagebox": messagebox_module,
            "StoryBookManager.core.project_manager": project_manager_module,
            "StoryBookManager.core.media_processor": media_processor_module,
            "StoryBookManager.utils.scroll_utils": scroll_utils_module,
        },
    ):
        return importlib.import_module("StoryBookManager.gui.media_panel")


def test_create_media_tree_filter_combo_lists_blockout_categories():
    module = _import_media_panel()
    MediaPanel = module.MediaPanel
    panel = MediaPanel.__new__(MediaPanel)

    combo_instances = []

    class TrackingCombobox(_FakeWidget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            combo_instances.append(self)

    with patch.object(module.ttk, "Combobox", TrackingCombobox):
        MediaPanel.create_media_tree(panel, _FakeWidget())

    assert len(combo_instances) == 1
    values = combo_instances[0].options["values"]
    for category in (
        "blockout_ref", "blockout_frame", "blockout_sheet",
        "blockout_video", "blockout_preview", "shot_video",
    ):
        assert category in values

    assert hasattr(panel, "show_blockout_frames_var")
    assert panel.show_blockout_frames_var.get() is False


def test_refresh_media_passes_checkbox_state_to_get_project_images():
    module = _import_media_panel()
    MediaPanel = module.MediaPanel
    panel = MediaPanel.__new__(MediaPanel)
    panel.media_processor = MagicMock()
    panel.media_processor.get_project_images.return_value = []
    panel.media_processor.get_project_videos.return_value = []
    panel.media_tree = _FakeWidget()
    panel._refresh_in_progress = False
    panel.show_blockout_frames_var = _FakeVar(True)
    panel.after = lambda delay, callback: callback()

    with patch.object(module.threading, "Thread") as thread_cls:
        def _run_target(target=None, daemon=None):
            thread = MagicMock()
            thread.start.side_effect = target
            return thread

        thread_cls.side_effect = _run_target
        MediaPanel.refresh_media(panel)

    panel.media_processor.get_project_images.assert_called_once_with(True)


def test_refresh_media_defaults_to_false_without_checkbox_attribute():
    module = _import_media_panel()
    MediaPanel = module.MediaPanel
    panel = MediaPanel.__new__(MediaPanel)
    panel.media_processor = MagicMock()
    panel.media_processor.get_project_images.return_value = []
    panel.media_processor.get_project_videos.return_value = []
    panel.media_tree = _FakeWidget()
    panel._refresh_in_progress = False
    panel.after = lambda delay, callback: callback()

    with patch.object(module.threading, "Thread") as thread_cls:
        def _run_target(target=None, daemon=None):
            thread = MagicMock()
            thread.start.side_effect = target
            return thread

        thread_cls.side_effect = _run_target
        MediaPanel.refresh_media(panel)

    panel.media_processor.get_project_images.assert_called_once_with(False)
