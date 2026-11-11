"""Выключатель субтитров: audio_subtitle и итоговая проверка монтажа."""

import json
from pathlib import Path

from custom_tools.storybook.audio_subtitle import storybook_audio_subtitle_tool
from custom_tools.storybook.montage_assembler import _build_final_review
from custom_tools.storybook.studio import project_store, series


def _project(tmp_path, monkeypatch, with_text=True):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id = project_store.create_project(tmp_path, title="World", description="Story world", genre="fairy tale", target_age="6-8", id_hint="p")
    series_id = series.series_create(project_id, "Episode", "Story", [], {})["series_id"]
    base = series.series_dir(project_id, series_id)
    (base / "97_shots").mkdir(parents=True, exist_ok=True)
    item = {"scene_number": 1, "shot_number": 1, "shot_type": "start", "timing": "00:00 - 00:03"}
    if with_text:
        item["initial_state_summary"] = "Текст"
    (base / "97_shots" / "shots.json").write_text(json.dumps({"items": [item]}, ensure_ascii=False), encoding="utf-8")
    audio = base / "98_audio"
    audio.mkdir(exist_ok=True)
    lines = [{"id": "line", "scene_number": 1, "shot_number": 1, "text": "Текст", "start": 0, "end": 3}] if with_text else []
    (audio / "speech.json").write_text(json.dumps({"status": "success" if with_text else "silent", "duration": 3, "audio_path": "speech.wav" if with_text else None, "lines": lines}, ensure_ascii=False), encoding="utf-8")
    return base, project_id, series_id


def _manifest(base):
    return json.loads((base / "98_audio" / "audio_manifest.json").read_text(encoding="utf-8"))


def test_subtitles_on_by_default(tmp_path, monkeypatch):
    base, project_id, series_id = _project(tmp_path, monkeypatch)
    result = storybook_audio_subtitle_tool("s", project_id, series_id=series_id, language="ru")
    assert result["status"] == "success"
    assert _manifest(base)["subtitles_enabled"] is True
    assert (base / "98_audio" / "subtitles.srt").is_file()


def test_subtitles_off_writes_no_srt_and_no_warning(tmp_path, monkeypatch):
    base, project_id, series_id = _project(tmp_path, monkeypatch, with_text=False)
    result = storybook_audio_subtitle_tool("s", project_id, series_id=series_id, language="ru", generate_subtitles=False)
    assert result["status"] == "success"
    assert _manifest(base)["subtitles_enabled"] is False
    assert not (base / "98_audio" / "subtitles.srt").exists()
    assert "no_subtitle_cues" not in json.dumps(result)


def test_subtitles_off_with_text_still_no_srt(tmp_path, monkeypatch):
    base, project_id, series_id = _project(tmp_path, monkeypatch)
    result = storybook_audio_subtitle_tool("s", project_id, series_id=series_id, language="ru", generate_subtitles=False)
    assert result["status"] == "success"
    assert not (base / "98_audio" / "subtitles.srt").exists()


def _review(tmp_path, enabled):
    return _build_final_review(
        clips=[], output_path=tmp_path / "final.mp4", final_probe=None, expected_duration=0.0,
        subtitles_path=tmp_path / "none.srt", render_result=None, blackdetect_result=None,
        volume_result=None, audio_required=False, errors=[], items=[], subtitles_enabled=enabled,
    )["checks"]["subtitles"]


def test_final_review_subtitles_disabled_passes(tmp_path):
    check = _review(tmp_path, False)
    assert check["passed"] is True and check["status"] == "disabled" and check["cue_count"] == 0


def test_final_review_subtitles_enabled_fails_without_file(tmp_path):
    assert _review(tmp_path, True)["passed"] is False
