"""Э8а / критерий A35 (docs/tz-blockout-reference-pipeline.md, раздел 18.0).

Проверяет, что пять точек StoryBookManager, определявших путь к каталогу
проектов по-разному, сведены к общим резолверам
custom_tools/storybook/project_paths.py: storybook_projects_root() (корень)
и safe_storybook_project_dir(project_id) (путь одного проекта):

- ProjectManager.__init__ (core/project_manager.py)
- FileManager.__init__ (core/file_manager.py)
- MediaProcessor.__init__ (core/media_processor.py)
- GenerationPanel._resolve_project_path() (gui/generation_panel.py, fallback-ветка)
- StoryBookManager/main.py::_prepare_projects_root() (bootstrap перед созданием MainWindow)

Шестая точка, PipelineRunner.validate_project_for_pipeline(), — эталон
(custom_tools.storybook.project_paths использует уже сегодня, раздел 18.0),
её не трогаем и здесь отдельно не тестируем; она покрыта
tests/test_project_validation_dependencies.py.

Headless — без реального Tk. Образец подмены sys.modules["tkinter"] —
tests/test_generation_panel_pipeline_config.py. Образец patch.dict окружения —
tests/test_project_validation_dependencies.py.
"""

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


tk_mock = MagicMock()
sys.modules["tkinter"] = tk_mock

ttk_mock = MagicMock()
sys.modules["tkinter.ttk"] = ttk_mock
tk_mock.ttk = ttk_mock

sys.modules["tkinter.filedialog"] = MagicMock()
sys.modules["tkinter.messagebox"] = MagicMock()
sys.modules["tkinter.scrolledtext"] = MagicMock()
tk_mock.filedialog = MagicMock()
tk_mock.messagebox = MagicMock()
tk_mock.scrolledtext = MagicMock()
tk_mock.StringVar = MagicMock
tk_mock.BooleanVar = MagicMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "StoryBookManager"))

from custom_tools.storybook.project_paths import (  # noqa: E402
    safe_storybook_project_dir,
    storybook_projects_root,
)
from StoryBookManager.config.settings import app_settings  # noqa: E402
from StoryBookManager.core.file_manager import FileManager  # noqa: E402
from StoryBookManager.core.media_processor import MediaProcessor  # noqa: E402
from StoryBookManager.core.project_manager import ProjectManager  # noqa: E402

# ProjectManager/FileManager/MediaProcessor import app_settings as bare
# "config.settings" (not "StoryBookManager.config.settings"): thanks to the
# sys.path order above this resolves to the same settings.py file, but under
# a different module name Python treats it as a SEPARATE singleton (exactly
# the trap documented in docs/tz-blockout-reference-pipeline.md §18.0). Both
# are patched in _isolated_backup_dir below so tests don't depend on the real
# machine's (possibly unwritable) backup_directory.
import config.settings as _legacy_config_settings  # noqa: E402


def _import_generation_panel():
    """Свежий импорт generation_panel с фейковыми tkinter/зависимостями (headless).

    Образец: tests/test_generation_panel_pipeline_config.py::_import_generation_panel.
    Тяжёлые StoryBookManager.core.pipeline_runner и gui.step_tracker подменены
    заглушками — реальный импорт не нужен, чтобы проверить только
    _resolve_project_path().
    """
    sys.modules.pop("StoryBookManager.gui.generation_panel", None)

    class FakeFrame:
        def __init__(self, *args, **kwargs):
            pass

    tk_module = types.ModuleType("tkinter")
    ttk_module = types.ModuleType("tkinter.ttk")
    messagebox_module = types.ModuleType("tkinter.messagebox")
    scrolledtext_module = types.ModuleType("tkinter.scrolledtext")
    project_manager_module = types.ModuleType("StoryBookManager.core.project_manager")
    pipeline_runner_module = types.ModuleType("StoryBookManager.core.pipeline_runner")
    step_tracker_module = types.ModuleType("StoryBookManager.gui.step_tracker")

    tk_module.ttk = ttk_module
    tk_module.messagebox = messagebox_module
    tk_module.scrolledtext = scrolledtext_module

    ttk_module.Frame = FakeFrame

    project_manager_module.Project = object
    pipeline_runner_module.PipelineRunner = MagicMock
    pipeline_runner_module.run_pipeline_sync = MagicMock
    step_tracker_module.StepTracker = FakeFrame

    with patch.dict(
        sys.modules,
        {
            "tkinter": tk_module,
            "tkinter.ttk": ttk_module,
            "tkinter.messagebox": messagebox_module,
            "tkinter.scrolledtext": scrolledtext_module,
            "StoryBookManager.core.project_manager": project_manager_module,
            "StoryBookManager.core.pipeline_runner": pipeline_runner_module,
            "StoryBookManager.gui.step_tracker": step_tracker_module,
        },
    ):
        return importlib.import_module("StoryBookManager.gui.generation_panel")


def _resolve_via_generation_panel(project_id: str) -> Path:
    """Fallback-ветка GenerationPanel._resolve_project_path() (проект не выбран)."""
    module = _import_generation_panel()
    panel = module.GenerationPanel.__new__(module.GenerationPanel)
    panel.current_project = None
    return panel._resolve_project_path(project_id)


def _import_main_module():
    """Свежий импорт StoryBookManager/main.py без запуска GUI/логирования.

    setup_logging() открывает файл лога на диске (может упасть с
    PermissionError на файле, оставшемся от другого пользователя) и не имеет
    отношения к определению пути к проекту — подменяем no-op-заглушкой.
    gui.main_window тянет весь GUI-стек (пайплайн-раннер и т.д.) — подменяем
    заглушкой, поскольку из main.py нужна только bootstrap-функция.
    """
    sys.modules.pop("StoryBookManager.main", None)

    logging_config_module = types.ModuleType("StoryBookManager.utils.logging_config")
    logging_config_module.setup_logging = lambda: None

    main_window_module = types.ModuleType("StoryBookManager.gui.main_window")
    main_window_module.MainWindow = object

    with patch.dict(
        sys.modules,
        {
            "StoryBookManager.utils.logging_config": logging_config_module,
            "StoryBookManager.gui.main_window": main_window_module,
        },
    ):
        return importlib.import_module("StoryBookManager.main")


@pytest.fixture(autouse=True)
def _isolated_backup_dir(tmp_path, monkeypatch):
    """Изолирует backup_directory и logs_directory от реальной машины.

    Не относится к Э8а (backup_dir/ensure_directories() не меняются), но
    ProjectManager.__init__ зовёт app_settings.ensure_directories(), которая
    mkdir'ит ОБА каталога — get_backup_directory() и get_logs_directory(); а
    FileManager/MediaProcessor создают поддиректории под backup_directory.
    settings.json (закоммичен) указывает оба каталога на путь с другого
    хоста (/Users/kosoj/...) без прав на запись для текущего пользователя —
    без изоляции тесты, конструирующие ProjectManager(), падали бы на чистом
    чекауте/CI. Патчим оба геттера на обоих синглтонах (см. комментарий у
    _legacy_config_settings выше), как test_new_project_creation.py/
    test_export_project.py.
    """
    backups = tmp_path / "isolated_backups"
    logs = tmp_path / "isolated_logs"
    for settings_obj in (app_settings, _legacy_config_settings.app_settings):
        monkeypatch.setattr(settings_obj, "get_backup_directory", lambda: backups)
        monkeypatch.setattr(settings_obj, "get_logs_directory", lambda: logs)


def test_project_manager_projects_dir_uses_custom_env(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))

    pm = ProjectManager()

    assert pm.projects_dir == storybook_projects_root()
    assert pm.projects_dir == root.resolve()


def test_file_manager_and_media_processor_share_resolved_project_path(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))

    fm = FileManager("proj")
    mp = MediaProcessor("proj")

    expected = safe_storybook_project_dir("proj")
    assert fm.project_path == expected
    assert mp.project_path == expected


def test_generation_panel_fallback_uses_resolver(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))

    resolved = _resolve_via_generation_panel("proj")

    assert resolved == safe_storybook_project_dir("proj")


def test_main_prepare_projects_root_sets_env_and_computes_root(tmp_path, monkeypatch):
    root = tmp_path / "configured_projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", "/should/be/overwritten")
    monkeypatch.setattr(app_settings, "get_projects_directory", lambda: root)

    module = _import_main_module()

    computed_root, exists_before = module._prepare_projects_root()

    assert computed_root == storybook_projects_root()
    assert exists_before is False
    import os

    assert os.environ["STORYBOOK_PROJECTS_DIR"] == str(root)

    root.mkdir(parents=True)
    _, exists_after = module._prepare_projects_root()
    assert exists_after is True


def test_a35_all_five_points_share_one_absolute_path_under_custom_env(tmp_path, monkeypatch):
    """ГЛАВНЫЙ тест A35: все пять точек под одним STORYBOOK_PROJECTS_DIR
    дают один и тот же абсолютный путь."""
    root = tmp_path / "shared_projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    monkeypatch.setattr(app_settings, "get_projects_directory", lambda: root)

    expected_root = storybook_projects_root()
    expected_project_path = safe_storybook_project_dir("proj")

    pm = ProjectManager()
    fm = FileManager("proj")
    mp = MediaProcessor("proj")
    generation_panel_path = _resolve_via_generation_panel("proj")
    main_module = _import_main_module()
    main_root, _ = main_module._prepare_projects_root()

    assert pm.projects_dir == expected_root
    assert main_root == expected_root
    assert fm.project_path == expected_project_path
    assert mp.project_path == expected_project_path
    assert generation_panel_path == expected_project_path

    import os

    assert os.environ["STORYBOOK_PROJECTS_DIR"] == str(app_settings.get_projects_directory())


def test_a35_independent_of_working_directory(tmp_path, monkeypatch):
    root = tmp_path / "shared_projects"
    other_cwd = tmp_path / "somewhere_else"
    other_cwd.mkdir()
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    monkeypatch.chdir(other_cwd)

    expected_project_path = safe_storybook_project_dir("proj")

    pm = ProjectManager()
    fm = FileManager("proj")
    mp = MediaProcessor("proj")
    generation_panel_path = _resolve_via_generation_panel("proj")

    assert pm.projects_dir == root.resolve()
    assert fm.project_path == expected_project_path
    assert mp.project_path == expected_project_path
    assert generation_panel_path == expected_project_path


def test_backward_compatibility_without_env_var_uses_cwd_relative_default(tmp_path, monkeypatch):
    monkeypatch.delenv("STORYBOOK_PROJECTS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    expected_root = (Path("plots") / "storybooks").resolve()

    pm = ProjectManager()
    fm = FileManager("proj")
    mp = MediaProcessor("proj")
    generation_panel_path = _resolve_via_generation_panel("proj")

    assert pm.projects_dir == expected_root
    assert fm.project_path == expected_root / "proj"
    assert mp.project_path == expected_root / "proj"
    assert generation_panel_path == expected_root / "proj"
