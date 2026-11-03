"""Э8а / критерий A35 (docs/tz-blockout-reference-pipeline.md, раздел 18.0).

Проверяет, что точки StoryBookManager, определявшие путь к каталогу
проектов по-разному, сведены к общим резолверам
custom_tools/storybook/project_paths.py: storybook_projects_root() (корень)
и safe_storybook_project_dir(project_id) (путь одного проекта):

- ProjectManager.__init__ (core/project_manager.py)
- FileManager.__init__ (core/file_manager.py)
- StoryBookManager/main.py::_prepare_projects_root() (bootstrap перед созданием MainWindow)

Ещё одна точка, PipelineRunner.validate_project_for_pipeline(), — эталон
(custom_tools.storybook.project_paths использует уже сегодня, раздел 18.0),
её не трогаем и здесь отдельно не тестируем; она покрыта
tests/test_project_validation_dependencies.py.

Headless — без реального Tk. Образец подмены sys.modules["tkinter"] —
Образец patch.dict окружения —
tests/test_project_validation_dependencies.py.
"""

import contextlib
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "StoryBookManager"))

from custom_tools.storybook.project_paths import (  # noqa: E402
    safe_storybook_project_dir,
    storybook_projects_root,
)

@contextlib.contextmanager
def _tk_stubbed(stubs):
    """На время блока подменяет tkinter заглушками и даёт свежий StoryBookManager.*.

    На выходе возвращает только эти модули как были: заглушки и
    StoryBookManager.*, импортированные под ними, не протекают в другие тесты
    (сторонние библиотеки, импортированные внутри, не трогаем).
    """
    def _own(name):
        return name in stubs or name == "StoryBookManager" or name.startswith("StoryBookManager.")

    saved = {n: m for n, m in sys.modules.items() if _own(n)}
    for name in saved:
        del sys.modules[name]
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for name in [n for n in sys.modules if _own(n)]:
            del sys.modules[name]
        sys.modules.update(saved)


app_settings = FileManager = ProjectManager = None  # заполняет фикстура _tk_stubbed_modules


@pytest.fixture(autouse=True, scope="module")
def _tk_stubbed_modules():
    """Подменяет tkinter заглушками только на время модуля.

    StoryBookManager.* импортируется под заглушкой и после модуля убирается из
    sys.modules вместе с самой подменой (_tk_stubbed возвращает их как были),
    иначе заглушки протекают в другие тесты (например, blockout-wiring).
    """
    global app_settings, FileManager, ProjectManager
    tk_mock = MagicMock()
    ttk_mock = MagicMock()
    tk_mock.ttk = ttk_mock
    tk_mock.filedialog = MagicMock()
    tk_mock.messagebox = MagicMock()
    tk_mock.scrolledtext = MagicMock()
    tk_mock.StringVar = MagicMock
    tk_mock.BooleanVar = MagicMock
    stubs = {
        "tkinter": tk_mock,
        "tkinter.ttk": ttk_mock,
        "tkinter.filedialog": tk_mock.filedialog,
        "tkinter.messagebox": tk_mock.messagebox,
        "tkinter.scrolledtext": tk_mock.scrolledtext,
    }
    with _tk_stubbed(stubs):
        from StoryBookManager.config.settings import app_settings as _settings
        from StoryBookManager.core.file_manager import FileManager as _fm
        from StoryBookManager.core.project_manager import ProjectManager as _pm
        app_settings, FileManager, ProjectManager = _settings, _fm, _pm
        yield
    app_settings = FileManager = ProjectManager = None


# Все модули берут app_settings из StoryBookManager.config.settings (единый синглтон);
# каталоги бэкапов/логов патчим в _isolated_backup_dir, чтобы не зависеть от машины.


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
    FileManager создаёт поддиректории под backup_directory.
    settings.json (закоммичен) указывает оба каталога на путь с другого
    хоста (/Users/kosoj/...) без прав на запись для текущего пользователя —
    без изоляции тесты, конструирующие ProjectManager(), падали бы на чистом
    чекауте/CI. Патчим оба геттера, как test_new_project_creation.py/
    test_export_project.py.
    """
    backups = tmp_path / "isolated_backups"
    logs = tmp_path / "isolated_logs"
    for settings_obj in (app_settings,):
        monkeypatch.setattr(settings_obj, "get_backup_directory", lambda: backups)
        monkeypatch.setattr(settings_obj, "get_logs_directory", lambda: logs)


def test_project_manager_projects_dir_uses_custom_env(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))

    pm = ProjectManager()

    assert pm.projects_dir == storybook_projects_root()
    assert pm.projects_dir == root.resolve()


def test_file_manager_uses_resolved_project_path(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))

    fm = FileManager("proj")

    expected = safe_storybook_project_dir("proj")
    assert fm.project_path == expected


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


def test_main_opens_window_and_prompts_settings_when_projects_dir_missing(tmp_path, monkeypatch):
    """Ф0 п.3: нет папки проектов - окно всё равно открывается и просит настройки."""
    monkeypatch.setattr(app_settings, "get_projects_directory", lambda: tmp_path / "missing")
    # main() пишет переменную напрямую; так monkeypatch вернёт прежнее значение после теста
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", "/should/be/overwritten")
    module = _import_main_module()
    events = []

    class FakeTk:
        def after(self, ms, fn):
            events.append(("after", fn))

        def mainloop(self):
            events.append(("mainloop", None))

    class FakeMainWindow:
        def __init__(self, root):
            events.append(("window", None))

        def prompt_missing_projects_dir(self):
            pass

    monkeypatch.setattr(module.tk, "Tk", FakeTk)
    monkeypatch.setattr(module, "MainWindow", FakeMainWindow)

    assert module.main() == 0
    names = [name for name, _ in events]
    assert names == ["window", "after", "mainloop"]
    assert events[1][1].__name__ == "prompt_missing_projects_dir"


def test_prompt_missing_projects_dir_opens_settings():
    """Ф0 п.3: метод MainWindow.prompt_missing_projects_dir зовёт show_settings."""
    src = (Path(__file__).parent.parent / "StoryBookManager" / "gui" / "main_window.py").read_text(encoding="utf-8")
    start = src.index("def prompt_missing_projects_dir(self)")
    body = src[start:src.index("\n    def ", start + 1)]
    assert "show_settings()" in body
    assert "showwarning" in body


def test_a35_all_points_share_one_absolute_path_under_custom_env(tmp_path, monkeypatch):
    """ГЛАВНЫЙ тест A35: все точки под одним STORYBOOK_PROJECTS_DIR
    дают один и тот же абсолютный путь."""
    root = tmp_path / "shared_projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    monkeypatch.setattr(app_settings, "get_projects_directory", lambda: root)

    expected_root = storybook_projects_root()
    expected_project_path = safe_storybook_project_dir("proj")

    pm = ProjectManager()
    fm = FileManager("proj")
    main_module = _import_main_module()
    main_root, _ = main_module._prepare_projects_root()

    assert pm.projects_dir == expected_root
    assert main_root == expected_root
    assert fm.project_path == expected_project_path

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

    assert pm.projects_dir == root.resolve()
    assert fm.project_path == expected_project_path


def test_backward_compatibility_without_env_var_uses_cwd_relative_default(tmp_path, monkeypatch):
    monkeypatch.delenv("STORYBOOK_PROJECTS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    expected_root = (Path("plots") / "storybooks").resolve()

    pm = ProjectManager()
    fm = FileManager("proj")

    assert pm.projects_dir == expected_root
    assert fm.project_path == expected_root / "proj"
