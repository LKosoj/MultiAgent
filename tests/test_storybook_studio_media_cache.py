import importlib
import json
import logging
import os
import subprocess
import sys
import types

import pytest

from custom_tools.storybook.studio import capabilities



class _LiveMedia:
    """Всегда текущий модуль media: другие тесты пересоздают реестр и подменяют модули в sys.modules."""

    def _mod(self):
        capabilities.registry()
        return importlib.import_module("custom_tools.storybook.studio.media")

    def __getattr__(self, name):
        return getattr(self._mod(), name)

    def __setattr__(self, name, value):
        setattr(self._mod(), name, value)

    def __delattr__(self, name):
        delattr(self._mod(), name)


media = _LiveMedia()


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    root = tmp_path / "demo"
    root.mkdir()

    def touch(rel, data=b"x", mtime=None):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    touch.root = root
    return touch


@pytest.fixture
def probes(monkeypatch):
    calls = []

    def fake(path, kind):
        calls.append(path.name)
        return None if "bad" in path.name else 2.5

    monkeypatch.setattr(media, "_probe_duration", fake)
    return calls


def inv(**kw):
    return capabilities.invoke("media_inventory", {"project_id": "demo", "kinds": ["video"], **kw})


def make_videos(project, n=5):
    for i in range(n):
        project(f"97_shots/scene_01_shot_0{i + 1}/video_final_01_0{i + 1}.mp4", mtime=1000 + i)


def test_second_call_opens_nothing(project, probes):
    make_videos(project)
    first = inv()
    assert len(probes) == 5 and first["durations_pending"] == 0
    assert all(i["duration_s"] == 2.5 for i in first["items"])
    inv()
    assert len(probes) == 5
    assert (project.root / ".studio" / "media_index.json").is_file()


def test_changed_file_reprobed_only(project, probes):
    make_videos(project, 3)
    inv()
    probes.clear()
    path = project.root / "97_shots/scene_01_shot_02/video_final_01_02.mp4"
    os.utime(path, (5000, 5000))
    inv()
    assert probes == ["video_final_01_02.mp4"]


def test_probe_limit_and_pending(project, probes):
    make_videos(project)
    first = inv(probe_limit=2)
    assert len(probes) == 2 and first["durations_pending"] == 3
    assert [inv(probe_limit=2)["durations_pending"] for _ in range(2)] == [1, 0]
    assert len(probes) == 5


def test_probe_limit_zero_keeps_cache(project, probes):
    make_videos(project, 2)
    inv()
    probes.clear()
    result = inv(probe_limit=0)
    assert probes == [] and all(i["duration_s"] == 2.5 for i in result["items"])
    project("97_shots/scene_01_shot_09/video_final_01_09.mp4")
    result = inv(probe_limit=0)
    assert probes == [] and result["durations_pending"] == 1


def test_failed_cached_until_change(project, probes):
    project("97_shots/scene_01_shot_01/bad.mp4", mtime=100)
    inv()
    inv()
    assert probes == ["bad.mp4"]
    os.utime(project.root / "97_shots/scene_01_shot_01/bad.mp4", (200, 200))
    inv()
    assert probes == ["bad.mp4", "bad.mp4"]


def test_deleted_file_pruned(project, probes):
    make_videos(project, 2)
    inv()
    (project.root / "97_shots/scene_01_shot_01/video_final_01_01.mp4").unlink()
    project("97_shots/scene_01_shot_03/video_final_01_03.mp4")
    inv()
    index = json.loads((project.root / ".studio/media_index.json").read_text())
    assert "97_shots/scene_01_shot_01/video_final_01_01.mp4" not in index["entries"]


@pytest.mark.parametrize("payload", ["not json", json.dumps({"version": 7, "entries": {}}), "[1]"])
def test_broken_index_ignored(project, probes, payload):
    make_videos(project, 1)
    (project.root / ".studio").mkdir()
    (project.root / ".studio/media_index.json").write_text(payload)
    assert inv()["items"][0]["duration_s"] == 2.5
    assert json.loads((project.root / ".studio/media_index.json").read_text())["version"] == 1


def test_write_failure_is_not_fatal(project, probes, monkeypatch, caplog):
    make_videos(project, 1)
    media._WARNED_WRITE.clear()

    def boom(*a, **k):
        raise PermissionError("ro")

    monkeypatch.setattr(media.os, "replace", boom)
    with caplog.at_level(logging.WARNING):
        assert inv()["status"] == "success"
        project("97_shots/scene_01_shot_05/video_final_01_05.mp4")
        assert inv()["status"] == "success"
    assert len([r for r in caplog.records if "индекс" in r.getMessage()]) == 1


def test_video_duration_single(project, probes):
    make_videos(project, 1)
    rel = "97_shots/scene_01_shot_01/video_final_01_01.mp4"
    assert media.video_duration(project.root, rel) == 2.5
    assert media.video_duration(project.root, rel, probe=False) == 2.5
    assert len(probes) == 1


def test_probe_branches(project, monkeypatch):
    import wave

    path = project("a.mp4")
    # без cv2 и без ffprobe
    monkeypatch.setitem(sys.modules, "cv2", None)
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    assert media._probe_duration(path, "video") is media._NO_TOOL
    # ffprobe
    monkeypatch.setattr(media.shutil, "which", lambda name: "/bin/ffprobe")
    monkeypatch.setattr(media.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="3.456\n"))
    assert media._probe_duration(path, "video") == 3.46
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="N/A"))
    assert media._probe_duration(path, "video") is None
    # настоящий wav
    wav = project.root / "s.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\0\0" * 800)
    assert media._probe_duration(wav, "audio") == 0.1


def test_no_tool_is_not_cached_as_failed(project, monkeypatch):
    make_videos(project, 1)
    monkeypatch.setitem(sys.modules, "cv2", None)
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    assert inv()["items"][0]["duration_s"] is None
    index = project.root / ".studio" / "media_index.json"
    assert not index.exists() or json.loads(index.read_text())["entries"] == {}
    monkeypatch.setattr(media, "_probe_duration", lambda path, kind: 7.0)
    assert inv()["items"][0]["duration_s"] == 7.0


def test_studio_symlink_outside_project_is_not_used(project, probes, tmp_path):
    make_videos(project, 1)
    rel = "97_shots/scene_01_shot_01/video_final_01_01.mp4"
    st = (project.root / rel).stat()
    outside = tmp_path / "outside_studio"
    outside.mkdir()
    (outside / "media_index.json").write_text(json.dumps({"version": 1, "entries": {
        rel: {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "duration_s": 99.0, "failed": False}}}))
    before = (outside / "media_index.json").read_text()
    (project.root / ".studio").symlink_to(outside)
    assert inv()["items"][0]["duration_s"] == 2.5  # чужой индекс не прочитан
    assert (outside / "media_index.json").read_text() == before  # и ничего туда не записано
    assert list(outside.iterdir()) == [outside / "media_index.json"]


def test_unsafe_index_keys_dropped(project):
    (project.root / ".studio").mkdir()
    good = {"size": 1, "mtime_ns": 1, "duration_s": 1.0, "failed": False}
    (project.root / ".studio/media_index.json").write_text(json.dumps({"version": 1, "entries": {
        "../x.mp4": good, "/abs.mp4": good, "a/../b.mp4": good, "ok/c.mp4": good}}))
    assert list(media._load_index(project.root)) == ["ok/c.mp4"]
