"""Подключение экранов «Фильм», «Болванка», «Файлы», «Настройки» к главному окну под Xvfb.

Тест запускает этот же файл дочерним процессом под xvfb-run (Tk живёт отдельно от pytest)
с подставным мостом: сервера и платных вызовов нет.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "helpers"))


def _child() -> int:
    from tk_screens_smoke import GOLDEN, PID, FakeBridge, _make_projects, _make_video, _pump

    tmp = Path(tempfile.mkdtemp(prefix="sbm_wire_"))
    projects = tmp / "projects"
    projects.mkdir()
    _make_projects(projects)
    _make_video(projects / PID / "final" / "film.mp4")
    os.environ["STORYBOOK_PROJECTS_DIR"] = str(projects)

    import tkinter as tk
    from StoryBookManager.config.settings import app_settings
    app_settings.config_file = tmp / "settings.json"
    app_settings.set("projects_directory", str(projects))
    app_settings.set("backup_directory", str(tmp / "backups"))
    app_settings.set("logs_directory", str(tmp / "logs"))
    app_settings.set("window_geometry", "1280x800")
    app_settings.set("auto_save_interval", 0)

    from StoryBookManager.gui.main_window import MainWindow
    report = {"errors": []}
    root = tk.Tk()
    app = None
    try:
        state = dict(json.loads(GOLDEN.read_text(encoding="utf-8"))["states"]["base"], status="ok")
        app = MainWindow(root)
        root.geometry("1280x800+0+0")
        root.update()
        app.bridge.call = FakeBridge(tmp, state).call
        app.on_project_selected(app.project_manager.load_project(PID))
        _pump(root, 1.0)

        names = {}
        for section in ("movie", "blockout", "files", "settings"):
            app.show_section(section)
            _pump(root, 0.3)
            names[section] = [app.host.current, type(app.sections[section]).__name__]
        report["sections"] = names

        app.show_section("overview")
        app.show_settings()
        report["show_settings"] = app.host.current

        jumps = []
        app.shots.apply_intent = lambda filter=None, shot_key=None: jumps.append(shot_key)
        app.show_section("movie")
        app.movie.on_open_shot("1-2")
        report["film_jump"] = [app.host.current, list(jumps)]
        app.show_section("blockout")
        _pump(root, 0.5)
        app.blockout_panel.tree.select_keys(["1-1"])
        _pump(root, 0.5)
        app.blockout_panel.open_in_shots()
        report["blockout_jump"] = [app.host.current, list(jumps)]

        app.show_section("files")
        _pump(root, 1.0)
        files = app.editor_panel
        files.tree.selection_set("brief")
        files._on_tree_select(None)
        _pump(root, 1.0)
        asked = []
        files.ask_choice = lambda title, body, buttons, default="": asked.append(title) or "stay"
        files.states[files.current].dirty = True
        left = app.show_section("movie")
        report["unsaved"] = {"left": left, "current": app.host.current, "asked": asked}
    except Exception:
        report["errors"].append(traceback.format_exc())
    finally:
        for step in (lambda: app._shutdown_shell(), root.destroy):
            try:
                step()
            except Exception:
                pass
    print("WIRING_REPORT " + json.dumps(report, ensure_ascii=False, default=str))
    return 1 if report["errors"] else 0


@pytest.fixture(scope="module")
def report():
    if shutil.which("xvfb-run") is None:
        pytest.skip("нет xvfb-run")
    try:
        import tkinter  # noqa: F401
    except ImportError:
        pytest.skip("нет tkinter")
    cmd = ["xvfb-run", "-a", "-s", "-screen 0 1700x1000x24", sys.executable, str(Path(__file__)), "--child"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=ROOT)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("WIRING_REPORT ")]
    assert lines, proc.stdout[-2000:] + proc.stderr[-2000:]
    data = json.loads(lines[-1][len("WIRING_REPORT "):])
    assert data["errors"] == []
    return data


def test_each_section_opens_new_screen(report):
    assert report["sections"] == {
        "movie": ["movie", "FilmScreen"], "blockout": ["blockout", "BlockoutScreen"],
        "files": ["files", "FilesScreen"], "settings": ["settings", "SettingsScreen"]}


def test_show_settings_opens_settings_section(report):
    assert report["show_settings"] == "settings"


def test_jump_to_shot_from_film_and_blockout(report):
    assert report["film_jump"] == ["shots", ["1-2"]]
    assert report["blockout_jump"] == ["shots", ["1-2", "1-1"]]


def test_leaving_files_with_unsaved_edits_asks(report):
    assert report["unsaved"]["left"] is False
    assert report["unsaved"]["current"] == "files"
    assert len(report["unsaved"]["asked"]) == 1


def _scenarios() -> int:
    """Сценарии ревью У5 (ошибки 2-7): каждый проверяет поведение экрана через подставной мост."""
    from tk_screens_smoke import GOLDEN, PID, FakeBridge, _make_projects, _make_video, _pump

    tmp = Path(tempfile.mkdtemp(prefix="sbm_scen_"))
    projects = tmp / "projects"
    projects.mkdir()
    _make_projects(projects)
    _make_video(projects / PID / "final" / "film.mp4")
    os.environ["STORYBOOK_PROJECTS_DIR"] = str(projects)

    import tkinter as tk
    from StoryBookManager.config.settings import app_settings
    app_settings.config_file = tmp / "settings.json"
    app_settings.set("projects_directory", str(projects))
    app_settings.set("backup_directory", str(tmp / "backups"))
    app_settings.set("logs_directory", str(tmp / "logs"))
    app_settings.set("window_geometry", "1280x800")
    app_settings.set("ui_scale", 100)
    app_settings.set("auto_save_interval", 0)

    class Bridge(FakeBridge):
        saves = []
        updates = []
        size_bump = 0
        update_error = False
        env_default = False
        update_ok_tk_fails = False
        settings_after = {}

        def call(self, cap, payload, on_success=None, on_error=None, latest_only_key=None, **kw):
            if cap == "artifact_save":
                self.saves.append(payload)
                return on_success({"status": "ok", "digest": "a2", "validation": {"findings": [], "counts": {}}})
            if cap == "app_settings_update":
                self.updates.append(payload)
                if self.update_ok_tk_fails:
                    return on_success(dict(self.settings_after, digest="sd2"))
                if self.update_error:
                    return on_error({"status": "error", "error": {"code": "invalid_argument", "message": "Папка бэкапов: нельзя"}})
            if cap == "media_timeline" and self.size_bump:
                def bumped(result):
                    result["final"]["video"]["size"] += self.size_bump
                    on_success(result)
                return super().call(cap, payload, on_success=bumped, on_error=on_error, latest_only_key=latest_only_key)
            if cap == "app_settings_get" and self.env_default:
                def marked(result):
                    result["settings"]["projects_directory"]["source"] = "env"
                    result["settings"]["backup_directory"]["source"] = "default"
                    on_success(result)
                return super().call(cap, payload, on_success=marked, on_error=on_error, latest_only_key=latest_only_key)
            return super().call(cap, payload, on_success=on_success, on_error=on_error, latest_only_key=latest_only_key)

    from StoryBookManager.gui.main_window import MainWindow
    report = {"errors": []}
    root = tk.Tk()
    app = None
    try:
        state = dict(json.loads(GOLDEN.read_text(encoding="utf-8"))["states"]["base"], status="ok")
        app = MainWindow(root)
        root.geometry("1280x800+0+0")
        root.update()
        bridge = Bridge(tmp, state)
        bridge.env_default = True
        app.bridge.call = bridge.call
        app.on_project_selected(app.project_manager.load_project(PID))
        _pump(root, 1.0)
        files = app.editor_panel
        files.ask_choice = lambda title, body, buttons, default="": "stay"

        # ошибка 2: сохранение из режима JSON обновляет содержимое, форма не откатывает правку
        app.show_section("files")
        _pump(root, 0.5)
        files.tree.selection_set("brief")
        files._on_tree_select(None)
        _pump(root, 0.5)
        files.mode_var.set("json")
        files._switch_mode()
        files.text.delete("1.0", "end")
        files.text.insert("1.0", json.dumps({"title": "Из JSON", "description": "d", "genre": "g", "target_age": "6-8"}, ensure_ascii=False))
        files.text.edit_modified(False)
        files.state_dirty = files.states[files.current].dirty = True
        files.save_current_file()
        files.mode_var.set("form")
        files._switch_mode()
        _pump(root, 0.3)
        titles = [f["value"] for f in files._field_list if f["pointer"] == "/title"]
        report["json_save"] = {"content_title": files.states["brief"].content.get("title"), "form_title": titles,
                               "dirty": files.states["brief"].dirty}

        # ошибка 3: Ctrl+S применяет поле, в котором печатают
        bridge.saves.clear()
        entry = files._field_vars["/title"]
        entry.delete(0, "end")
        entry.insert(0, "Набрано без ухода курсора")
        entry.focus_force()
        root.update()
        app.save_current()
        report["ctrl_s"] = {"sent": [p.get("content", {}).get("title") for p in bridge.saves], "dirty": files.states["brief"].dirty}

        # ошибка 4: смена проекта через Ctrl+K при несохранённых правках спрашивает и не бросает правки
        files.states["brief"].dirty = True
        selected = []
        original = app.on_project_selected
        app.on_project_selected = lambda project: selected.append(project.project_id) or original(project)
        app._open_project_by_id(PID)
        report["switch_project"] = {"selected": selected, "dirty": files.states["brief"].dirty}
        files.states["brief"].dirty = False
        app.on_project_selected = original

        # финальное ревью: Ctrl+K в «Кадрах» спрашивает раздел, а не стирает правку карточки
        app.show_section("shots")
        _pump(root, 0.5)
        asked_shots = []
        app.shots.can_leave = lambda: asked_shots.append(1) or False
        selected.clear()
        app.on_project_selected = lambda project: selected.append(project.project_id) or original(project)
        app._open_project_by_id(PID)
        report["switch_in_shots"] = {"asked": len(asked_shots), "selected": list(selected), "section": app.host.current}
        app.on_project_selected = original
        del app.shots.can_leave

        # ошибки 5 и 6 и предупреждение 1: настройки
        app.show_section("settings")
        _pump(root, 1.2)
        st = app.settings_screen
        st.ask_confirm = lambda spec: True
        report["marks"] = {"projects": st.notes["projects_directory"].get(), "backups": st.notes["backup_directory"].get()}
        st.vars["auto_save_interval"].set("5000")
        st.save()
        report["autosave_range"] = {"banner": st.banner_var.get(), "updates": len(bridge.updates),
                                    "stored": app_settings.get("auto_save_interval")}
        st.vars["auto_save_interval"].set("0")
        st.vars["ui_scale"].set("150")
        st.vars["max_backup_files"].set("11")
        bridge.update_error = True
        st.save()
        report["scale_after_error"] = {"stored": app_settings.get("ui_scale"), "banner": st.banner_var.get()}
        bridge.update_error = False

        # раунд 2: сервер принял, запись параметров окна упала - форма и отпечаток всё равно обновлены
        from custom_tools.storybook.studio import app_settings as studio_app
        from custom_tools.storybook.studio.errors import StudioError
        bridge.update_ok_tk_fails = True
        st.vars["max_backup_files"].set("11")
        st.vars["ui_scale"].set("125")
        snap_after = {"status": "ok", "digest": "sd2", "settings": {k: dict(v) for k, v in st.snapshot["settings"].items()}, "checks": {}, "warnings": []}
        snap_after["settings"]["max_backup_files"]["value"] = 11
        bridge.settings_after = snap_after
        original_update = studio_app.tk_settings_update
        studio_app.tk_settings_update = lambda **kw: (_ for _ in ()).throw(StudioError("invalid_argument", "Окно: сбой записи"))
        try:
            st.save()
        finally:
            studio_app.tk_settings_update = original_update
            bridge.update_ok_tk_fails = False
        report["tk_write_fails"] = {"digest": st.digest, "form": st.vars["max_backup_files"].get(), "banner": st.banner_var.get()}

        # ошибка 7: после пересборки тот же путь, но другой файл - плеер открывает его заново
        app.show_section("movie")
        _pump(root, 1.5)
        film = app.movie
        before = (film._loaded_key, film.player._open_token)
        bridge.size_bump = 5000
        film.refresh()
        _pump(root, 1.5)
        report["rebuild"] = {"key_changed": film._loaded_key != before[0], "reopened": film.player._open_token > before[1]}
        # закрытие во время открытия: захват освобождается потоком, ничего не остаётся открытым
        import cv2
        opened, released = [], []
        real = cv2.VideoCapture

        class Spy:
            def __init__(self, path):
                self.cap = real(path)
                opened.append(self)

            def isOpened(self):
                import time
                time.sleep(0.4)
                return self.cap.isOpened()

            def release(self):
                released.append(self)
                self.cap.release()

        cv2.VideoCapture = Spy
        try:
            film.player.load(str(projects / PID / "final" / "film.mp4"))
            film.player.close()
            _pump(root, 1.5)
        finally:
            cv2.VideoCapture = real
        report["close_while_opening"] = {"opened": len(opened), "released": len(released), "cap": film.player.cap is None}
        film.error, film.timeline = "сбой", None
        film._render()
        report["left_ready"] = {"key": film._loaded_key, "path": film._loaded_path}
    except Exception:
        report["errors"].append(traceback.format_exc())
    finally:
        for step in (lambda: app._shutdown_shell(), root.destroy):
            try:
                step()
            except Exception:
                pass
    print("WIRING_REPORT " + json.dumps(report, ensure_ascii=False, default=str))
    return 1 if report["errors"] else 0


@pytest.fixture(scope="module")
def scen():
    if shutil.which("xvfb-run") is None:
        pytest.skip("нет xvfb-run")
    try:
        import tkinter  # noqa: F401
    except ImportError:
        pytest.skip("нет tkinter")
    cmd = ["xvfb-run", "-a", "-s", "-screen 0 1700x1000x24", sys.executable, str(Path(__file__)), "--scenarios"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=ROOT)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("WIRING_REPORT ")]
    assert lines, proc.stdout[-2000:] + proc.stderr[-2000:]
    data = json.loads(lines[-1][len("WIRING_REPORT "):])
    assert data["errors"] == []
    return data


def test_json_save_updates_form_content(scen):
    assert scen["json_save"] == {"content_title": "Из JSON", "form_title": ["Из JSON"], "dirty": False}


def test_ctrl_s_applies_field_being_typed(scen):
    assert scen["ctrl_s"] == {"sent": ["Набрано без ухода курсора"], "dirty": False}


def test_project_switch_asks_about_unsaved_files(scen):
    assert scen["switch_project"] == {"selected": [], "dirty": True}


def test_ctrl_k_in_shots_asks_shots_section(scen):
    assert scen["switch_in_shots"] == {"asked": 1, "selected": [], "section": "shots"}


def test_settings_marks_survive_path_check(scen):
    assert "переменной окружения" in scen["marks"]["projects"]
    assert scen["marks"]["backups"]


def test_autosave_out_of_range_shows_banner_without_crash(scen):
    assert scen["autosave_range"]["banner"]
    assert scen["autosave_range"]["updates"] == 0
    assert scen["autosave_range"]["stored"] == 0


def test_scale_not_written_when_server_rejects(scen):
    assert scen["scale_after_error"]["stored"] == 100
    assert scen["scale_after_error"]["banner"]


def test_close_while_opening_releases_capture(scen):
    assert scen["close_while_opening"] == {"opened": 1, "released": 1, "cap": True}


def test_server_accepted_but_window_write_failed_still_reloads_form(scen):
    assert scen["tk_write_fails"] == {"digest": "sd2", "form": "11", "banner": "Окно: сбой записи"}


def test_rebuilt_film_reopens_in_player(scen):
    assert scen["rebuild"] == {"key_changed": True, "reopened": True}
    assert scen["left_ready"] == {"key": None, "path": None}


if __name__ == "__main__":
    sys.exit(_scenarios() if "--scenarios" in sys.argv else _child() if "--child" in sys.argv else 0)
