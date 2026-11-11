"""Логика проектов studio без реестра: папки передаются параметрами."""

import hashlib
import json
import os
import shutil
import threading
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from tests.studio_run_fakes import make_series_project

from custom_tools.storybook.studio import project_store as ps
from custom_tools.storybook.studio.errors import StudioError


@pytest.fixture
def dirs(tmp_path):
    root, backups = tmp_path / "projects", tmp_path / "backups"
    root.mkdir()
    backups.mkdir()
    return root, backups


def make_project(root, name="alpha", files=None):
    base = root / name
    make_series_project(root, name)
    (base / "project.json").write_text(json.dumps({"format": "storybook-series-v1", "project_id": name, "title": "Альфа", "description": "d" * 12, "resources": {}}), encoding="utf-8")
    for rel, text in (files or {"series/main/20_story/story.json": json.dumps({"pages": [1, 2]}), "data.txt": "hello"}).items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return base


def tree_hashes(path):
    return {
        p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(path.rglob("*")) if p.is_file() and not p.is_symlink()
    }


# ---- список / служебные папки

def test_list_hides_service_and_outside_links(dirs, tmp_path):
    root, _ = dirs
    for name in ("_shared_internal", ".hidden", ".studio_jobs", "real", "bare"):
        (root / name).mkdir()
    (root / "file.txt").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    make_series_project(root, "real")
    names = sorted(p.name for p in ps.list_project_dirs(root))
    assert names == ["real"]
    assert ps.list_project_dirs(root / "missing") == []


# ---- id

def test_suggest_id_translit_and_rules(dirs):
    root, _ = dirs
    assert ps.suggest_project_id("Снег валит не только с неба", root) == "sneg_valit_ne_tolko_s_neba"
    assert not ps.suggest_project_id("_x", root).startswith("_")
    assert ps.suggest_project_id("", root) == "storybook"
    assert len(ps.suggest_project_id("a" * 200, root)) <= ps.MAX_ID_LEN
    (root / "fox").mkdir()
    taken = ps.suggest_project_id("Fox", root)
    assert taken.startswith("fox_") and taken != "fox"


def test_reserve_is_atomic_for_parallel_calls(dirs):
    root, _ = dirs
    got, lock = [], threading.Lock()

    def worker():
        value = ps.suggest_project_id("same", root, reserve=True)
        with lock:
            got.append(value)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(set(got)) == 8
    assert all((root / g).is_dir() for g in got)


# ---- обложка

def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"png")


def test_pick_cover_priorities(tmp_path):
    p = tmp_path / "p"
    p.mkdir()
    assert ps.pick_cover(p) is None
    touch(p / "20_bible/references/characters/hero__backup_1.png")
    touch(p / "20_bible/references/characters/.hidden.png")
    assert ps.pick_cover(p) is None
    touch(p / "20_bible/references/characters/hero.png")
    assert ps.pick_cover(p) == "20_bible/references/characters/hero.png"
    touch(p / "50_images/loose.png")
    assert ps.pick_cover(p) == "50_images/loose.png"
    touch(p / "50_images/page_10/b.png")
    touch(p / "50_images/page_10/a.png")
    assert ps.pick_cover(p) == "50_images/page_10/a.png"
    touch(p / "50_images/page_10/img_final.png")
    touch(p / "50_images/page_2/img_final.webp")
    assert ps.pick_cover(p) == "50_images/page_2/img_final.webp"


def test_pick_cover_skips_link_outside(tmp_path):
    p = tmp_path / "p"
    (p / "50_images/page_1").mkdir(parents=True)
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"x")
    (p / "50_images/page_1/img_final.png").symlink_to(outside)
    assert ps.pick_cover(p) is None


# ---- этап

STAGE_FILES = [
    ("brief", {"00_brief.json": "{}"}),
    ("plot", {"10_synopsis/synopsis.json": "{}"}),
    ("world", {"30_style/style_text.json": "{}"}),
    ("text", {"20_story/story.json": '{"pages": []}'}),
    ("illustrations", {"50_images/page_1/img_final.png": "x"}),
    ("book", {"95_pdf/book.pdf": "x"}),
    ("screenplay", {"91_screenplay/screenplay.json": "{}"}),
    ("shots", {"97_shots/shots.json": "{}"}),
    ("video", {"97_shots/shot_01/v.mp4": "x"}),
    ("audio", {"98_audio/music_manifest.json": "{}"}),
    ("film", {"99_final/final_video.mp4": "x"}),
]


@pytest.mark.parametrize("stage_id,files", STAGE_FILES)
def test_stage_each(tmp_path, stage_id, files):
    for rel, text in files.items():
        touch(tmp_path / rel)
        (tmp_path / rel).write_text(text)
    assert ps.detect_stage(tmp_path)["id"] == stage_id


def test_stage_skips_and_broken_json(tmp_path):
    assert ps.detect_stage(tmp_path)["id"] == "empty"
    (tmp_path / "97_shots").mkdir()
    (tmp_path / "97_shots/shots.json").write_text("{}")
    assert ps.detect_stage(tmp_path)["id"] == "shots"
    (tmp_path / "20_story").mkdir()
    (tmp_path / "20_story/story.json").write_text("{broken")
    assert ps.detect_stage(tmp_path)["id"] == "shots"


def test_modified_follows_deep_write(tmp_path):
    (tmp_path / "a/b").mkdir(parents=True)
    os.utime(tmp_path / "a", (1000, 1000))
    os.utime(tmp_path, (1000, 1000))
    assert ps.modified_time(tmp_path) == 1000
    (tmp_path / "a/b/f.txt").write_text("x")
    os.utime(tmp_path / "a", (5000, 5000))
    assert ps.modified_time(tmp_path) == 5000


# ---- создание и проверки формы

GOOD = dict(title="Лис", description="Добрая история про лиса", genre="сказка", target_age="3-5")


@pytest.mark.parametrize("patch,field", [
    ({"title": "а"}, "Название"), ({"description": "коротко"}, "Описание"), ({"genre": " "}, "Жанр"),
    ({"target_age": ""}, "Возраст"), ({"language": "de"}, "Язык"), ({"pages_min": 0}, "Страниц от"),
    ({"pages_min": 5, "pages_max": 4}, "Страниц до"), ({"words_per_page_min": 10}, "Слов на страницу от"),
    ({"words_per_page_min": 100, "words_per_page_max": 90}, "Слов на страницу до"), ({"pages_min": "9"}, "Страниц от"),
])
def test_validate_new_project_names_field(patch, field):
    with pytest.raises(StudioError) as err:
        ps.validate_new_project(**{**GOOD, **patch})
    assert err.value.code == "invalid_argument" and field in err.value.message


def test_create_project_writes_brief_and_structure(dirs):
    root, _ = dirs
    pid = ps.create_project(root, **GOOD, id_hint="Мой проект")
    assert pid == "moy_proekt"
    project = json.loads((root / pid / "project.json").read_text(encoding="utf-8"))
    assert project["format"] == "storybook-series-v1" and project["title"] == "Лис"
    assert project["description"] == GOOD["description"] and project["project_id"] == pid
    assert (root / pid / "shared").is_dir() and (root / pid / "series").is_dir()


# ---- бэкап

def test_backup_name_meta_and_exclusions(dirs):
    root, backups = dirs
    base = make_project(root)
    (base / ".pipeline.lock").write_text("")
    touch(base / ".studio/thumbs/a.png")
    touch(base / ".studio/exports/old.zip")
    touch(base / ".studio/control.json")
    touch(base / ".studio/keep.json")
    (base / "link").symlink_to("/etc/hostname")
    fixed = lambda: datetime(2026, 1, 2, 3, 4, 5)
    first = ps.create_backup(root, backups, "alpha", reason="manual", note="n", now=fixed)
    second = ps.create_backup(root, backups, "alpha", now=fixed)
    assert first["backup_id"] == "alpha_backup_20260102_030405"
    assert second["backup_id"] == "alpha_backup_20260102_030405_2"
    copy = backups / first["backup_id"]
    assert not (copy / ".pipeline.lock").exists()
    assert not (copy / ".studio/thumbs").exists() and not (copy / ".studio/exports").exists()
    assert not (copy / ".studio/control.json").exists() and (copy / ".studio/keep.json").exists()
    assert (copy / "link").is_symlink() and os.readlink(copy / "link") == "/etc/hostname"
    meta = json.loads((backups / ".meta" / f"{first['backup_id']}.json").read_text(encoding="utf-8"))
    assert meta["reason"] == "manual" and meta["note"] == "n" and meta["source"] == "studio"
    assert not [p for p in backups.iterdir() if p.name.startswith(".tmp")]


def test_backup_low_disk_and_incomplete_copy_leave_no_trace(dirs, monkeypatch):
    root, backups = dirs
    make_project(root)
    monkeypatch.setattr(ps.shutil, "disk_usage", lambda p: type("U", (), {"free": 1})())
    with pytest.raises(StudioError) as err:
        ps.create_backup(root, backups, "alpha")
    assert err.value.code == "unavailable"
    monkeypatch.undo()
    real = ps.shutil.copy2

    def broken(src, dst, *a, **k):
        real(src, dst)
        Path(dst).write_text("cut")

    monkeypatch.setattr(ps.shutil, "copy2", broken)
    with pytest.raises(StudioError):
        ps.create_backup(root, backups, "alpha")
    assert [p.name for p in backups.iterdir() if p.name != ".meta"] == []


def test_cleanup_keeps_newest_protected_and_before_delete(dirs):
    root, backups = dirs
    make_project(root)
    for i in range(1, 6):
        ps.create_backup(root, backups, "alpha", max_backups=100, now=lambda i=i: datetime(2026, 1, 1, 0, 0, i))
    (backups / "files").mkdir()
    (backups / "other_backup_20260101_000000").mkdir()
    names = lambda: sorted(b["backup_id"] for b in ps.list_backups(root, backups, "alpha"))
    ps.cleanup_backups(backups, "alpha", 2, protect=("alpha_backup_20260101_000001",))
    assert names() == ["alpha_backup_20260101_000001", "alpha_backup_20260101_000004", "alpha_backup_20260101_000005"]
    assert (backups / "files").is_dir() and (backups / "other_backup_20260101_000000").is_dir()
    ps.create_backup(root, backups, "alpha", reason="before_delete", max_backups=1,
                     now=lambda: datetime(2026, 1, 1, 0, 0, 0))
    assert "alpha_backup_20260101_000000" in names()


def test_list_backups_unknown_reason_and_deleted_project(dirs):
    root, backups = dirs
    old = backups / "gone_backup_20250101_101010"
    old.mkdir()
    (old / "f.txt").write_text("12345")
    rows = ps.list_backups(root, backups)
    assert rows[0]["reason"] == "unknown" and rows[0]["reason_title"] == "Неизвестно"
    assert rows[0]["size_bytes"] == 5 and rows[0]["project_exists"] is False
    assert ps.list_backups(root, backups, "alpha") == []


# ---- восстановление

def test_restore_success_creates_safety_backup(dirs):
    root, backups = dirs
    base = make_project(root)
    saved = ps.create_backup(root, backups, "alpha", now=lambda: datetime(2026, 1, 1, 0, 0, 1))
    before = tree_hashes(base)
    (base / "data.txt").write_text("changed")
    (base / "new.txt").write_text("n")
    result = ps.restore_backup(root, backups, "alpha", saved["backup_id"])
    restored = tree_hashes(root / "alpha")
    restored.pop(ps.LOCK_NAME, None)  # текущий служебный замок сохраняется при замене каталога
    assert restored == before
    safety = backups / result["safety_backup_id"]
    assert (safety / "data.txt").read_text() == "changed"
    meta = json.loads((backups / ".meta" / f"{safety.name}.json").read_text(encoding="utf-8"))
    assert meta["reason"] == "before_restore"
    assert not [p for p in root.iterdir() if p.name.startswith((".restore", ".old"))]


def test_restore_after_delete_recreates(dirs):
    root, backups = dirs
    base = make_project(root)
    before = tree_hashes(base)
    deleted = ps.delete_project(root, backups, "alpha")
    assert not base.exists()
    result = ps.restore_backup(root, backups, "alpha", deleted["backup_id"])
    assert result["safety_backup_id"] is None and tree_hashes(root / "alpha") == before


@pytest.mark.parametrize("bad,code", [
    ("../x", "invalid_argument"), ("a/b", "invalid_argument"), ("other_backup_20260101_000000", "invalid_argument"),
    ("alpha_backup_20990101_000000", "not_found"), ("", "invalid_argument"),
])
def test_restore_rejects_bad_backup_id(dirs, bad, code):
    root, backups = dirs
    make_project(root)
    (backups / "other_backup_20260101_000000").mkdir()
    with pytest.raises(StudioError) as err:
        ps.restore_backup(root, backups, "alpha", bad)
    assert err.value.code == code


def test_restore_rejects_symlink_backup(dirs, tmp_path):
    root, backups = dirs
    make_project(root)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (backups / "alpha_backup_20260101_000000").symlink_to(target, target_is_directory=True)
    with pytest.raises(StudioError) as err:
        ps.restore_backup(root, backups, "alpha", "alpha_backup_20260101_000000")
    assert err.value.code == "not_found"


def test_restore_locked_keeps_project(dirs):
    root, backups = dirs
    base = make_project(root)
    saved = ps.create_backup(root, backups, "alpha")
    (base / "data.txt").write_text("changed")
    handle = ps.try_lock(base)
    try:
        with pytest.raises(StudioError) as err:
            ps.restore_backup(root, backups, "alpha", saved["backup_id"])
    finally:
        ps.unlock(handle)
    assert err.value.code == "locked" and (base / "data.txt").read_text() == "changed"


@pytest.mark.parametrize("stage", ["safety", "copy", "second_rename"])
def test_restore_failures_leave_project_intact(dirs, monkeypatch, stage):
    root, backups = dirs
    base = make_project(root)
    saved = ps.create_backup(root, backups, "alpha", now=lambda: datetime(2026, 1, 1, 0, 0, 1))
    (base / "data.txt").write_text("changed")
    if stage == "safety":
        real = ps.create_backup
        monkeypatch.setattr(ps, "create_backup", lambda *a, **k: (_ for _ in ()).throw(StudioError("unavailable", "x")))
    elif stage == "copy":
        monkeypatch.setattr(ps.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    else:
        real_rename, calls = os.rename, []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) == 3 or (len(calls) == 2 and str(dst) == str(base)):
                raise OSError("rename")
            real_rename(src, dst)

        monkeypatch.setattr(ps.os, "rename", flaky)
    with pytest.raises((StudioError, OSError)):
        ps.restore_backup(root, backups, "alpha", saved["backup_id"])
    monkeypatch.undo()
    assert (base / "data.txt").read_text() == "changed"
    assert not [p for p in root.iterdir() if p.name.startswith((".restore", ".old"))]


# ---- удаление

def test_delete_backup_before_rmtree_and_failures(dirs, monkeypatch):
    root, backups = dirs
    base = make_project(root)
    monkeypatch.setattr(ps, "create_backup", lambda *a, **k: (_ for _ in ()).throw(StudioError("unavailable", "нет места")))
    with pytest.raises(StudioError):
        ps.delete_project(root, backups, "alpha")
    assert base.is_dir()
    monkeypatch.undo()
    handle = ps.try_lock(base)
    try:
        with pytest.raises(StudioError) as err:
            ps.delete_project(root, backups, "alpha")
    finally:
        ps.unlock(handle)
    assert err.value.code == "locked" and base.is_dir()
    result = ps.delete_project(root, backups, "alpha")
    assert not base.exists() and (backups / result["backup_id"] / "data.txt").exists()
    other = make_project(root, "beta")
    done = ps.delete_project(root, backups, "beta")
    assert not other.exists() and (backups / done["backup_id"]).is_dir()


# ---- копия, переименование

def test_duplicate_excludes_and_titles_copy(dirs):
    root, _ = dirs
    base = make_project(root)
    (base / ".pipeline.lock").write_text("")
    touch(base / ".studio/thumbs/a.png")
    new_id = ps.suggest_project_id("alpha_copy", root, reserve=True)
    result = ps.duplicate_project(root, "alpha", new_id)
    copy = root / new_id
    assert result == {"new_project_id": new_id, "source_busy": False}
    assert json.loads((copy / "project.json").read_text(encoding="utf-8"))["title"] == "Альфа (копия)"
    assert not (copy / ".pipeline.lock").exists() and not (copy / ".studio/thumbs").exists()
    assert json.loads((base / "project.json").read_text(encoding="utf-8"))["title"] == "Альфа"


def test_rename_changes_only_title_atomically(dirs, monkeypatch):
    root, _ = dirs
    base = make_project(root)
    assert ps.rename_project(root, "alpha", "  Новое имя ") == "Альфа"
    brief = json.loads((base / "project.json").read_text(encoding="utf-8"))
    assert brief == {"format": "storybook-series-v1", "project_id": "alpha", "title": "Новое имя", "description": "d" * 12, "resources": {}}
    monkeypatch.setattr(ps.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(OSError):
        ps.rename_project(root, "alpha", "Ещё")
    monkeypatch.undo()
    assert json.loads((base / "project.json").read_text(encoding="utf-8"))["title"] == "Новое имя"
    with pytest.raises(StudioError):
        ps.rename_project(root, "alpha", "x")


# ---- экспорт и размер

def test_export_zip_contents_progress_and_empty(dirs, tmp_path):
    root, _ = dirs
    base = make_project(root)
    (base / ".pipeline.lock").write_text("")
    touch(base / ".studio/exports/old.zip")
    (base / "link").symlink_to("/etc/hostname")
    calls = []
    info = ps.export_zip(base, tmp_path / "out.zip", lambda c, t, m: calls.append((c, t)))
    with zipfile.ZipFile(tmp_path / "out.zip") as zf:
        names = set(zf.namelist())
    assert names == {"project.json", "series/main/series.json", "series/main/20_story/story.json", "data.txt"}
    assert calls[-1] == (4, 4) and info["files_count"] == 4
    empty = root / "empty"
    empty.mkdir()
    with pytest.raises(StudioError) as err:
        ps.export_zip(empty, tmp_path / "e.zip")
    assert err.value.code == "invalid_argument"


def test_export_for_download_keeps_two(dirs):
    root, _ = dirs
    base = make_project(root)
    for sec in range(1, 5):
        ps.export_for_download(base, now=lambda s=sec: datetime(2026, 1, 1, 0, 0, s))
    assert sorted(p.name for p in (base / ".studio/exports").glob("*.zip")) == [
        "alpha_20260101_000003.zip", "alpha_20260101_000004.zip"]


def test_dir_size_ignores_links_and_cache(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a/f.txt").write_text("12345")
    (tmp_path / "a/loop").symlink_to(tmp_path / "a", target_is_directory=True)
    ps.invalidate_size_cache()
    first = ps.dir_size(tmp_path)
    assert first == {"size_bytes": 5, "files_count": 1, "cached": False}
    (tmp_path / "a/g.txt").write_text("1")
    assert ps.dir_size(tmp_path)["cached"] is True and ps.dir_size(tmp_path)["size_bytes"] == 5
    ps.invalidate_size_cache()
    assert ps.dir_size(tmp_path)["size_bytes"] == 6


# ---- копирование во время прогона

def test_copy_tolerates_vanished_and_grown_files(dirs, monkeypatch):
    """Бэкап без блокировки: исчезнувший файл пропускается, дописанный сравнивается по факту."""
    root, backups = dirs
    base = make_project(root)
    real_scan = ps._scan_tree

    def scan_then_change(src):
        result = real_scan(src)
        (base / "data.txt").write_text("hello, дописано во время копирования", encoding="utf-8")
        (base / "series/main/20_story" / "story.json").unlink()
        return result

    monkeypatch.setattr(ps, "_scan_tree", scan_then_change)
    info = ps.create_backup(root, backups, "alpha")
    copy = backups / info["backup_id"]
    assert (copy / "data.txt").read_text(encoding="utf-8").endswith("копирования")
    assert not (copy / "series/main/20_story" / "story.json").exists()


def test_studio_artifact_history_survives_copy_backup_restore(dirs):
    root, backups = dirs
    base = make_project(root, files={"data.txt": "x", ".studio/artifact_history/story/v1.json": "{\"v\": 1}",
                                     ".studio/thumbs/a.png": "t"})
    history = ".studio/artifact_history/story/v1.json"
    ps.duplicate_project(root, "alpha", _claim(root, "beta"))
    assert (root / "beta" / history).read_text(encoding="utf-8") == "{\"v\": 1}"
    assert not (root / "beta" / ".studio" / "thumbs").exists()
    info = ps.create_backup(root, backups, "alpha")
    assert (backups / info["backup_id"] / history).is_file()
    (base / history).unlink()
    ps.restore_backup(root, backups, "alpha", info["backup_id"])
    assert (base / history).read_text(encoding="utf-8") == "{\"v\": 1}"


def _claim(root, name):
    (root / name).mkdir()
    return name


def test_copy_grown_file_not_false_incomplete_and_counts_copied(dirs, monkeypatch):
    """Файл вырос во время копирования - не ошибка; files_count считает реально скопированное."""
    root, backups = dirs
    base = make_project(root)
    real_copy = shutil.copy2

    def grow_after_copy(src, dst, *a, **k):
        result = real_copy(src, dst, *a, **k)
        if Path(src).name == "data.txt":
            Path(src).write_text("hello, выросло после копирования", encoding="utf-8")
        return result

    monkeypatch.setattr(ps.shutil, "copy2", grow_after_copy)
    info = ps.create_backup(root, backups, "alpha")
    assert (backups / info["backup_id"] / "data.txt").read_text(encoding="utf-8") == "hello"
    shutil.rmtree(backups / info["backup_id"])
    real_scan = ps._scan_tree

    def scan_then_drop(src):
        result = real_scan(src)
        (base / "data.txt").unlink()
        return result

    monkeypatch.setattr(ps, "_scan_tree", scan_then_drop)
    monkeypatch.setattr(ps.shutil, "copy2", real_copy)
    lost = ps.create_backup(root, backups, "alpha")
    assert lost["files_count"] == 3


def test_copy_shrunk_file_lenient_without_lock_strict_under_lock(dirs, monkeypatch):
    """Файл уменьшился во время копирования: без замка не ошибка, под замком - неполная копия."""
    root, backups = dirs
    base = make_project(root)

    real_copy = shutil.copy2

    def short_copy(src, dst, *a, **k):
        if Path(src).name != "data.txt":
            return real_copy(src, dst, *a, **k)
        Path(dst).write_text("he", encoding="utf-8")
        Path(src).write_text("he", encoding="utf-8")  # прогон перезаписал короче

    monkeypatch.setattr(ps.shutil, "copy2", short_copy)
    info = ps.create_backup(root, backups, "alpha")
    assert (backups / info["backup_id"] / "data.txt").read_text(encoding="utf-8") == "he"
    (base / "data.txt").write_text("hello", encoding="utf-8")
    with pytest.raises(StudioError) as err:
        ps.create_backup(root, backups, "alpha", locked=True)
    assert err.value.code == "internal"


def test_copy_progress_reaches_total_when_file_vanished(dirs, monkeypatch):
    root, backups = dirs
    base = make_project(root)
    real_scan = ps._scan_tree

    def scan_then_drop(src):
        result = real_scan(src)
        (base / "data.txt").unlink()
        return result

    monkeypatch.setattr(ps, "_scan_tree", scan_then_drop)
    calls = []
    ps.create_backup(root, backups, "alpha", progress=lambda done, total, label: calls.append((done, total)))
    assert calls and calls[-1][0] == calls[-1][1] == 4
