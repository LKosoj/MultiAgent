import json
from pathlib import Path

from custom_tools.storybook.audio_subtitle import storybook_audio_subtitle_tool


def _write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_storybook_audio_subtitle_writes_srt_manifest_and_cue_sheet(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_audio"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(
        base / "97_shots" / "shots.json",
        {
            "items": [
                {
                    "scene_number": 1,
                    "shot_number": 1,
                    "shot_type": "start",
                    "timing": "00:00 - 00:03",
                    "initial_state_summary": "fallback text",
                },
                {
                    "scene_number": 1,
                    "shot_number": 2,
                    "shot_type": "start",
                    "initial_state_summary": "fallback second",
                },
            ]
        },
    )
    _write_json(
        base / "91_screenplay" / "screenplay.json",
        {
            "screenplay": [
                {
                    "scene_number": 1,
                    "action": "Scene action",
                    "storyboard": [
                        {"shot_number": 1, "description": "Первый титр"},
                        {"shot_number": 2, "description": "Второй титр", "timing": "3s"},
                    ],
                }
            ]
        },
    )

    result = storybook_audio_subtitle_tool("sess", project_id, language="ru")

    assert result["status"] == "success"
    subtitles = base / "98_audio" / "subtitles.srt"
    manifest = json.loads((base / "98_audio" / "audio_manifest.json").read_text(encoding="utf-8"))
    cue_sheet = json.loads((base / "98_audio" / "cue_sheet.json").read_text(encoding="utf-8"))
    srt_text = subtitles.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:03,000" in srt_text
    assert "00:00:03,000 --> 00:00:06,000" in srt_text
    assert "Первый титр" in srt_text
    assert "Второй титр" in srt_text
    assert manifest["tts_status"] == "unavailable"
    assert manifest["audio_tracks"] == []
    assert cue_sheet["cue_count"] == 2


def test_storybook_audio_subtitle_uses_shot_text_without_screenplay(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_no_screenplay"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(
        base / "97_shots" / "shots.json",
        {
            "items": [
                {
                    "scene_number": 1,
                    "shot_number": 1,
                    "timing": {"start": 0, "end": 2.5},
                    "subtitle": "Shot subtitle",
                }
            ]
        },
    )

    result = storybook_audio_subtitle_tool("sess", project_id)

    assert result["status"] == "success"
    srt_text = (base / "98_audio" / "subtitles.srt").read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:02,500" in srt_text
    assert "Shot subtitle" in srt_text


def test_storybook_audio_subtitle_rejects_project_id_path_traversal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = storybook_audio_subtitle_tool("sess", "../outside")

    assert result["status"] == "error"
    assert "escapes storybook root" in result["message"]
