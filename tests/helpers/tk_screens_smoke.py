"""Дымовая проверка экранов «Фильм», «Болванка», «Файлы», «Настройки» под Xvfb (отдельный процесс).

    xvfb-run -a -s "-screen 0 1800x1000x24" python3 tests/helpers/tk_screens_smoke.py \
        --width 1280 --height 800 --shots /tmp/shots

Вместо сервера подставлен фейковый мост: платных вызовов и реальных запусков нет.
Настройки приложения пишутся во временную папку. Последняя строка вывода - JSON-отчёт (SMOKE_REPORT).
"""

import argparse
import copy
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "helpers"))

from tk_shell_smoke import _make_projects, _pump, find_clipped, shoot  # noqa: E402

PID = "demo_one"
GOLDEN = ROOT / "tests" / "fixtures" / "storybook_screens" / "golden_blockout_vectors.json"


def _timeline(video_exists=True):
    def clip(i, label, start, end, placement="scene", scene=1, music_id=None):
        return {"id": i, "label": label, "start_s": start, "end_s": end, "exists": True, "placement": placement,
                "scene": scene, "path": f"audio/{i}.mp3", "music_id": music_id, "loop": False}
    shots = [{"key": f"1-{n}", "scene": 1, "shot": n, "in_cut": True, "start_s": (n - 1) * 3.0, "end_s": n * 3.0,
              "approximate": n == 2, "warnings": [], "covered_by": None, "unit_span": None} for n in range(1, 5)]
    return {
        "status": "ok", "duration_s": 12.0, "durations_pending": 0, "source": "montage", "warnings": [],
        "shots": shots,
        "tracks": {
            "narration": {"clips": [clip("n1", "Диктор 1", 0, 5), clip("n2", "Диктор 2", 6, 11)]},
            "music": {"clips": [clip("m1", "Лейтмотив", 0, 12, placement="film", scene=None, music_id="m1")]},
            "sfx": {"clips": [clip("s1", "Шаги", 2, 3)]},
        },
        "final": {"stale_hint": False,
                  "video": {"exists": video_exists, "path": "final/film.mp4", "size": 123456, "duration_s": 12.0,
                            "modified": "2026-11-02T10:42:00", "older_than_shots": False},
                  "photo_video": {"exists": False}},
    }


CUES = [{"index": i, "start_s": i * 3.0, "end_s": i * 3.0 + 2.5, "text": t}
        for i, t in enumerate(("Зима пришла рано.", "Лиса вышла из норы.", "Снег скрипел под лапами.", "Конец."))]
ARTIFACTS = [
    {"artifact_key": "brief", "title": "Бриф", "status": "present", "path": "00_brief.json", "editable": True, "exists": True},
    {"artifact_key": "screenplay", "title": "Сценарий", "status": "missing", "path": "01_screenplay.json", "editable": True, "exists": False},
    {"artifact_key": "final_manifest", "title": "Итоговый манифест", "status": "present", "path": "final/manifest.json",
     "editable": False, "exists": True},
]
BRIEF = {"title": "Снег валит не только с неба", "description": "учебный", "genre": "сказка", "target_age": "6-8"}


def _settings_snapshot(tmp):
    def s(value, source="file"):
        return {"value": value, "source": source, "default": value}
    return {"status": "ok", "digest": "sd1", "settings": {
        "projects_directory": s(str(tmp / "projects")), "backup_directory": s(str(tmp / "backups")),
        "logs_directory": s(str(tmp / "logs")), "max_backup_files": s(10), "log_level": s("INFO")},
        "checks": {"projects_directory": {"state": "ok"}}, "warnings": []}


class FakeBridge:
    def __init__(self, tmp, state):
        self.tmp = tmp
        self.state = state
        self.calls = []

    def call(self, cap, payload, on_success=None, on_error=None, latest_only_key=None, **_kw):
        self.calls.append(cap)
        answers = {
            "media_timeline": _timeline(),
            "media_subtitles": {"status": "ok", "cues": CUES, "truncated": False},
            "versions_list": {"status": "ok", "versions": [
                {"id": "v2", "number": 2, "created_at": "2026-11-02T10:00:00", "reason": "regen", "reason_title": "Перегенерация",
                 "surface": "tk", "files": []},
                {"id": "v1", "number": 1, "created_at": "2026-11-01T09:00:00", "reason": "first", "reason_title": "Первая сборка",
                 "surface": "tk", "files": []}]},
            "regenerate_preview": {"status": "ok", "can_start": True, "plan_digest": "p1", "problems": [],
                                   "confirm_required": {"cost_rub": 30.0}},
            "regenerate_start": {"status": "ok"},
            "blockout_state": self.state,
            "blockout_detail": {"status": "ok"},
            "artifacts_list": {"status": "ok", "items": ARTIFACTS},
            "artifact_read": {"status": "ok", "title": "Бриф", "path": "00_brief.json", "digest": "a1", "exists": True,
                              "editable": True, "content": BRIEF},
            "artifact_validate": {"status": "ok", "valid": True, "findings": [], "counts": {"errors": 0, "warnings": 0}},
            "app_settings_get": _settings_snapshot(self.tmp),
            "app_settings_check_path": {"status": "ok", "state": "ok"},
        }
        result = copy.deepcopy(answers.get(cap, {"status": "ok"}))
        result.setdefault("status", "ok")
        if result["status"] == "ok":
            if on_success:
                on_success(result)
        elif on_error:
            on_error(result)

    def close(self):
        pass


def _make_video(path: Path):
    import cv2
    import numpy as np
    path.parent.mkdir(parents=True, exist_ok=True)
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 180))
    for i in range(120):
        frame = np.full((180, 320, 3), (i * 2 % 255, 90, 160), dtype=np.uint8)
        out.write(frame)
    out.release()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--shots", default="")
    ap.add_argument("--tag", default="screens")
    ap.add_argument("--scale", type=int, default=100, help="масштаб интерфейса, %%")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="sbm_scr_"))
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
    app_settings.set("window_geometry", f"{args.width}x{args.height}")
    app_settings.set("ui_scale", args.scale)
    app_settings.set("auto_save_interval", 0)

    report = {"size": f"{args.width}x{args.height}", "errors": [], "clipped": {}, "checks": {}}
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    from StoryBookManager.gui.main_window import MainWindow
    root = tk.Tk()
    app = None
    try:
        state = json.loads(GOLDEN.read_text(encoding="utf-8"))["states"]["base"]
        state = dict(state, status="ok")
        app = MainWindow(root)
        root.geometry(f"{args.width}x{args.height}+0+0")
        root.update()
        fake = FakeBridge(tmp, state)
        app.bridge.call = fake.call
        for name in ("movie", "blockout_panel", "editor_panel", "settings_screen"):
            screen = getattr(app, name)
            for attr in ("ask_confirm",):
                if hasattr(screen, attr):
                    setattr(screen, attr, lambda spec: True)

        def snap(name):
            _pump(root, 0.5)
            report["clipped"][name] = find_clipped(root)
            if shots:
                shoot(root, shots / f"{args.tag}_{args.width}x{args.height}_{name}.png")

        app.on_project_selected(app.project_manager.load_project(PID))
        app.show_section("movie")
        _pump(root, 2.0)
        film = app.movie
        report["checks"]["film"] = {"timeline": film.timeline is not None, "blocks": len(film.layout_data["blocks"]),
                                    "cues": len(film.cues), "video_loaded": bool(film._loaded_path)}
        snap("film")
        film.player.seek(5.0)
        film.tabs.select(2)
        snap("film_music")
        film.tabs.select(3)
        _pump(root, 0.5)
        film.regen_music()
        _pump(root, 1.0)
        report["checks"]["music_start"] = "regenerate_start" in fake.calls

        app.show_section("blockout")
        _pump(root, 2.0)
        inner = app.blockout_panel.tree.tree

        def count(node=""):
            return sum(1 + count(child) for child in inner.get_children(node))

        report["checks"]["blockout"] = {"state": app.blockout_panel.state is not None, "rows": count()}
        if report["checks"]["blockout"]["rows"] < 1:
            report["errors"].append("дерево болванки пустое")
        panel = app.blockout_panel
        panel.tree.select_keys(["1-1"])
        _pump(root, 1.0)
        root.update_idletasks()
        bottom = root.winfo_rooty() + root.winfo_height()
        hidden = [a for a, btn in panel.buttons.items()
                  if not btn.winfo_viewable() or btn.winfo_rooty() + btn.winfo_height() > bottom]
        report["checks"]["blockout_actions_hidden"] = hidden
        if hidden:
            report["errors"].append(f"кнопки действий болванки не видны: {hidden}")
        # правая колонка прокручивается: каждая плитка и «Замечания» должны быть видны вверху или после прокрутки вниз
        scroll = panel.right_scroll
        targets = {**{f"плитка {n}": lab.master for n, lab in panel.compare.tiles.items()}, "Замечания": panel.notes}
        reached = set()
        for step in range(0, 11):
            scroll.canvas.yview_moveto(step / 10)
            _pump(root, 0.1)
            reached |= {n for n, w in targets.items() if scroll.visible(w)}
        scroll.canvas.yview_moveto(0.0)
        seen_top = reached
        unreachable = sorted(set(targets) - reached)
        report["checks"]["blockout_unreachable"] = unreachable
        if unreachable:
            report["errors"].append(f"не достижимы в правой колонке болванки: {unreachable}")
        scroll.canvas.yview_moveto(0.0)
        snap("blockout")

        app.show_section("files")
        _pump(root, 1.5)
        files = app.editor_panel
        report["checks"]["files_rows"] = len(files.rows)
        files.tree.selection_set("brief")
        files._on_tree_select(None)
        _pump(root, 1.5)
        report["checks"]["files_current"] = files.current
        snap("files")

        app.show_section("settings")
        _pump(root, 1.5)
        report["checks"]["settings_loaded"] = app.settings_screen.snapshot is not None
        snap("settings")
    except Exception:
        report["errors"].append(traceback.format_exc())
    finally:
        try:
            if app is not None:
                app._shutdown_shell()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
    print("SMOKE_REPORT " + json.dumps(report, ensure_ascii=False, default=str))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
