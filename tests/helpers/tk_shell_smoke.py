"""Дымовая проверка оболочки Tk под Xvfb (запускается отдельным процессом).

Пример:
    xvfb-run -a -s "-screen 0 1600x1000x24" python3 tests/helpers/tk_shell_smoke.py \
        --width 1280 --height 800 --scale 100 --shots /tmp/shots

Печатает последней строкой JSON-отчёт: разделы, обрезанные виджеты, ошибки.
Работает и со старой оболочкой (вкладки, режим --before), и с новой.
Настройки приложения меняются только в памяти, на диск ничего не пишется.
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

OLD_TABS = ["projects", "editor", "media", "generation", "blockout"]
CLIP_CLASSES = ("TButton", "Button", "TLabel", "Label", "TCheckbutton", "Checkbutton", "TEntry", "Entry", "TCombobox")


def _make_projects(base: Path) -> None:
    import json as _json
    for pid, title in (("demo_one", "Снег валит не только с неба"), ("demo_two", "Второй учебный проект")):
        d = base / pid
        d.mkdir(parents=True, exist_ok=True)
        (d / "00_brief.json").write_text(
            _json.dumps({"title": title, "description": "учебный", "genre": "сказка", "target_age": "6-8"},
                        ensure_ascii=False), encoding="utf-8")


def find_clipped(root, limit=40):
    """Виджеты, у которых выделенная ширина меньше нужной, или правый край за окном."""
    out = []
    root.update_idletasks()
    win_w = root.winfo_width()
    stack = [root]
    while stack:
        w = stack.pop()
        try:
            children = w.winfo_children()
        except Exception:
            children = []
        stack.extend(children)
        try:
            if w.winfo_class() not in CLIP_CLASSES or not w.winfo_viewable():
                continue
            width, req = w.winfo_width(), w.winfo_reqwidth()
            right = w.winfo_rootx() - root.winfo_rootx() + width
            if width < req - 1 or right > win_w + 1:
                try:
                    text = str(w.cget("text"))
                except Exception:
                    text = ""
                out.append({"cls": w.winfo_class(), "path": str(w), "text": text[:40],
                            "width": width, "req": req, "right": right, "win": win_w})
        except Exception:
            continue
    return out[:limit]


def _pump(root, seconds: float) -> None:
    """Дать фоновым вызовам и таймерам отработать."""
    import time
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def u2_scenarios(app, root, snap) -> None:
    """Экраны «Проекты» и «Обзор»: заполненный, пустой, ошибочный список и состояния обзора."""
    from StoryBookManager.gui.sections import overview_logic as O
    app.show_section("projects")
    screen = app.project_panel
    _pump(root, 1.5)
    snap("projects_filled")
    ctl = screen.ctl
    saved = (ctl.cards, ctl.total, ctl.error)
    ctl.cards, ctl.total, ctl.error = [], 0, None
    screen.list_updated()
    snap("projects_empty")
    ctl.cards, ctl.total, ctl.error = saved[0], saved[1], {"code": "not_found", "message": "Папка проектов не найдена: /x"}
    screen.list_updated()
    snap("projects_error")
    ctl.cards, ctl.total, ctl.error = saved
    screen.query_var.set("zzz")
    _pump(root, 0.4)   # поиск применяется с задержкой 280 мс
    snap("projects_nothing_found")
    screen.query_var.set("")
    ctl.error = None
    screen.list_updated()

    app.show_section("overview")
    status = {
        "project": {"id": "demo_one", "title": "Снег валит не только с неба"},
        "active_run": {"state": "running", "text": "Идёт прогон: шаг 3 из 12"},
        "next_action": {"title": "Проверьте кадры", "detail": "Часть кадров ещё без изображений.",
                        "severity": "warning", "target": {"screen": "shots"}},
    }
    view = O.build_overview(status)
    app.overview.update_view(view)
    snap("overview_ready")
    app.overview.update_view(O.with_error(view, {"message": "Нет связи"}))
    snap("overview_stale")
    app.overview.update_view(O.loading_view())
    snap("overview_loading")
    app.overview.update_view(O.with_error(None, {"code": "not_found", "message": "нет"}))
    snap("overview_gone")
    app.overview.update_view(O.no_project_view())
    snap("overview_no_project")
    app.overview.update_view(view)
    app.show_section("projects")


def shoot(root, path: Path) -> bool:
    try:
        from PIL import ImageGrab
        root.update()
        x, y = root.winfo_rootx(), root.winfo_rooty()
        img = ImageGrab.grab(bbox=(x, y, x + root.winfo_width(), y + root.winfo_height()),
                             xdisplay=os.environ.get("DISPLAY"))
        img.save(path)
        return True
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--scale", type=int, default=100)
    ap.add_argument("--shots", default="")
    ap.add_argument("--tag", default="after")
    ap.add_argument("--before", action="store_true", help="старая оболочка с вкладками")
    ap.add_argument("--toasts", action="store_true", help="показать тосты и диалог")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="sbm_smoke_"))
    projects = tmp / "projects"
    projects.mkdir()
    _make_projects(projects)
    os.environ["STORYBOOK_PROJECTS_DIR"] = str(projects)

    import tkinter as tk
    from StoryBookManager.config.settings import app_settings
    app_settings.set("projects_directory", str(projects))
    app_settings.set("backup_directory", str(tmp / "backups"))
    app_settings.set("logs_directory", str(tmp / "logs"))
    app_settings.set("window_geometry", f"{args.width}x{args.height}")
    app_settings.set("ui_scale", args.scale)
    app_settings.set("auto_save_interval", 0)

    report = {"tag": args.tag, "size": f"{args.width}x{args.height}", "scale": args.scale,
              "sections": {}, "errors": [], "clipped": {}}
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    from StoryBookManager.gui.main_window import MainWindow
    root = tk.Tk()
    try:
        app = MainWindow(root)
        root.geometry(f"{args.width}x{args.height}+0+0")
        root.update()
        new_shell = hasattr(app, "show_section")
        report["new_shell"] = new_shell

        def snap(name):
            root.update()
            clipped = find_clipped(root)
            report["clipped"][name] = clipped
            if shots:
                shoot(root, shots / f"{args.tag}_{args.width}x{args.height}_s{args.scale}_{name}.png")

        snap("no_project_projects")
        if new_shell:
            from StoryBookManager.gui.shell.sections import SECTIONS
            report["nav_items"] = len(SECTIONS)
            # без проекта: разделы 2-7 не открываются
            blocked = [s.id for s in SECTIONS if s.needs_project and app.show_section(s.id)]
            report["opened_without_project"] = blocked
        project = app.project_manager.load_project("demo_one")
        app.on_project_selected(project)
        root.update()
        if new_shell:
            report["landing"] = app.host.current if hasattr(app.host, "current") else None
            for s in SECTIONS:
                try:
                    ok = app.show_section(s.id)
                    root.update()
                    report["sections"][s.id] = bool(ok)
                    snap(s.id)
                except Exception:
                    report["errors"].append(f"{s.id}: {traceback.format_exc()}")
            try:
                u2_scenarios(app, root, snap)
            except Exception:
                report["errors"].append("u2: " + traceback.format_exc())
            if args.toasts:
                from StoryBookManager.gui.shell import notify
                notify.success("Проект создан")
                notify.info("Сообщение для проверки")
                notify.warning("Предупреждение для проверки")
                notify.error("Ошибка для проверки: что случилось и что делать", details="подробности")
                root.update()
                snap("toasts")
        else:
            for i, name in enumerate(OLD_TABS):
                try:
                    app.notebook.select(i)
                    root.update()
                    report["sections"][name] = True
                    snap(name)
                except Exception:
                    report["errors"].append(f"{name}: {traceback.format_exc()}")
        report["minsize"] = list(root.minsize())
        root.update()
        try:
            app.root.update_idletasks()
        except Exception:
            pass
    except Exception:
        report["errors"].append(traceback.format_exc())
    finally:
        try:
            bridge = getattr(app, "bridge", None)
            if bridge is not None:
                bridge.close()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
    print("SMOKE_REPORT " + json.dumps(report, ensure_ascii=False))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
