"""Дымовая проверка экрана «Кадры» под Xvfb (отдельный процесс).

    xvfb-run -a -s "-screen 0 1700x1000x24" python3 tests/helpers/tk_shots_smoke.py \
        --width 1280 --height 800 --shots /tmp/shots

Проект на 250 шотов создаётся во временной папке. Платных вызовов нет.
Последняя строка вывода - JSON-отчёт (SMOKE_REPORT).
"""

import argparse
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "helpers"))

from tk_shell_smoke import _pump, find_clipped, shoot  # noqa: E402

PID = "demo_shots"
SHOTS = 250


def make_project(base: Path, count: int = SHOTS) -> dict:
    from PIL import Image

    from custom_tools.storybook.studio import project_store, series

    project_id = project_store.create_project(
        base, title="Кадры: проверка", description="учебный проект для проверки", genre="сказка",
        target_age="6-8", id_hint=PID,
    )
    episode = series.series_create(project_id, "Episode", "Story", [], {})["series"]
    root = base / project_id / "series" / episode["series_id"]
    items = []
    for n in range(count):
        scene, shot = n // 5 + 1, n % 5 + 1
        d = root / "97_shots" / f"scene_{scene:02d}_shot_{shot:02d}"
        d.mkdir(parents=True, exist_ok=True)
        for phase in ("start", "end"):
            rel = f"plots/storybooks/{PID}/series/{episode['series_id']}/97_shots/scene_{scene:02d}_shot_{shot:02d}/img_final_{phase}_{scene:02d}_{shot:02d}.png"
            items.append({"scene_number": scene, "shot_number": shot, "shot_type": phase, "timing": "00:05",
                          "english_prompt": f"prompt {phase} {scene}-{shot} " + "слово " * 8,
                          "negative_prompt": "neg", "video_prompt": "move" if phase == "start" else "",
                          "output_path": rel, "image_path": None, "reference_image_paths": [],
                          "width": 64, "height": 64, "seed": 1})
            if n % 7 != 3:   # часть картинок отсутствует
                Image.new("RGB", (96, 64), ((n * 13) % 255, (n * 7) % 255, 120)).save(
                    d / f"img_final_{phase}_{scene:02d}_{shot:02d}.png")
    (root / "97_shots" / "shots.json").write_text(
        json.dumps({"seed": 7, "consistency_rules": ["a"], "items": items}, ensure_ascii=False), encoding="utf-8")
    return episode


def canvas_items(root) -> int:
    best = 0
    stack = [root]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if w.winfo_class() == "Canvas":
            try:
                best = max(best, len(w.find_all()))
            except Exception:
                pass
    return best


def key(root, widget, sequence):
    widget.focus_force()
    root.update()
    widget.event_generate(sequence)
    root.update()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--scale", type=int, default=100)
    ap.add_argument("--shots", default="")
    ap.add_argument("--tag", default="shots")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="sbm_shots_"))
    projects = tmp / "projects"
    projects.mkdir()
    os.environ["STORYBOOK_PROJECTS_DIR"] = str(projects)
    episode = make_project(projects)

    import tkinter as tk
    from StoryBookManager.config.settings import app_settings
    # настоящий settings.json не трогаем: настройки пишутся во временную папку
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
        app = MainWindow(root)
        root.geometry(f"{args.width}x{args.height}+0+0")
        root.update()

        def snap(name):
            _pump(root, 0.4)
            report["clipped"][name] = find_clipped(root)
            if shots:
                shoot(root, shots / f"{args.tag}_{args.width}x{args.height}_{name}.png")

        app.on_series_selected(app.project_manager.load_project(PID), episode)
        app.show_section("shots")
        _pump(root, 2.5)
        ctl = app.shots.ctl
        report["checks"]["loaded"] = [ctl.load_state, len(ctl.items), ctl.total]
        report["checks"]["selected"] = ctl.selected
        report["checks"]["canvas_items"] = canvas_items(app.shots.list_view)
        snap("list")
        lv = app.shots.list_view
        lv.canvas.event_generate("<MouseWheel>", delta=-120)
        for _ in range(10):
            lv.canvas.event_generate("<Button-5>")
            root.update()
        _pump(root, 1)
        report["checks"]["scrolled_top"] = lv._top
        report["checks"]["canvas_items_after_scroll"] = canvas_items(lv)
        snap("scrolled")
        before = ctl.selected
        key(root, lv.canvas, "<Down>")
        report["checks"]["down"] = [before, ctl.selected, lv._top]
        _pump(root, 1)
        snap("card")
        key(root, lv.canvas, "<a>")
        _pump(root, 1)
        report["checks"]["approved"] = (ctl.row_of(ctl.selected) or {}).get("review")
        key(root, lv.canvas, "<r>")
        _pump(root, 1)
        report["checks"]["redo"] = (ctl.entry or {}).get("status")
        ctl.set_mode("grid")
        _pump(root, 1)
        report["checks"]["canvas_items_grid"] = canvas_items(lv)
        snap("grid")
        ctl.set_mode("list")
        from StoryBookManager.gui.shots import model as M
        ctl.set_filters(M.ListFilters(state="missing"))
        _pump(root, 1.5)
        report["checks"]["filter_missing"] = [ctl.load_state, ctl.found_count]
        snap("filtered")
        ctl.set_filters(M.ListFilters(query="no-such-text-xyz"))
        _pump(root, 1.5)
        snap("empty_filter")
        ctl.reset_filters()
        _pump(root, 1.5)
        ctl.toggle_mark("1-1")
        ctl.toggle_mark("1-2")
        snap("marked")
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
