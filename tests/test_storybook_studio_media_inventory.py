import importlib
import json
import os

import pytest

from tests.studio_run_fakes import make_series_project
from custom_tools.storybook.project_paths import series_scope

from custom_tools.storybook.studio import capabilities
from custom_tools.storybook.studio.errors import StudioError
from custom_tools.storybook.studio.paths import resolve_in_project



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
def _no_real_probe(monkeypatch):
    monkeypatch.setattr(media, "_probe_duration", lambda path, kind: 1.5)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    root = make_series_project(tmp_path, "character_demo")

    def touch(rel, data=b"x", mtime=None):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    touch.root = root
    with series_scope("character_demo", "main"):
        yield touch


def scan(touch, **kw):
    return {e.path: e for e in media.scan_media(touch.root, **kw)}


CASES = [
    # путь, type, category, key, phase, scene, page, music_id
    ("50_images/page_03/img_final.png", "illustration", "book_page", None, None, None, 3, None),
    ("20_bible/references/characters/hero.png", "reference", "character", None, None, None, None, None),
    ("20_bible/references/landmarks/lm_tower_ab.png", "reference", "landmark", None, None, None, None, None),
    ("20_bible/references/background_references/ab/space_1_sheet_1.png", "reference", "background", None, None, None, None, None),
    ("20_bible/references/other.png", "reference", "location", None, None, None, None, None),
    ("97_shots/background_references/ab/space_1_sheet_1.png", "reference", "background", None, None, None, None, None),
    ("97_shots/scene_02_shot_03/img_final_start_02_03.png", "shot", "start_frame", "2-3", "start", 2, None, None),
    ("97_shots/scene_02_shot_03/img_final_end_02_03.png", "shot", "end_frame", "2-3", "end", 2, None, None),
    ("97_shots/scene_02_shot_03/video_final_02_03.mp4", "shot_video", "shot_clip", "2-3", None, 2, None, None),
    ("97_shots/scene_02_shot_03/extra.png", "shot", "shot_other", "2-3", None, 2, None, None),
    ("97_shots/scene_02_shot_03/extra.mp4", "shot_video", "shot_video_other", "2-3", None, 2, None, None),
    ("97_shots/scene_01/clip.mp4", "shot_video", "shot_video_other", None, None, 1, None, None),
    ("93_blockout/scene_01_shot_02/ref_start.png", "blockout_ref", "blockout_ref", "1-2", "start", 1, None, None),
    ("93_blockout/scene_01_shot_02/blockout_ref.mp4", "blockout_video", "blockout_video", "1-2", None, 1, None, None),
    ("93_blockout/preview/contact_sheet_1.png", "blockout_sheet", "blockout_sheet", None, None, None, None, None),
    ("93_blockout/preview/reel.mp4", "blockout_preview", "blockout_preview", None, None, None, None, None),
    ("98_audio/narration.mp3", "narration", "narration", None, None, None, None, None),
    ("98_audio/music/boy.mp3", "music", "leitmotif", None, None, None, None, "boy"),
    ("98_audio/music/neutral.mp3", "music", "neutral", None, None, None, None, "neutral"),
    ("98_audio/sfx/aabbcc.mp3", "sfx", "scene_sfx", None, None, None, None, None),
    ("98_audio/other.wav", "audio", "audio_other", None, None, None, None, None),
    ("98_audio/subtitles.srt", "subtitles", "planned", None, None, None, None, None),
    ("99_final/final_video.mp4", "final_video", "final", None, None, None, None, None),
    ("99_final/photo_video.mp4", "photo_video", "photo", None, None, None, None, None),
    ("99_final/subtitles.srt", "subtitles", "final_subs", None, None, None, None, None),
]


@pytest.mark.parametrize("rel,type_,category,key,phase,scene,page,music_id", CASES)
def test_classification(project, rel, type_, category, key, phase, scene, page, music_id):
    project(rel)
    entry = scan(project)[rel]
    assert (entry.type, entry.category, entry.key, entry.phase, entry.scene, entry.page, entry.music_id) == (
        type_, category, key, phase, scene, page, music_id)


def test_landmark_not_character_by_project_name(project):
    project("20_bible/references/landmarks/x.png")
    assert scan(project)["20_bible/references/landmarks/x.png"].category == "landmark"


def test_video_units_with_plan_and_without(project):
    project("97_shots/video_units/2-2_2-3.mp4")
    project("97_shots/video_units/3-1_3-3.mp4")
    project("97_shots/video_units/2-2_2-3_wan_cycles.mp4")
    project("97_shots/video_plan.json", json.dumps(
        {"units": [{"shot_keys": ["2-2", "2-3"], "video_path": "/x/y/97_shots/video_units/2-2_2-3.mp4"}]}).encode())
    found = scan(project)
    assert found["97_shots/video_units/2-2_2-3.mp4"].keys == ("2-2", "2-3")
    assert found["97_shots/video_units/3-1_3-3.mp4"].keys == ("3-1", "3-2", "3-3")
    variant = found["97_shots/video_units/2-2_2-3_wan_cycles.mp4"]
    assert variant.category == "video_unit_variant" and variant.key == "2-2"


def test_skips_and_symlinks(project, tmp_path):
    project("50_images/page_01/a.png")
    project(".studio/thumbs/t.png")
    project("97_shots/.hidden/x.png")
    project("97_shots/shots.json")
    project("97_shots/x.fcpxml")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    (project.root / "50_images" / "page_01" / "link_out.png").symlink_to(outside)
    (project.root / "50_images" / "page_01" / "link_in.png").symlink_to(project.root / "50_images" / "page_01" / "a.png")
    paths = set(scan(project))
    assert paths == {"50_images/page_01/a.png", "50_images/page_01/link_in.png"}


def test_blockout_frames_flag_and_no_blind_recursion(project, monkeypatch):
    project("93_blockout/scene_01_shot_01/frames/0001.png")
    project("93_blockout/x/deep/y/z.png")
    visited = []
    real = os.scandir
    monkeypatch.setattr(media.os, "scandir", lambda p: (visited.append(str(p)), real(p))[1])
    assert scan(project) == {}
    assert not any(v.endswith("frames") for v in visited)
    assert set(scan(project, include_blockout_frames=True)) == {"93_blockout/scene_01_shot_01/frames/0001.png"}


def test_servable_flag(project):
    project("97_shots/scene_01_shot_01/a.bmp")
    project("97_shots/scene_01_shot_01/b.avi")
    project("97_shots/scene_01_shot_01/c.png")
    found = scan(project)
    assert [found[p].servable for p in sorted(found)] == [False, False, True]


def test_sfx_scene_and_music_id_from_manifests(project):
    project("98_audio/sfx/aa.mp3")
    project("98_audio/music/file1.mp3")
    project("98_audio/sfx_manifest.json", json.dumps(
        {"audio_tracks": [{"role": "sfx", "scene_number": 4, "path": "sfx/aa.mp3"}]}).encode())
    project("98_audio/music_manifest.json", json.dumps({"boy": "music/file1.mp3", "status": "x"}).encode())
    found = scan(project)
    assert found["98_audio/sfx/aa.mp3"].scene == 4
    assert found["98_audio/music/file1.mp3"].music_id == "boy"


def call(project_id="character_demo", **kw):
    return capabilities.invoke("media_inventory", {"project_id": project_id, "series_id": "main", **kw})


def test_filters_and_counts(project):
    project("97_shots/scene_01_shot_01/img_final_start_01_01.png")
    project("97_shots/scene_01_shot_01/video_final_01_01.mp4")
    project("97_shots/scene_02_shot_01/video_final_02_01.mp4")
    project("98_audio/music/boy.mp3")
    project("50_images/page_01/Кадр.png")
    result = call(types=["shot_video"])
    assert result["total"] == 5 and result["matched"] == 2
    assert result["counts"]["types"]["shot_video"] == 2 and result["counts"]["kinds"]["image"] == 2
    assert [i["name"] for i in call(scene=2)["items"]] == ["video_final_02_01.mp4"]
    assert call(key="1-1")["matched"] == 2
    assert call(kinds=["audio"])["matched"] == 1
    assert call(categories=["book_page"])["matched"] == 1
    assert call(search="КАДР")["matched"] == 1
    assert call(types=["shot_video"], scene=1)["matched"] == 1
    assert call(key="9-9")["matched"] == 0


def test_unit_found_by_key(project):
    project("97_shots/video_units/2-2_2-3.mp4")
    assert call(key="2-3")["items"][0]["keys"] == ["2-2", "2-3"]


def test_errors(project):
    project("a/b.png")
    for kw in ({"types": ["nope"]}, {"kinds": ["x"]}, {"sort_by": "bad"}, {"limit": 0}, {"limit": 2001},
               {"offset": -1}, {"types": "shot"}):
        assert call(**kw)["error"]["code"] == "invalid_argument", kw
    assert call("missing_project")["error"]["code"] == "not_found"
    assert call("../x")["error"]["code"] == "invalid_argument"
    assert capabilities.invoke("media_inventory", {"project_id": "character_demo", "series_id": "main", "zzz": 1})["error"]["code"] == "invalid_argument"


def test_sorting(project):
    project("97_shots/scene_01_shot_10/video_final_01_10.mp4", b"xxx", mtime=300)
    project("97_shots/scene_01_shot_02/video_final_01_02.mp4", b"x", mtime=100)
    project("97_shots/scene_01_shot_02/img_final_start_01_02.png", b"xx", mtime=200)
    names = lambda r: [i["name"] for i in r["items"]]
    assert names(call()) == ["img_final_start_01_02.png", "video_final_01_02.mp4", "video_final_01_10.mp4"]
    assert names(call(sort_by="name"))[0] == "img_final_start_01_02.png"
    assert names(call(sort_by="modified", descending=True))[0] == "video_final_01_10.mp4"
    assert names(call(sort_by="size"))[0] == "video_final_01_02.mp4"
    assert names(call(sort_by="type"))[0] == "img_final_start_01_02.png"
    assert names(call(sort_by="duration", descending=True))[-1] == "img_final_start_01_02.png"
    assert names(call(sort_by="duration"))[-1] == "img_final_start_01_02.png"


def test_pagination_and_paths(project):
    for n in range(5):
        project(f"50_images/page_0{n}/картинка {n}.png")
    page = call(limit=2, offset=3)
    assert page["total"] == page["matched"] == 5 and len(page["items"]) == 2
    for it in call()["items"]:
        assert "\\" not in it["path"] and ".." not in it["path"]
        resolve_in_project("character_demo", it["path"])
        assert it["type_title"] and it["category_title"]


def test_registry():
    cap = capabilities.registry()["media_inventory"]
    assert cap.access == "read" and "media_inventory" in cap.supersedes


def _bind(root, records):
    (root.parent.parent / "shared").mkdir(exist_ok=True)
    (root.parent.parent / "shared/reference_catalog.json").write_text(json.dumps(
        {"version": 1, "initialized_series": [], "references": {
            r["id"]: {"keys": [], "aliases": [], "parents": [], "selectable": True, **r} for r in records}}),
        encoding="utf-8")


def test_catalog_references_visible_only_in_bound_series(project):
    project("../../shared/references/characters/aa.png")
    project("../../shared/references/landmarks/bb.png")
    _bind(project.root, [
        {"id": "aa", "path": "shared/references/characters/aa.png", "kinds": ["identity"], "series_ids": ["main"]},
        {"id": "bb", "path": "shared/references/landmarks/bb.png", "kinds": ["landmark"], "series_ids": ["main"]}])
    found = scan(project)
    assert (found["shared/references/characters/aa.png"].category,
            found["shared/references/landmarks/bb.png"].category) == ("character", "landmark")
    assert found["shared/references/characters/aa.png"].type == "reference"
    other = project.root.parent / "other"
    other.mkdir()
    assert not [p for p in media.scan_media(other) if p.path.startswith("shared/")]
    _bind(project.root, [
        {"id": "aa", "path": "shared/references/characters/aa.png", "kinds": ["identity"], "series_ids": ["two"]}])
    assert not [p for p in scan(project) if p.startswith("shared/")]


def test_catalog_references_categories_and_prefix_filter(project):
    project("../../shared/references/backgrounds/bg.png")
    project("../../shared/references/styles/st.png")
    project("../../shared/characters/actor/a.png")
    base = {"series_ids": ["main"]}
    _bind(project.root, [
        {"id": "bg", "path": "shared/references/backgrounds/bg.png", "kinds": ["background"], **base},
        {"id": "st", "path": "shared/references/styles/st.png", "kinds": ["style"], **base},
        {"id": "ac", "path": "shared/characters/actor/a.png", "kinds": ["identity"], **base}])
    found = scan(project)
    assert found["shared/references/backgrounds/bg.png"].category == "background"
    assert found["shared/references/styles/st.png"].category == "location"
    assert "shared/characters/actor/a.png" not in found
