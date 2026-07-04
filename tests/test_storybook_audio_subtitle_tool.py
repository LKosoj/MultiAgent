import json
from pathlib import Path

from custom_tools.storybook import audio_subtitle
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
    assert "project_id must be a safe path segment" in result["message"]


def _write_shots_without_cues(base: Path):
    # Valid shots.json (so there is no read error) but nothing produces a cue:
    # no timing and no text -> every item is skipped -> zero cues.
    _write_json(base / "97_shots" / "shots.json", {"items": [{"scene_number": 1, "shot_number": 1}]})


def test_storybook_audio_subtitle_errors_on_zero_cues(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_zero_cues"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_shots_without_cues(base)

    result = storybook_audio_subtitle_tool("sess", project_id)

    assert result["status"] == "error"
    assert result["cue_count"] == 0
    assert result["error"] == result["message"]


def test_storybook_audio_subtitle_allow_missing_subtitles_returns_success(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_allow_missing"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_shots_without_cues(base)

    result = storybook_audio_subtitle_tool("sess", project_id, allow_missing_subtitles=True)

    assert result["status"] == "success"
    assert result["cue_count"] == 0
    assert "no_subtitle_cues" in result["warnings"]
    cue_sheet = json.loads((base / "98_audio" / "cue_sheet.json").read_text(encoding="utf-8"))
    assert cue_sheet["cue_count"] == 0
    assert "no_subtitle_cues" in cue_sheet["warnings"]


def test_storybook_audio_subtitle_error_result_has_required_output_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = storybook_audio_subtitle_tool("sess", "../outside")

    assert result["status"] == "error"
    for key in ("subtitles_path", "audio_manifest_path", "cue_sheet_path"):
        assert key in result
        assert result[key] == ""


def test_storybook_audio_subtitle_writes_are_atomic(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_atomic"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(
        base / "97_shots" / "shots.json",
        {"items": [{"scene_number": 1, "shot_number": 1, "timing": "00:00 - 00:03", "subtitle": "Cue"}]},
    )

    replaced = []
    real_replace = audio_subtitle.os.replace

    def spy_replace(src, dst):
        replaced.append(str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(audio_subtitle.os, "replace", spy_replace)

    result = storybook_audio_subtitle_tool("sess", project_id)

    assert result["status"] == "success"
    assert any(dst.endswith("subtitles.srt") for dst in replaced)
    assert any(dst.endswith("cue_sheet.json") for dst in replaced)
    assert any(dst.endswith("audio_manifest.json") for dst in replaced)
    assert list((base / "98_audio").glob(".*.tmp")) == []


def test_storybook_audio_subtitle_cue_sheet_exposes_join_keys_for_montage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_join_keys"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(
        base / "97_shots" / "shots.json",
        {
            "items": [
                {"scene_number": 1, "shot_number": 1, "timing": "00:00 - 00:03", "subtitle": "First"},
                {"scene_number": 2, "shot_number": 1, "timing": {"start": 3, "end": 6}, "subtitle": "Second"},
            ]
        },
    )

    result = storybook_audio_subtitle_tool("sess", project_id)

    assert result["status"] == "success"
    cue_sheet = json.loads((base / "98_audio" / "cue_sheet.json").read_text(encoding="utf-8"))
    assert cue_sheet["timeline"] == "planned"
    assert cue_sheet["cue_count"] == 2
    for cue in cue_sheet["cues"]:
        for key in ("scene_number", "shot_number", "text", "text_source"):
            assert key in cue
