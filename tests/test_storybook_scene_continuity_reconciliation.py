import pytest

from custom_tools.storybook import screenplay_generator


def _inputs():
    return {
        "story_anchors": {"concept": "Нейтральная сцена"},
        "screenplay_outline": {"scenes": [{"scene_number": 1, "action": "Работа"}]},
        "scene": {
            "scene_number": 1,
            "action": "Двое сотрудников работают в мастерской",
            "storyboard": [{"shot_number": 1, "description": "У двери склада"}],
        },
    }


@pytest.mark.parametrize("facts", [None, {}, [], "missing"])
def test_reconciliation_requires_nonempty_continuity_facts(monkeypatch, facts):
    response = {"scene_number": 1, "action": "Работа", "storyboard": []}
    if facts != "missing":
        response["scene_continuity_facts"] = facts

    monkeypatch.setattr(screenplay_generator, "_llm_json_call", lambda **kwargs: response)

    with pytest.raises(ValueError, match="scene_continuity_facts"):
        screenplay_generator._reconcile_scene_action_and_storyboard_llm(**_inputs())


def test_reconciliation_preserves_scene_fields_and_sends_full_context(monkeypatch):
    captured = {}
    source = _inputs()
    response = {
        "scene_number": 1,
        "action": source["scene"]["action"],
        "storyboard": source["scene"]["storyboard"],
        "voiceover": "Нейтральный закадровый текст",
        "timing": "00:02",
        "scene_continuity_facts": {
            "space": "склад",
            "boundary": "дверь",
            "participants": "двое сотрудников",
        },
    }

    def fake_llm(**kwargs):
        captured.update(kwargs)
        return response

    monkeypatch.setattr(screenplay_generator, "_llm_json_call", fake_llm)
    result = screenplay_generator._reconcile_scene_action_and_storyboard_llm(**source)

    assert result == response
    assert result["voiceover"] == response["voiceover"]
    assert result["timing"] == response["timing"]
    assert captured["payload"]["story_anchors"] == source["story_anchors"]
    assert captured["payload"]["screenplay_outline"] == source["screenplay_outline"]
    assert captured["payload"]["scene"] == source["scene"]


def test_reconciliation_rejects_missing_scene_response(monkeypatch):
    monkeypatch.setattr(screenplay_generator, "_llm_json_call", lambda **kwargs: None)

    with pytest.raises(ValueError, match="no scene"):
        screenplay_generator._reconcile_scene_action_and_storyboard_llm(**_inputs())
