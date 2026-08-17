"""Раздел 18.4 ТЗ (docs/tz-blockout-reference-pipeline.md): вкладка «Болванка».

Проверяет чистые функции модуля StoryBookManager/gui/blockout_panel.py (хеш
спецификации, разбор/форматирование длительности, правило актуальности
manifest.json, состояние шота, P10, список файлов на удаление, атомарная
запись manifest.json) и часть поведения класса BlockoutPanel, которую можно
проверить без реального Tk (headless — tkinter не установлен в этом
окружении, см. tests/test_generation_panel_pipeline_config.py и
tests/test_media_processor_blockout_categories.py — образцы подмены
sys.modules["tkinter"]).
"""

import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class FakeVar:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeWidget:
    """Минимальный mock-виджет Tk/ttk (образец: test_generation_panel_pipeline_config.py)."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.options = dict(kwargs)
        self.children = []
        self._parent = args[0] if args and hasattr(args[0], "children") else None
        if self._parent is not None:
            self._parent.children.append(self)

    def pack(self, *args, **kwargs):
        return self

    def grid(self, *args, **kwargs):
        return self

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

    def insert(self, *args, **kwargs):
        return None

    def delete(self, *args, **kwargs):
        return None

    def get_children(self, *args, **kwargs):
        return []

    def selection(self, *args, **kwargs):
        return []

    def winfo_children(self):
        return list(self.children)

    def set(self, *args, **kwargs):
        return None

    def get(self, *args, **kwargs):
        return self.options.get("value")

    def delete_range(self, *args, **kwargs):
        return None


def _import_blockout_panel():
    """Свежий импорт blockout_panel с фейковым tkinter (headless)."""
    sys.modules.pop("StoryBookManager.gui.blockout_panel", None)

    tk_module = types.ModuleType("tkinter")
    ttk_module = types.ModuleType("tkinter.ttk")
    messagebox_module = types.ModuleType("tkinter.messagebox")

    tk_module.StringVar = FakeVar
    tk_module.BooleanVar = FakeVar
    tk_module.Canvas = FakeWidget
    tk_module.Listbox = FakeWidget
    tk_module.Toplevel = FakeWidget
    tk_module.Frame = FakeWidget
    tk_module.END = "end"
    tk_module.ttk = ttk_module
    tk_module.messagebox = messagebox_module

    for name in ("Frame", "LabelFrame", "Label", "Button", "Combobox",
                 "Checkbutton", "Treeview", "Scrollbar", "Separator", "Notebook"):
        setattr(ttk_module, name, FakeWidget)

    messagebox_module.showerror = lambda *args, **kwargs: None
    messagebox_module.showwarning = lambda *args, **kwargs: None
    messagebox_module.showinfo = lambda *args, **kwargs: None
    messagebox_module.askyesno = lambda *args, **kwargs: True

    file_manager_module = types.ModuleType("StoryBookManager.core.file_manager")
    file_manager_module.FileManager = MagicMock

    media_processor_module = types.ModuleType("StoryBookManager.core.media_processor")
    media_processor_module.MediaProcessor = MagicMock

    video_player_module = types.ModuleType("StoryBookManager.gui.video_player")
    video_player_module.VideoPlayer = MagicMock

    with patch.dict(
        sys.modules,
        {
            "tkinter": tk_module,
            "tkinter.ttk": ttk_module,
            "tkinter.messagebox": messagebox_module,
            "StoryBookManager.core.file_manager": file_manager_module,
            "StoryBookManager.core.media_processor": media_processor_module,
            "StoryBookManager.gui.video_player": video_player_module,
        },
    ):
        return importlib.import_module("StoryBookManager.gui.blockout_panel")


# ---------------------------------------------------------------------------
# Чистые функции
# ---------------------------------------------------------------------------

class TestPureFunctions(unittest.TestCase):
    def setUp(self):
        self.module = _import_blockout_panel()

    def test_compute_spec_hash_matches_blockout_renderer_algorithm(self):
        """Дубликат раздела 10.2: sha256 канонического JSON, тот же префикс,
        что и у custom_tools/storybook/blockout_renderer.py::compute_spec_hash()."""
        import hashlib
        chain_spec = {"chain_id": "sc01_ch01", "scene_number": 1, "shots": [{"a": 1}]}
        canonical = json.dumps(chain_spec, ensure_ascii=False, sort_keys=True, default=str)
        expected = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.assertEqual(self.module.compute_spec_hash(chain_spec), expected)

    def test_compute_spec_hash_is_order_independent(self):
        a = {"b": 1, "a": 2}
        b = {"a": 2, "b": 1}
        self.assertEqual(self.module.compute_spec_hash(a), self.module.compute_spec_hash(b))

    def test_format_timing_rounds_to_mm_ss(self):
        self.assertEqual(self.module.format_timing(5), "00:05")
        self.assertEqual(self.module.format_timing(65), "01:05")
        self.assertEqual(self.module.format_timing(5.6), "00:06")

    def test_parse_timing_seconds_handles_mm_ss_and_bare_number(self):
        self.assertEqual(self.module.parse_timing_seconds("01:05"), 65)
        self.assertEqual(self.module.parse_timing_seconds("5"), 5.0)
        self.assertIsNone(self.module.parse_timing_seconds("garbage"))
        self.assertIsNone(self.module.parse_timing_seconds(None))

    def test_build_manual_duration_update_fills_requested_once(self):
        item = {"timing": "00:05"}
        update = self.module.build_manual_duration_update(item, 10)
        self.assertEqual(update["duration_s"], 10)
        self.assertEqual(update["duration_source"], "manual")
        self.assertEqual(update["timing"], "00:10")
        # Раздел 18.4: старый timing разбирается ДО перезаписи -> 5, не 10.
        self.assertEqual(update["duration_requested_s"], 5)

    def test_build_manual_duration_update_does_not_overwrite_existing_requested(self):
        item = {"timing": "00:05", "duration_requested_s": 7}
        update = self.module.build_manual_duration_update(item, 10)
        self.assertNotIn("duration_requested_s", update)

    def test_shot_duration_mismatches_scene_true_when_diverged(self):
        self.assertTrue(self.module.shot_duration_mismatches_scene(10, 5))
        self.assertFalse(self.module.shot_duration_mismatches_scene(5, 5))

    def test_shot_duration_mismatches_scene_false_when_unknown(self):
        self.assertFalse(self.module.shot_duration_mismatches_scene(None, 5))
        self.assertFalse(self.module.shot_duration_mismatches_scene(5, None))

    def test_is_manifest_current_true_only_when_all_fields_match(self):
        manifest = {"duration_s": 5, "fps": 24, "spec_hash": "sha256:abc", "resolution": [1920, 1080]}
        self.assertTrue(
            self.module.is_manifest_current(
                manifest, duration_s=5, fps=24, spec_hash="sha256:abc", resolution=[1920, 1080]
            )
        )
        self.assertFalse(
            self.module.is_manifest_current(
                manifest, duration_s=6, fps=24, spec_hash="sha256:abc", resolution=[1920, 1080]
            )
        )
        self.assertFalse(
            self.module.is_manifest_current(
                None, duration_s=5, fps=24, spec_hash="x", resolution=[1920, 1080]
            )
        )

    def test_is_manifest_current_false_on_resolution_mismatch(self):
        """Код-ревью Э10, ошибка 2: смена настройки разрешения (960x540 ->
        1920x1080) без пересборки болванки обязана давать «устарела», а не
        ложное «актуально» — иначе рендер пропускает блокирующие проверки
        B01/B02/B16 самого рендерера (раздел 10.2)."""
        manifest = {"duration_s": 5, "fps": 24, "spec_hash": "sha256:abc", "resolution": [960, 540]}
        self.assertFalse(
            self.module.is_manifest_current(
                manifest, duration_s=5, fps=24, spec_hash="sha256:abc", resolution=[1920, 1080]
            )
        )

    def test_is_manifest_current_false_when_resolution_unknown(self):
        """Раздел 10.2: разрешение неизвестно (не разобралась настройка, или
        старый manifest.json не хранит поле) -> ошибка только в консервативную
        сторону («устарела»), никогда в сторону ложного «актуально»."""
        manifest = {"duration_s": 5, "fps": 24, "spec_hash": "sha256:abc", "resolution": [1920, 1080]}
        self.assertFalse(
            self.module.is_manifest_current(
                manifest, duration_s=5, fps=24, spec_hash="sha256:abc", resolution=None
            )
        )
        manifest_no_resolution = {"duration_s": 5, "fps": 24, "spec_hash": "sha256:abc"}
        self.assertFalse(
            self.module.is_manifest_current(
                manifest_no_resolution, duration_s=5, fps=24, spec_hash="sha256:abc", resolution=[1920, 1080]
            )
        )

    def test_parse_resolution_setting(self):
        self.assertEqual(self.module.parse_resolution_setting("1920x1080"), [1920, 1080])
        self.assertEqual(self.module.parse_resolution_setting("960x540"), [960, 540])
        self.assertIsNone(self.module.parse_resolution_setting(None))
        self.assertIsNone(self.module.parse_resolution_setting(""))
        self.assertIsNone(self.module.parse_resolution_setting("garbage"))

    def test_compute_shot_state_missing_when_no_manifest(self):
        state = self.module.compute_shot_state(None)
        self.assertEqual(state, self.module.SHOT_STATE_MISSING)

    def test_compute_shot_state_stale_on_duration_mismatch_ground_b(self):
        """B01 третья форма (раздел 12): duration_s в shots.json разошёлся с
        scene_spec.json -> stale, даже если manifest.json сам по себе актуален."""
        manifest = {"duration_s": 5, "fps": 24, "spec_hash": "sha256:abc"}
        state = self.module.compute_shot_state(
            manifest, shots_duration_s=10, scene_spec_duration_s=5, fps=24, spec_hash="sha256:abc"
        )
        self.assertEqual(state, self.module.SHOT_STATE_STALE)

    def test_compute_shot_state_stale_on_manifest_actuality_mismatch_ground_a(self):
        manifest = {"duration_s": 5, "fps": 24, "spec_hash": "sha256:OLD"}
        state = self.module.compute_shot_state(
            manifest, shots_duration_s=5, scene_spec_duration_s=5, fps=24, spec_hash="sha256:NEW"
        )
        self.assertEqual(state, self.module.SHOT_STATE_STALE)

    def test_compute_shot_state_junction_error(self):
        manifest = {
            "duration_s": 5, "fps": 24, "spec_hash": "sha256:abc", "resolution": [1920, 1080],
            "junction_with_prev": {"status": "failed"},
        }
        state = self.module.compute_shot_state(
            manifest, shots_duration_s=5, scene_spec_duration_s=5, fps=24, spec_hash="sha256:abc",
            resolution=[1920, 1080],
        )
        self.assertEqual(state, self.module.SHOT_STATE_JUNCTION_ERROR)

    def test_compute_shot_state_rendered_when_all_current_and_no_junction_error(self):
        manifest = {
            "duration_s": 5, "fps": 24, "spec_hash": "sha256:abc", "resolution": [1920, 1080],
            "junction_with_prev": {"status": "exact"},
        }
        state = self.module.compute_shot_state(
            manifest, shots_duration_s=5, scene_spec_duration_s=5, fps=24, spec_hash="sha256:abc",
            resolution=[1920, 1080],
        )
        self.assertEqual(state, self.module.SHOT_STATE_RENDERED)

    def test_compute_shot_state_stale_on_resolution_mismatch(self):
        """Раздел 10.2 через compute_shot_state: расхождение resolution -> stale,
        даже когда duration_s/fps/spec_hash/junction всё в порядке."""
        manifest = {
            "duration_s": 5, "fps": 24, "spec_hash": "sha256:abc", "resolution": [960, 540],
            "junction_with_prev": {"status": "exact"},
        }
        state = self.module.compute_shot_state(
            manifest, shots_duration_s=5, scene_spec_duration_s=5, fps=24, spec_hash="sha256:abc",
            resolution=[1920, 1080],
        )
        self.assertEqual(state, self.module.SHOT_STATE_STALE)

    def test_compute_shot_state_priority_missing_over_stale(self):
        # Раздел 18.4: приоритет "отсутствует" выше "устарела" — manifest.json
        # просто нет, обсуждать его актуальность бессмысленно.
        state = self.module.compute_shot_state(
            None, shots_duration_s=10, scene_spec_duration_s=5, fps=24, spec_hash="sha256:abc"
        )
        self.assertEqual(state, self.module.SHOT_STATE_MISSING)

    def test_is_p10_mismatched_true_when_image_older_and_not_acknowledged(self):
        manifest = {"rendered_at": "2026-08-16T10:00:00Z", "p10_acknowledged": None}
        older_mtime = 1755000000.0  # заведомо раньше rendered_at
        self.assertTrue(self.module.is_p10_mismatched(manifest, [older_mtime]))

    def test_is_p10_mismatched_false_when_acknowledged(self):
        manifest = {"rendered_at": "2026-08-16T10:00:00Z", "p10_acknowledged": "2026-08-16T11:00:00Z"}
        self.assertFalse(self.module.is_p10_mismatched(manifest, [1.0]))

    def test_is_p10_mismatched_false_when_no_manifest_or_no_images(self):
        self.assertFalse(self.module.is_p10_mismatched(None, [1.0]))
        self.assertFalse(
            self.module.is_p10_mismatched({"rendered_at": "2026-08-16T10:00:00Z"}, [])
        )

    def test_files_to_delete_for_regenerate_only_selected_shots(self):
        items = [
            {"scene_number": 1, "shot_number": 1, "shot_type": "start", "output_path": "a.png"},
            {"scene_number": 1, "shot_number": 1, "shot_type": "end", "output_path": "b.png"},
            {"scene_number": 1, "shot_number": 2, "shot_type": "start", "output_path": "c.png"},
        ]
        paths = self.module.files_to_delete_for_regenerate(items, [(1, 1)])
        self.assertEqual(sorted(paths), ["a.png", "b.png"])

    def test_write_manifest_atomic_leaves_no_temp_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "scene_01_shot_01" / "manifest.json"
            self.module.write_manifest_atomic(manifest_path, {"p10_acknowledged": "now"})

            self.assertTrue(manifest_path.is_file())
            written = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(written["p10_acknowledged"], "now")

            leftovers = list(manifest_path.parent.glob("*.tmp"))
            self.assertEqual(leftovers, [])

    def test_blender_binary_path_prefers_env_var(self):
        import tempfile
        import os
        import stat
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "blender"
            fake_bin.write_text("#!/bin/sh\n")
            fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IEXEC)
            with patch.dict(os.environ, {"BLOCKOUT_BLENDER_BIN": str(fake_bin)}):
                self.assertEqual(self.module.blender_binary_path(), str(fake_bin))

    def test_blender_binary_path_none_when_not_found(self):
        import os
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(self.module.shutil, "which", return_value=None):
                self.assertIsNone(self.module.blender_binary_path())

    def test_utc_now_iso_and_parse_iso_utc_roundtrip(self):
        ts = self.module.utc_now_iso()
        self.assertTrue(ts.endswith("Z"))
        parsed = self.module.parse_iso_utc(ts)
        self.assertIsNotNone(parsed)


# ---------------------------------------------------------------------------
# Класс BlockoutPanel — поведение без реального Tk
# ---------------------------------------------------------------------------

class TestBlockoutPanelManualDuration(unittest.TestCase):
    """Раздел 18.4, критерий A24: ручная правка длительности идёт через
    FileManager.save_json_file("shots", ...) — общий протокол раздела 10.2."""

    def setUp(self):
        self.module = _import_blockout_panel()

    def test_apply_manual_duration_updates_both_pair_elements_and_saves(self):
        BlockoutPanel = self.module.BlockoutPanel
        panel = BlockoutPanel.__new__(BlockoutPanel)

        items = [
            {"scene_number": 1, "shot_number": 1, "shot_type": "start", "timing": "00:05"},
            {"scene_number": 1, "shot_number": 1, "shot_type": "end", "timing": "00:05"},
            {"scene_number": 1, "shot_number": 2, "shot_type": "start", "timing": "00:03"},
        ]
        fake_fm = MagicMock()
        fake_fm.load_json_file.return_value = {"items": items}
        fake_fm.save_json_file.return_value = True
        panel.file_manager = fake_fm
        panel.refresh = MagicMock()

        panel._apply_manual_duration((1, 1), 10)

        saved_data = fake_fm.save_json_file.call_args.args[0]
        saved_file_type = fake_fm.save_json_file.call_args.args[1]
        self.assertEqual(saved_file_type, "shots")
        touched = [it for it in saved_data["items"] if it["scene_number"] == 1 and it["shot_number"] == 1]
        self.assertEqual(len(touched), 2)
        for it in touched:
            self.assertEqual(it["duration_s"], 10)
            self.assertEqual(it["duration_source"], "manual")
            self.assertEqual(it["timing"], "00:10")
        untouched = [it for it in saved_data["items"] if it["shot_number"] == 2][0]
        self.assertNotIn("duration_s", untouched)
        panel.refresh.assert_called_once()

    def test_apply_manual_duration_shows_error_and_does_not_refresh_on_save_failure(self):
        BlockoutPanel = self.module.BlockoutPanel
        panel = BlockoutPanel.__new__(BlockoutPanel)

        items = [{"scene_number": 1, "shot_number": 1, "shot_type": "start", "timing": "00:05"}]
        fake_fm = MagicMock()
        fake_fm.load_json_file.return_value = {"items": items}
        fake_fm.save_json_file.return_value = False
        panel.file_manager = fake_fm
        panel.refresh = MagicMock()

        with patch.object(self.module.messagebox, "showerror") as mock_error:
            panel._apply_manual_duration((1, 1), 10)

        mock_error.assert_called_once()
        panel.refresh.assert_not_called()


class TestBlockoutPanelLeaveAsIs(unittest.TestCase):
    """Раздел 19.2: «Оставить как есть» пишет p10_acknowledged в manifest.json
    атомарно — без sidecar-блокировки, только выбранным рассогласованным шотам."""

    def setUp(self):
        self.module = _import_blockout_panel()

    def test_leave_as_is_writes_only_p10_mismatched_selected_shots(self):
        import tempfile
        BlockoutPanel = self.module.BlockoutPanel
        panel = BlockoutPanel.__new__(BlockoutPanel)

        with tempfile.TemporaryDirectory() as tmp:
            fake_fm = MagicMock()
            fake_fm.project_path = Path(tmp)
            panel.file_manager = fake_fm
            panel._get_selected_shot_keys = MagicMock(return_value=[(1, 1), (1, 2)])
            panel._shot_rows = {
                (1, 1): {"p10_mismatched": True, "manifest": {"scene_number": 1, "shot_number": 1}},
                (1, 2): {"p10_mismatched": False, "manifest": {"scene_number": 1, "shot_number": 2}},
            }
            panel.refresh = MagicMock()

            panel.leave_as_is()

            manifest_11 = json.loads(
                (Path(tmp) / "93_blockout" / "scene_01_shot_01" / "manifest.json").read_text()
            )
            self.assertTrue(manifest_11["p10_acknowledged"])

            manifest_12_path = Path(tmp) / "93_blockout" / "scene_01_shot_02" / "manifest.json"
            self.assertFalse(manifest_12_path.exists())
            panel.refresh.assert_called_once()


class TestBlockoutPanelOpenPreview(unittest.TestCase):
    """Предупреждение 2 код-ревью Э10: закрытие окна превью обязано
    останавливать VideoPlayer — иначе поток воспроизведения (video_player.py::
    _playback_loop) ловит исключение на уничтоженном canvas и пишет ложную
    ошибку в лог (раздел 18.4/17.2)."""

    def setUp(self):
        self.module = _import_blockout_panel()

    def test_open_preview_stops_player_on_window_close(self):
        import tempfile
        BlockoutPanel = self.module.BlockoutPanel
        panel = BlockoutPanel.__new__(BlockoutPanel)

        with tempfile.TemporaryDirectory() as tmp:
            preview_dir = Path(tmp) / "93_blockout" / "preview"
            preview_dir.mkdir(parents=True)
            (preview_dir / "blockout_all.mp4").write_bytes(b"fake")

            fake_fm = MagicMock()
            fake_fm.project_path = Path(tmp)
            panel.file_manager = fake_fm

            mock_toplevel_cls = MagicMock()
            mock_video_player_cls = MagicMock()

            with patch.object(self.module.tk, "Toplevel", mock_toplevel_cls), \
                    patch.object(self.module, "VideoPlayer", mock_video_player_cls):
                panel.open_preview()

            top = mock_toplevel_cls.return_value
            player = mock_video_player_cls.return_value

            player.play.assert_called_once()
            self.assertEqual(top.protocol.call_args.args[0], "WM_DELETE_WINDOW")
            on_close = top.protocol.call_args.args[1]

            player.stop.assert_not_called()
            top.destroy.assert_not_called()

            # Симулируем закрытие окна пользователем (Tk зовёт обработчик
            # WM_DELETE_WINDOW сам, здесь мы просто эмулируем этот вызов).
            on_close()

            player.stop.assert_called_once()
            top.destroy.assert_called_once()


class TestBlockoutPanelButtonStates(unittest.TestCase):
    """Раздел 18.4: доступность кнопок запуска зависит от checkpoint, от
    идущей генерации и от несовпадения длительности (ground b)."""

    def setUp(self):
        self.module = _import_blockout_panel()

    def _make_panel(self, has_checkpoint, is_generating, primary_row=None):
        BlockoutPanel = self.module.BlockoutPanel
        panel = BlockoutPanel.__new__(BlockoutPanel)
        panel._has_checkpoint = has_checkpoint
        panel.generation_panel = types.SimpleNamespace(is_generating=is_generating)
        panel._get_selected_shot_keys = MagicMock(return_value=[(1, 1)] if primary_row else [])
        panel._get_primary_selected_shot_key = MagicMock(return_value=(1, 1) if primary_row else None)
        panel._shot_rows = {(1, 1): primary_row} if primary_row else {}
        panel.file_manager = MagicMock()
        panel.file_manager.project_path = Path("/tmp/does-not-exist-blockout-panel-test")
        panel.redraw_shot_btn = FakeWidget()
        panel.redraw_chain_btn = FakeWidget()
        panel.rebuild_scene_btn = FakeWidget()
        panel.build_preview_btn = FakeWidget()
        panel.open_blend_btn = FakeWidget()
        panel.regenerate_btn = FakeWidget()
        panel.leave_as_is_btn = FakeWidget()
        panel.open_preview_btn = FakeWidget()
        panel.hint_label = FakeWidget()
        return panel

    def test_buttons_disabled_without_checkpoint(self):
        panel = self._make_panel(has_checkpoint=False, is_generating=False)
        self.module.BlockoutPanel._update_button_states(panel)
        self.assertEqual(panel.rebuild_scene_btn.options["state"], "disabled")
        self.assertEqual(panel.build_preview_btn.options["state"], "disabled")

    def test_buttons_disabled_while_generating_even_with_checkpoint(self):
        panel = self._make_panel(has_checkpoint=True, is_generating=True)
        self.module.BlockoutPanel._update_button_states(panel)
        self.assertEqual(panel.rebuild_scene_btn.options["state"], "disabled")

    def test_redraw_buttons_disabled_when_duration_mismatches_scene(self):
        """Ground (b): после ручной правки длительности перерисовка шота/цепочки
        недоступна, пока сцена не пересобрана — доступна только «Пересобрать сцену»."""
        primary_row = {"chain_id": "sc01_ch01", "duration_mismatch": True, "p10_mismatched": False}
        panel = self._make_panel(has_checkpoint=True, is_generating=False, primary_row=primary_row)
        self.module.BlockoutPanel._update_button_states(panel)
        self.assertEqual(panel.redraw_shot_btn.options["state"], "disabled")
        self.assertEqual(panel.redraw_chain_btn.options["state"], "disabled")
        self.assertEqual(panel.rebuild_scene_btn.options["state"], "normal")

    def test_redraw_buttons_enabled_after_duration_mismatch_clears(self):
        """A24: после пересборки сцены ground (b) снимается — кнопки перерисовки
        снова активны (даже если дерево всё ещё помечает шот «устарела» по ground a)."""
        primary_row = {"chain_id": "sc01_ch01", "duration_mismatch": False, "p10_mismatched": False}
        panel = self._make_panel(has_checkpoint=True, is_generating=False, primary_row=primary_row)
        self.module.BlockoutPanel._update_button_states(panel)
        self.assertEqual(panel.redraw_shot_btn.options["state"], "normal")
        self.assertEqual(panel.redraw_chain_btn.options["state"], "normal")

    def test_leave_as_is_enabled_only_when_p10_mismatch_selected(self):
        primary_row = {"chain_id": "sc01_ch01", "duration_mismatch": False, "p10_mismatched": True}
        panel = self._make_panel(has_checkpoint=True, is_generating=False, primary_row=primary_row)
        self.module.BlockoutPanel._update_button_states(panel)
        self.assertEqual(panel.leave_as_is_btn.options["state"], "normal")


class TestResolveHasCheckpoint(unittest.TestCase):
    """Предупреждение 2 код-ревью Э10: `_probe_checkpoint()` обращался к
    приватному `PipelineRunner._get_latest_project_checkpoint` напрямую — без
    публичной обёртки, а `core/pipeline_runner.py` правится параллельно
    другим агентом и вне области этой задачи. `_resolve_has_checkpoint()`
    оборачивает обращение через `getattr`, чтобы переименование/удаление
    метода деградировало тихо (checkpoint = «отсутствует»), а не роняло
    вкладку."""

    def setUp(self):
        self.module = _import_blockout_panel()

    def test_returns_true_when_checkpoint_with_context_present(self):
        import asyncio
        BlockoutPanel = self.module.BlockoutPanel
        panel = BlockoutPanel.__new__(BlockoutPanel)
        fake_checkpoint = types.SimpleNamespace(context={"some": "context"})

        async def fake_getter(project_id):
            return fake_checkpoint

        panel.generation_panel = types.SimpleNamespace(
            pipeline_runner=types.SimpleNamespace(_get_latest_project_checkpoint=fake_getter)
        )
        result = asyncio.run(panel._resolve_has_checkpoint("proj-1"))
        self.assertTrue(result)

    def test_returns_false_when_checkpoint_has_no_context(self):
        import asyncio
        BlockoutPanel = self.module.BlockoutPanel
        panel = BlockoutPanel.__new__(BlockoutPanel)
        fake_checkpoint = types.SimpleNamespace(context=None)

        async def fake_getter(project_id):
            return fake_checkpoint

        panel.generation_panel = types.SimpleNamespace(
            pipeline_runner=types.SimpleNamespace(_get_latest_project_checkpoint=fake_getter)
        )
        result = asyncio.run(panel._resolve_has_checkpoint("proj-1"))
        self.assertFalse(result)

    def test_degrades_to_false_without_crashing_when_method_missing(self):
        """Основная проверка: PipelineRunner без _get_latest_project_checkpoint
        (переименован/убран) не роняет вкладку AttributeError-ом — checkpoint
        тихо считается отсутствующим."""
        import asyncio
        BlockoutPanel = self.module.BlockoutPanel
        panel = BlockoutPanel.__new__(BlockoutPanel)
        panel.generation_panel = types.SimpleNamespace(
            pipeline_runner=types.SimpleNamespace()  # метода нет вовсе
        )
        result = asyncio.run(panel._resolve_has_checkpoint("proj-1"))
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
