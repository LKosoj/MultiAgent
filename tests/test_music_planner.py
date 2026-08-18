import json
import logging
from pathlib import Path

from custom_tools.storybook import music_planner


_MUSIC_PLANNER_LOGGER = "custom_tools.storybook.music_planner"
_NO_VOCALS_SUFFIX = "instrumental only, no vocals"


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_scenes(base: Path, scenes) -> None:
    _write_json(
        base / "91_screenplay" / "screenplay.json",
        {
            "concept": {"title": "Test Film"},
            "characters": [{"name": "Hero", "appearance": "tall", "character": "brave"}],
            "screenplay": scenes,
        },
    )
    _write_json(
        base / "00_brief.json",
        {"title": "Test Film", "genre": "adventure", "target_age": "8-12"},
    )


def test_music_planner_ok_valid_llm_response(tmp_path, monkeypatch):
    base = tmp_path
    _write_scenes(
        base,
        [
            {"scene_number": 1, "location_time": "Forest, day", "action": "Hero walks", "sound": "birds", "characters": ["Hero"]},
            {"scene_number": 2, "location_time": "Castle, night", "action": "Villain plots", "sound": "wind", "characters": ["Villain"]},
        ],
    )

    llm_response = {
        "leitmotifs": {
            "hero": {
                "suno_prompt": "orchestral adventure theme, 120 bpm, french horns and strings, heroic and bright, instrumental only, no vocals",
                "description": "Hero theme",
                "target": "character",
            },
            "villain": {
                "suno_prompt": "dark ambient theme, 80 bpm, low brass and drones, menacing and cold, instrumental only, no vocals",
                "description": "Villain theme",
                "target": "character",
            },
        },
        "neutral": {
            "suno_prompt": "gentle ambient pad, 90 bpm, soft strings, calm and warm, instrumental only, no vocals",
            "description": "Neutral theme",
        },
        "scene_mapping": {"1": "hero", "2": "villain"},
        "rationale": "Two main characters get themes.",
    }
    monkeypatch.setattr(music_planner, "call_openai_api", lambda **kwargs: llm_response)

    result = music_planner.music_planner_tool(str(base), language="ru")

    assert result["status"] == "ok"
    assert result["leitmotif_count"] == 2

    plan_path = base / "98_audio" / "music_plan.json"
    assert plan_path.exists()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    assert set(plan["leitmotifs"].keys()) == {"hero", "villain"}
    assert set(plan["scene_mapping"].keys()) == {"1", "2"}
    assert plan["scene_mapping"]["1"] == "hero"
    assert plan["scene_mapping"]["2"] == "villain"


def test_music_planner_truncates_over_budget(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=_MUSIC_PLANNER_LOGGER)
    base = tmp_path
    _write_scenes(
        base,
        [
            {"scene_number": 1, "location_time": "A", "action": "a", "sound": "", "characters": []},
            {"scene_number": 2, "location_time": "B", "action": "b", "sound": "", "characters": []},
            {"scene_number": 3, "location_time": "C", "action": "c", "sound": "", "characters": []},
        ],
    )

    llm_response = {
        "leitmotifs": {
            "a": {"suno_prompt": "theme a, 100 bpm, piano, warm, instrumental only, no vocals", "description": "A", "target": "character"},
            "b": {"suno_prompt": "theme b, 100 bpm, guitar, bright, instrumental only, no vocals", "description": "B", "target": "character"},
            "c": {"suno_prompt": "theme c, 100 bpm, strings, dark, instrumental only, no vocals", "description": "C", "target": "location"},
        },
        "neutral": {"suno_prompt": "neutral pad, 90 bpm, synth, calm, instrumental only, no vocals", "description": "Neutral"},
        "scene_mapping": {"1": "a", "2": "b", "3": "a"},
        "rationale": "Three themes proposed.",
    }
    monkeypatch.setattr(music_planner, "call_openai_api", lambda **kwargs: llm_response)

    result = music_planner.music_planner_tool(str(base), language="ru")

    assert result["status"] == "degraded"
    assert result["leitmotif_count"] == 2

    plan = json.loads((base / "98_audio" / "music_plan.json").read_text(encoding="utf-8"))
    assert set(plan["leitmotifs"].keys()) == {"a", "b"}
    assert "c" not in plan["leitmotifs"]

    assert "budget" in caplog.text.lower()


def test_music_planner_missing_scene_defaults_to_neutral(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=_MUSIC_PLANNER_LOGGER)
    base = tmp_path
    _write_scenes(
        base,
        [
            {"scene_number": 1, "location_time": "A", "action": "a", "sound": "", "characters": ["Hero"]},
            {"scene_number": 2, "location_time": "B", "action": "b", "sound": "", "characters": []},
            {"scene_number": 3, "location_time": "C", "action": "c", "sound": "", "characters": []},
        ],
    )

    llm_response = {
        "leitmotifs": {
            "hero": {"suno_prompt": "hero theme, 110 bpm, brass, bold, instrumental only, no vocals", "description": "Hero", "target": "character"},
        },
        "neutral": {"suno_prompt": "neutral pad, 90 bpm, synth, calm, instrumental only, no vocals", "description": "Neutral"},
        "scene_mapping": {"1": "hero", "2": "neutral"},
        "rationale": "Only scene 1 has the hero.",
    }
    monkeypatch.setattr(music_planner, "call_openai_api", lambda **kwargs: llm_response)

    result = music_planner.music_planner_tool(str(base), language="ru")

    assert result["status"] == "degraded"

    plan = json.loads((base / "98_audio" / "music_plan.json").read_text(encoding="utf-8"))
    assert plan["scene_mapping"]["1"] == "hero"
    assert plan["scene_mapping"]["2"] == "neutral"
    assert plan["scene_mapping"]["3"] == "neutral"

    assert "3" in caplog.text
    assert "neutral" in caplog.text.lower()


def test_music_planner_strips_vocals_tokens(tmp_path, monkeypatch):
    base = tmp_path
    _write_scenes(
        base,
        [
            {"scene_number": 1, "location_time": "Room", "action": "solo", "sound": "", "characters": ["Hero"]},
        ],
    )

    llm_response = {
        "leitmotifs": {
            "hero": {
                "suno_prompt": "energetic rock with heavy vocals, some singing melody and lyrics for the chorus, guitar riffs",
                "description": "Hero",
                "target": "character",
            },
        },
        "neutral": {
            "suno_prompt": "calm piano with soft vocals, gentle singing tones, no lyrics needed, ambient textures throughout the scene",
            "description": "Neutral",
        },
        "scene_mapping": {"1": "hero"},
        "rationale": "Single scene, single theme.",
    }
    monkeypatch.setattr(music_planner, "call_openai_api", lambda **kwargs: llm_response)

    result = music_planner.music_planner_tool(str(base), language="ru")
    assert result["status"] == "ok"

    plan = json.loads((base / "98_audio" / "music_plan.json").read_text(encoding="utf-8"))
    leitmotif_prompt = plan["leitmotifs"]["hero"]["suno_prompt"]
    neutral_prompt = plan["neutral"]["suno_prompt"]

    for prompt in (leitmotif_prompt, neutral_prompt):
        assert "singing" not in prompt.lower()
        assert "lyrics" not in prompt.lower()
        # "vocals" must only remain as part of the mandatory "no vocals" suffix.
        assert prompt.lower().count("vocals") == 1
        assert prompt.count(_NO_VOCALS_SUFFIX) == 1
        assert prompt.endswith(_NO_VOCALS_SUFFIX)
        assert 30 <= len(prompt) <= 500


def test_music_planner_llm_failure_writes_fallback(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=_MUSIC_PLANNER_LOGGER)
    base = tmp_path
    _write_scenes(
        base,
        [
            {"scene_number": 1, "location_time": "A", "action": "a", "sound": "", "characters": []},
            {"scene_number": 2, "location_time": "B", "action": "b", "sound": "", "characters": []},
        ],
    )

    def _raise(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(music_planner, "call_openai_api", _raise)

    result = music_planner.music_planner_tool(str(base), language="ru")

    assert result["status"] == "fallback"
    assert result["leitmotif_count"] == 0

    plan = json.loads((base / "98_audio" / "music_plan.json").read_text(encoding="utf-8"))
    assert plan["leitmotifs"] == {}
    assert plan["scene_mapping"] == {"1": "neutral", "2": "neutral"}

    assert "boom" in caplog.text
