import json

import pytest

from custom_tools.storybook import screenplay_generator


def _write_project(tmp_path, screenplay):
    project_id = "location_rebind"
    base = tmp_path / project_id
    (base / "91_screenplay").mkdir(parents=True)
    (base / "20_bible").mkdir(parents=True)
    (base / "91_screenplay" / "screenplay.json").write_text(
        json.dumps(screenplay, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (base / "20_bible" / "locations.json").write_text(
        json.dumps(
            [
                {"name": "Workshop"},
                {"name": "Workshop Dream"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return project_id, base


def _screenplay(location):
    return {
        "screenplay": [
            {
                "scene_number": 1,
                "action": "Нейтральное действие",
                "location_canon_name": location,
                "dialogue": [{"speaker": "Герой", "text": "Слово"}],
                "storyboard": [
                    {
                        "shot_number": 1,
                        "description": "Нейтральный кадр",
                        "camera_plan": "Medium shot",
                        "timing": "2s",
                        "location_canon_name": location,
                    }
                ],
            }
        ],
        "director_notes": {"music": "neutral", "sfx": ["step"]},
    }


def _without_locations(value):
    if isinstance(value, dict):
        return {
            key: _without_locations(item)
            for key, item in value.items()
            if key not in {"location_canon_name", "shot_locations"}
        }
    if isinstance(value, list):
        return [_without_locations(item) for item in value]
    return value


def test_cached_screenplay_rebinds_invalid_locations_only(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id, base = _write_project(tmp_path, _screenplay("Old label"))
    original = json.loads(
        (base / "91_screenplay" / "screenplay.json").read_text(encoding="utf-8")
    )
    calls = []

    def fake_llm(**kwargs):
        calls.append(kwargs)
        return {
            "bindings": [
                {
                    "scene_number": 1,
                    "location_canon_name": "Workshop Dream",
                    "shot_locations": [
                        {"shot_number": 1, "location_canon_name": "Workshop Dream"}
                    ],
                }
            ]
        }

    monkeypatch.setattr(screenplay_generator, "_llm_json_call", fake_llm)
    screenplay_generator.screenplay_generator_tool("session", project_id, 120, True)

    updated = json.loads(
        (base / "91_screenplay" / "screenplay.json").read_text(encoding="utf-8")
    )
    assert len(calls) == 1
    scene = updated["screenplay"][0]
    assert scene["location_canon_name"] == "Workshop Dream"
    assert scene["storyboard"][0]["location_canon_name"] == "Workshop Dream"
    assert _without_locations(updated) == _without_locations(original)


def test_valid_cached_screenplay_skips_rebind_and_write(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id, base = _write_project(tmp_path, _screenplay("Workshop"))
    screenplay_path = base / "91_screenplay" / "screenplay.json"
    before = screenplay_path.read_bytes()

    monkeypatch.setattr(
        screenplay_generator,
        "_llm_json_call",
        lambda **kwargs: pytest.fail("valid cached bindings must not call the model"),
    )
    screenplay_generator.screenplay_generator_tool("session", project_id, 120, True)

    assert screenplay_path.read_bytes() == before


def test_invalid_rebind_name_fails_without_changing_cached_screenplay(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id, base = _write_project(tmp_path, _screenplay("Old label"))
    screenplay_path = base / "91_screenplay" / "screenplay.json"
    before = screenplay_path.read_bytes()

    monkeypatch.setattr(
        screenplay_generator,
        "_llm_json_call",
        lambda **kwargs: {
            "bindings": [{"scene_number": 1, "location_canon_name": "Unknown location"}]
        },
    )
    with pytest.raises(ValueError):
        screenplay_generator.screenplay_generator_tool("session", project_id, 120, True)

    assert screenplay_path.read_bytes() == before
