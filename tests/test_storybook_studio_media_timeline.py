import importlib
import json
import os

import pytest

from tests.studio_run_fakes import make_series_project
from custom_tools.storybook.project_paths import series_scope

from custom_tools.storybook.studio import capabilities
from custom_tools.storybook.studio import media_timeline



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


@pytest.fixture(autouse=True)
def _probe_guard(monkeypatch):
    calls = []
    monkeypatch.setattr(media, "_probe_duration", lambda path, kind: calls.append(path.name) or None)
    return calls


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    root = make_series_project(tmp_path, "demo")

    def put(rel, data=b"x", mtime=None):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode())
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    put.root = root
    with series_scope("demo", "main"):
        yield put


def timeline(**kw):
    return capabilities.invoke("media_timeline", {"project_id": "demo", "series_id": "main", **kw})


def seq(*rows):
    return {"sequence": [{"index": i, "scene_number": s, "shot_number": n, "timeline_start_seconds": a,
                          "timeline_end_seconds": b} for i, (s, n, a, b) in enumerate(rows, 1)]}


def full(project):
    """2 сцены, 3 шота, блок 1-2+1-3 на 10 с, звук по всем правилам."""
    project("99_final/edit_decisions.json", {"project_id": "demo", "sequence": [
        {"index": 1, "scene_number": 1, "shot_number": 1, "timeline_start_seconds": 0, "timeline_end_seconds": 5,
         "source_path": "/Users/kosoj/x/demo/97_shots/scene_01_shot_01/video_final_01_01.mp4"},
        {"index": 2, "scene_number": 1, "shot_number": 2, "timeline_start_seconds": 5, "timeline_end_seconds": 15,
         "source_path": "/srv/other/demo/97_shots/video_units/1-2_1-3.mp4"}]})
    project("97_shots/shots.json", {"items": [
        {"scene_number": 1, "shot_number": 1, "shot_type": "start", "timing": "00:05"},
        {"scene_number": 1, "shot_number": 1, "shot_type": "end", "timing": "00:05"},
        {"scene_number": 1, "shot_number": 2, "timing": "00:02"},
        {"scene_number": 1, "shot_number": 3, "timing": "00:08"}]})
    project("97_shots/video_plan.json", {"units": [
        {"shot_keys": ["1-2", "1-3"], "video_path": "/q/97_shots/video_units/1-2_1-3.mp4"}]})
    project("97_shots/scene_01_shot_01/video_final_01_01.mp4", mtime=1000)
    project("97_shots/scene_01_shot_01/img_final_start_01_01.png")
    project("97_shots/video_units/1-2_1-3.mp4", mtime=1000)
    project("99_final/final_video.mp4", mtime=2000)
    project("99_final/final_video.render.json", {"duration_seconds": 15.0})
    project("99_final/asset_manifest.json", {"clips": [
        {"scene_number": 1, "shot_number": 1, "planned_duration_seconds": 5.0, "actual_duration_seconds": 5.06}]})
    project("98_audio/music/boy.mp3")
    project("98_audio/sfx/aa.mp3")
    project("98_audio/audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": [
        {"role": "narration", "path": "/Users/kosoj/demo/98_audio/narration.mp3"},
        {"role": "sfx", "scene_number": 1, "path": "sfx/aa.mp3"}]})
    project("98_audio/sfx_manifest.json", {"audio_tracks": [{"role": "sfx", "scene_number": 1, "path": "sfx/aa.mp3"}]})
    project("98_audio/music_manifest.json", {"boy": "music/boy.mp3"})
    project("98_audio/music_plan.json", {"scene_mapping": {"1": "boy"}})


def test_full_case(project):
    full(project)
    r = timeline()
    assert r["status"] == "success" and r["source"] == "edit_decisions" and r["duration_s"] == 15.0
    by = {s["key"]: s for s in r["shots"]}
    assert by["1-1"]["actual_duration_s"] == 5.06 and by["1-1"]["planned_duration_s"] == 5.0
    assert by["1-1"]["video"] == "97_shots/scene_01_shot_01/video_final_01_01.mp4"
    assert by["1-1"]["thumb_path"] == "97_shots/scene_01_shot_01/img_final_start_01_01.png"
    assert by["1-2"]["unit_span"] and by["1-2"]["unit"]["keys"] == ["1-2", "1-3"]
    assert r["scenes"] == [{"scene": 1, "start_s": 0, "end_s": 15.0, "duration_s": 15.0, "shot_keys": ["1-1", "1-2", "1-3"]}]
    # блок: доли 2:8 от 10 с, 1-3 получает последние 8 с
    assert by["1-3"]["covered_by"] == "1-2" and by["1-3"]["approximate"]
    assert (by["1-3"]["start_s"], by["1-3"]["end_s"]) == (7.0, 15.0)
    assert any(w["code"] == "unit_member_approximate" for w in r["warnings"])
    assert r["final"]["video"]["duration_s"] == 15.0 and r["final"]["stale_hint"] is False


def test_tracks(project, _probe_guard):
    full(project)
    r = timeline()
    assert r["tracks"]["narration"]["status"] == "unavailable" and r["tracks"]["narration"]["clips"] == []
    music = r["tracks"]["music"]
    assert music["source"] == "plan" and len(music["clips"]) == 1
    assert (music["clips"][0]["start_s"], music["clips"][0]["end_s"], music["clips"][0]["placement"]) == (0.0, 15.0, "scene")
    sfx = r["tracks"]["sfx"]["clips"]
    assert len(sfx) == 1 and sfx[0]["placement"] == "scene" and sfx[0]["duration_s"] == 15.0
    assert any(w["code"] == "audio_unknown_duration" for w in r["warnings"])
    assert "/Users/" not in json.dumps(r, ensure_ascii=False)


def test_final_duration_from_render_json_without_probe(project, _probe_guard):
    full(project)
    timeline(probe_limit=0)
    assert "final_video.mp4" not in _probe_guard


def test_final_mismatch_and_stale(project):
    full(project)
    project("99_final/final_video.render.json", {"duration_seconds": 30.0})
    project("97_shots/video_units/1-2_1-3.mp4", mtime=3000)
    r = timeline()
    assert any(w["code"] == "final_duration_mismatch" for w in r["warnings"])
    assert r["final"]["video"]["older_than_shots"] and r["final"]["stale_hint"]


def test_no_final(project):
    project("99_final/edit_decisions.json", seq((1, 1, 0, 5)))
    r = timeline()
    assert r["final"]["video"] is None and "final_video" in r["missing"]


def test_fallback_shots_and_timing(project):
    project("97_shots/shots.json", {"items": [
        {"scene_number": 1, "shot_number": 1, "timing": "00:05"},
        {"scene_number": 1, "shot_number": 2, "timing": "7s"},
        {"scene_number": 1, "shot_number": 3, "timing": "3"},
        {"scene_number": 1, "shot_number": 4, "timing": "мусор"},
        {"scene_number": 2, "shot_number": 1, "duration_s": 4}]})
    r = timeline()
    assert r["source"] == "shots"
    assert [(s["key"], s["start_s"], s["end_s"]) for s in r["shots"]] == [
        ("1-1", 0, 5), ("1-2", 5, 12), ("1-3", 12, 15), ("1-4", 15, 20), ("2-1", 20, 24)]
    assert any(w["code"] == "no_edit_decisions" for w in r["warnings"])


def test_fallback_cue_sheet_and_nothing(project):
    r = timeline()
    assert r["status"] == "success" and r["shots"] == [] and r["source"] is None and "shots" in r["missing"]
    project("98_audio/cue_sheet.json", {"cues": [
        {"scene_number": 1, "shot_number": 1, "start_seconds": 0, "end_seconds": 2},
        {"scene_number": 1, "shot_number": 1, "start_seconds": 2, "end_seconds": 4},
        {"scene_number": 1, "shot_number": 2, "start_seconds": 4, "end_seconds": 6}]})
    r = timeline()
    assert r["source"] == "cue_sheet" and [s["end_s"] for s in r["shots"]] == [4, 6]


def test_broken_edit_decisions_falls_back(project):
    project("99_final/edit_decisions.json", b"{not json")
    project("97_shots/shots.json", {"items": [{"scene_number": 1, "shot_number": 1, "timing": "00:05"}]})
    r = timeline()
    assert r["source"] == "shots"
    assert {"edit_decisions_unreadable"} <= {w["code"] for w in r["warnings"]}


def test_shot_not_in_cut(project):
    project("99_final/edit_decisions.json", seq((1, 1, 0, 5)))
    project("97_shots/shots.json", {"items": [{"scene_number": 1, "shot_number": 2, "timing": "00:05"}]})
    r = timeline()
    extra = [s for s in r["shots"] if s["key"] == "1-2"][0]
    assert extra["in_cut"] is False and extra["start_s"] is None
    assert any(w["code"] == "shot_not_in_cut" for w in r["warnings"])


def test_foreign_paths(project):
    project("99_final/edit_decisions.json", seq((1, 1, 0, 5)))
    project("98_audio/audio_manifest.json", {"audio_tracks": [
        {"role": "sfx", "scene_number": 1, "path": "/Users/kosoj/elsewhere/clip.mp3"},
        {"role": "sfx", "scene_number": 1, "path": "/mnt/p/demo/98_audio/sfx/a.mp3"},
        {"role": "sfx", "scene_number": 1, "path": "plots/storybooks/demo/98_audio/sfx/b.mp3"}]})
    project("98_audio/sfx/a.mp3")
    r = timeline()
    assert [w["code"] for w in r["warnings"] if w["code"] == "path_unresolved"] == ["path_unresolved"]
    assert "/Users/" not in json.dumps(r, ensure_ascii=False)
    assert sorted(c["path"] for c in r["tracks"]["sfx"]["clips"]) == ["98_audio/sfx/a.mp3", "98_audio/sfx/b.mp3"]
    clips = {c["path"]: c for c in r["tracks"]["sfx"]["clips"]}
    assert clips["98_audio/sfx/b.mp3"]["exists"] is False
    assert any(w["code"] == "missing_file" for w in r["warnings"])


def test_audio_rules(project, monkeypatch):
    project("99_final/edit_decisions.json", seq((1, 1, 0, 10), (2, 1, 10, 20)))
    for name in ("narration.mp3", "sfx/e1.mp3", "sfx/e2.mp3", "sfx/e3.mp3", "music/boy.mp3"):
        project(f"98_audio/{name}")
    project("98_audio/audio_manifest.json", {"audio_tracks": [
        {"role": "narration", "path": "narration.mp3", "offset_seconds": 2},
        {"role": "sfx", "scene_number": 2, "path": "sfx/e1.mp3"},
        {"role": "sfx", "scene_number": 1, "path": "sfx/e2.mp3", "offset_seconds": 3},
        {"role": "sfx", "scene_number": 1, "path": "sfx/e3.mp3"},
        {"role": "music", "path": "music/boy.mp3"}]})
    lengths = {"e3.mp3": 4.0, "boy.mp3": 5.0}
    monkeypatch.setattr(media, "_probe_duration", lambda path, kind: lengths.get(path.name))
    r = timeline()
    nar = r["tracks"]["narration"]["clips"][0]
    assert (nar["start_s"], nar["duration_s"], nar["placement"]) == (2.0, 18.0, "manifest")
    sfx = {c["label"]: c for c in r["tracks"]["sfx"]["clips"]}
    assert (sfx["e1"]["start_s"], sfx["e1"]["duration_s"]) == (10.0, 10.0)
    assert (sfx["e2"]["start_s"], sfx["e2"]["placement"]) == (3.0, "manifest")
    assert sfx["e3"]["duration_s"] == 4.0  # обрезано по длине файла
    music = r["tracks"]["music"]
    assert music["source"] == "manifest" and music["clips"][0]["placement"] == "film"
    assert music["clips"][0]["duration_s"] == 5.0


def test_music_plan_loop_gaps_and_pending(project, monkeypatch):
    project("99_final/edit_decisions.json", seq((1, 1, 0, 10), (2, 1, 10, 20), (3, 1, 20, 30)))
    project("98_audio/music/boy.mp3")
    project("98_audio/music_manifest.json", {"boy": {"path": "music/boy.mp3"}, "status": "submitted", "clip_ids": ["a"]})
    project("98_audio/music_plan.json", {"scene_mapping": {"1": "boy", "3": "boy"}})
    monkeypatch.setattr(media, "_probe_duration", lambda path, kind: 4.0)
    r = timeline()
    music = r["tracks"]["music"]
    assert [c["scene"] for c in music["clips"]] == [1, 3]  # сцена 2 без музыки
    assert all(c["loop"] is True for c in music["clips"])
    assert any(w["code"] == "music_pending" for w in r["warnings"])


def test_music_pending_only(project):
    project("98_audio/music_manifest.json", {"status": "submitted", "clip_ids": ["a"]})
    r = timeline()
    assert r["tracks"]["music"]["status"] == "missing" and r["tracks"]["music"]["note"]
    assert any(w["code"] == "music_pending" for w in r["warnings"])


def test_sfx_duplicates_collapsed(project):
    project("98_audio/sfx/a.mp3")
    for name in ("audio_manifest.json", "sfx_manifest.json"):
        project(f"98_audio/{name}", {"audio_tracks": [{"role": "sfx", "scene_number": 1, "path": "sfx/a.mp3"}]})
    assert len(timeline()["tracks"]["sfx"]["clips"]) == 1


def test_labels_cover_responses(project):
    full(project)
    r = timeline()
    inv = capabilities.invoke("media_inventory", {"project_id": "demo", "series_id": "main"})
    for item in inv["items"]:
        assert item["type"] in r["labels"]["types"] and item["category"] in r["labels"]["categories"]
    assert r["final"]["video"]["type"] in r["labels"]["types"]


def test_actual_duration_from_cache_within_limit(project, monkeypatch):
    project("99_final/edit_decisions.json", seq((1, 1, 0, 5), (1, 2, 5, 10)))
    project("97_shots/scene_01_shot_01/video_final_01_01.mp4")
    project("97_shots/scene_01_shot_02/video_final_01_02.mp4")
    monkeypatch.setattr(media, "_probe_duration", lambda path, kind: 5.1)
    r = timeline(probe_limit=1)
    assert [s["actual_duration_s"] for s in r["shots"]] == [5.1, None] and r["durations_pending"] == 1


def test_json_cache_parses_once(project, monkeypatch):
    path = project("97_shots/video_plan.json", {"units": []})
    count = {"n": 0}
    real = json.loads
    monkeypatch.setattr(media.json, "loads", lambda *a, **k: (count.__setitem__("n", count["n"] + 1), real(*a, **k))[1])
    media._JSON_CACHE.clear()
    media.read_json_cached(path, path.parent.parent)
    media.read_json_cached(path, path.parent.parent)
    assert count["n"] == 1
    path.write_text('{"units": [1]}')
    os.utime(path, (9999, 9999))
    assert media.read_json_cached(path, path.parent.parent)[1] == {"units": [1]}


def test_invalid_probe_limit(project):
    assert timeline(probe_limit=-1)["error"]["code"] == "invalid_argument"


def test_parse_timing():
    p = media_timeline.parse_timing
    assert (p("00:05"), p("5s"), p("5"), p("1:02:03"), p("xx"), p(None), p(0)) == (5, 5, 5, 3723, 5, 5, 5)


def test_symlink_json_outside_project_ignored(project, tmp_path):
    outside = tmp_path / "evil.json"
    outside.write_text(json.dumps(seq((1, 1, 0, 5))))
    (project.root / "99_final").mkdir()
    (project.root / "99_final" / "edit_decisions.json").symlink_to(outside)
    r = timeline()
    assert r["shots"] == [] and r["source"] is None
