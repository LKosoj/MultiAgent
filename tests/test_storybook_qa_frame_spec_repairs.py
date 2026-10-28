import copy

from custom_tools.storybook import shots_prompt_qa as qa
from custom_tools.storybook.screenplay_shots_generator_utils import shared_utils


def _items():
    spec = {"primary_subject": "Hero", "must_show": ["Hero"]}
    return [
        {
            "scene_number": 1,
            "shot_number": 2,
            "shot_type": "start",
            "english_prompt": "start",
            "reference_image_paths": ["/ref.png"],
            "style_id": "main",
            "_shot_frame_spec": copy.deepcopy(spec),
            "_shot_frame_spec_cache_key": "scene-1-shot-2",
        },
        {
            "scene_number": 1,
            "shot_number": 2,
            "shot_type": "end",
            "english_prompt": "end",
            "reference_image_paths": ["/ref.png"],
            "style_id": "main",
            "_shot_frame_spec": copy.deepcopy(spec),
            "_shot_frame_spec_cache_key": "scene-1-shot-2",
        },
    ]


def test_frame_spec_repair_updates_both_phases_and_persists_cache(monkeypatch):
    items = _items()
    before = copy.deepcopy(items)
    corrected = {
        "primary_subject": "Hero",
        "must_show": ["Hero", "open gate"],
        "must_not_show": [],
    }
    normalize_calls = []
    store_calls = []

    def fake_normalize(raw, scene_characters):
        normalize_calls.append((raw, scene_characters))
        return copy.deepcopy(corrected)

    monkeypatch.setattr(qa, "_normalize_shot_frame_spec", fake_normalize, raising=False)
    monkeypatch.setattr(
        qa,
        "_store_shot_frame_spec_to_disk_cache",
        lambda **kwargs: store_calls.append(kwargs),
        raising=False,
    )
    with shared_utils._shot_frame_spec_lock:
        shared_utils._SHOT_FRAME_SPEC_CACHE["scene-1-shot-2"] = before[0]["_shot_frame_spec"]

    applied, rejected = qa._apply_repairs_to_items(
        items,
        [
            {
                "scene_number": 1,
                "shot_number": 2,
                "shot_type": "start",
                "set": {"_shot_frame_spec": {"must_show": ["Hero", "open gate"]}},
                "reasons": ["correct frame state"],
            }
        ],
        storyboard_lookup={(1, 2): {"characters": ["Hero"], "description": "Hero at gate"}},
    )

    assert applied == 1
    assert rejected == []
    assert len(normalize_calls) == 1
    assert all(item["_shot_frame_spec"] == corrected for item in items)
    assert all(item["reference_image_paths"] == before[0]["reference_image_paths"] for item in items)
    assert all(item["style_id"] == "main" for item in items)
    with shared_utils._shot_frame_spec_lock:
        assert shared_utils._SHOT_FRAME_SPEC_CACHE["scene-1-shot-2"] == corrected
    assert len(store_calls) == 1
    assert store_calls[0]["cache_key"] == "scene-1-shot-2"
    assert store_calls[0]["shot_frame_spec"] == corrected


def test_invalid_normalized_frame_spec_is_rejected_without_cache_or_mutation(monkeypatch):
    items = _items()
    before = copy.deepcopy(items)
    store_calls = []
    monkeypatch.setattr(qa, "_normalize_shot_frame_spec", lambda *args: {}, raising=False)
    monkeypatch.setattr(
        qa,
        "_store_shot_frame_spec_to_disk_cache",
        lambda **kwargs: store_calls.append(kwargs),
        raising=False,
    )
    with shared_utils._shot_frame_spec_lock:
        shared_utils._SHOT_FRAME_SPEC_CACHE.pop("scene-1-shot-2", None)

    applied, rejected = qa._apply_repairs_to_items(
        items,
        [
            {
                "scene_number": 1,
                "shot_number": 2,
                "shot_type": "start",
                "set": {"_shot_frame_spec": {"must_show": ["invented"]}},
                "reasons": ["invalid correction"],
            }
        ],
        storyboard_lookup={(1, 2): {"characters": ["Hero"]}},
    )

    assert applied == 0
    assert rejected
    assert items == before
    assert store_calls == []
    with shared_utils._shot_frame_spec_lock:
        assert "scene-1-shot-2" not in shared_utils._SHOT_FRAME_SPEC_CACHE


def test_frame_spec_normalization_uses_full_storyboard_character_allow_list(monkeypatch):
    items = _items()
    corrected = {
        "primary_subject": "Mira",
        "visible_characters": ["Hero", "Mira"],
        "must_show": ["Mira"],
    }
    received = []
    monkeypatch.setattr(
        qa,
        "_normalize_shot_frame_spec",
        lambda raw, scene_characters: received.append(scene_characters) or corrected,
        raising=False,
    )
    monkeypatch.setattr(
        qa,
        "_store_shot_frame_spec_to_disk_cache",
        lambda **kwargs: None,
        raising=False,
    )
    before_refs = [item["reference_image_paths"][:] for item in items]
    before_styles = [item["style_id"] for item in items]

    applied, rejected = qa._apply_repairs_to_items(
        items,
        [
            {
                "scene_number": 1,
                "shot_number": 2,
                "shot_type": "start",
                "set": {"_shot_frame_spec": {"visible_characters": ["Hero", "Mira"]}},
                "reasons": ["correct cast"],
            }
        ],
        storyboard_lookup={(1, 2): {"characters": ["Hero", "Mira"]}},
    )

    assert applied == 1
    assert rejected == []
    assert received == [["Hero", "Mira"]]
    assert all(item["_shot_frame_spec"] == corrected for item in items)
    assert [item["reference_image_paths"] for item in items] == before_refs
    assert [item["style_id"] for item in items] == before_styles
