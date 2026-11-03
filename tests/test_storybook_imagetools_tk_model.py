"""Tk-модели зума, правки и галереи иллюстраций (без дисплея)."""

import json
from pathlib import Path

import pytest

from StoryBookManager.gui.imagetools import edit_model as EM
from StoryBookManager.gui.imagetools import gallery_model as GM
from StoryBookManager.gui.imagetools import zoom_model as ZM

VECTORS = json.loads((Path(__file__).parent / "fixtures/storybook_screens/golden_imagetools_vectors.json")
                     .read_text(encoding="utf-8"))["vectors"]
FUNCS = {f.__name__: f for f in (ZM.zoom_step, ZM.fit_scale, ZM.zoom_percent, ZM.pan_allowed, EM.edit_can_submit,
                                 EM.edit_button, EM.edit_error_text, EM.editable_extension, GM.gallery_groups,
                                 GM.gallery_filter, GM.file_info_lines)}


@pytest.mark.parametrize("name", sorted(FUNCS))
def test_vectors(name):
    for case in VECTORS[name]:
        assert json.loads(json.dumps(FUNCS[name](*case["in"]))) == case["out"], case["in"]


# --- зум ---

def state(view=(800, 600), image=(1600, 1200)):
    st = ZM.ZoomState()
    st.set_image(*image)
    st.set_view(*view)
    return st


def test_new_image_fits_window():
    st = state()
    assert st.fit_mode and st.scale == 0.475 and st.percent == "48%"
    assert not st.can_pan()


def test_steps_actual_and_fit_roundtrip():
    st = state()
    st.step("in")
    assert not st.fit_mode and st.scale == pytest.approx(0.5938, abs=1e-4)
    st.actual()
    assert st.scale == 1.0 and st.can_pan()
    st.fit()
    assert st.fit_mode and st.scale == 0.475 and (st.dx, st.dy) == (0, 0)


def test_limits():
    st = state()
    for _ in range(40):
        st.step("in")
    assert st.scale == ZM.ZOOM_MAX
    for _ in range(80):
        st.step("out")
    assert st.scale == ZM.ZOOM_MIN


def test_resize_recomputes_only_in_fit_mode():
    st = state()
    st.set_view(400, 300)
    assert st.scale == 0.2375
    st.step("in")
    manual = st.scale
    st.set_view(1600, 1200)
    assert st.scale == manual and not st.fit_mode


def test_pan_only_when_larger_and_limited():
    st = state()
    st.pan(50, 50)
    assert (st.dx, st.dy) == (0, 0)
    st.actual()
    st.pan(10000, -10000)
    assert st.dx == (1600 - 800) / 2 and st.dy == -(1200 - 600) / 2


def test_listing_new_image_resets_zoom():
    st = state()
    st.actual()
    st.pan(30, 0)
    st.set_image(100, 100)
    assert st.fit_mode and st.dx == 0


# --- правка ---

def test_start_payload_known_cost():
    preview = {"plan_digest": "d", "estimate": {"cost_rub": 5.0, "unknown_cost": False}}
    target = {"shot_key": "1-1", "part": "start", "path": "p"}
    payload = EM.start_payload(target, "  сделай небо  ", ["a.png"], preview)
    assert payload == {"shot_key": "1-1", "part": "start", "prompt": "сделай небо", "expected_plan_digest": "d",
                       "surface": "tk", "reference_paths": ["a.png"], "confirm_cost_rub": 5.0}


def test_start_payload_unknown_cost_confirms_unknown_only():
    preview = {"plan_digest": "d", "estimate": {"cost_rub": 0.0, "unknown_cost": True}}
    payload = EM.start_payload({"path": "50_images/page_001/a.png", "shot_key": "", "part": ""}, "abcde", [], preview)
    assert payload["confirm_unknown_cost"] is True
    assert "confirm_cost_rub" not in payload and "reference_paths" not in payload
    assert payload["path"] == "50_images/page_001/a.png" and "shot_key" not in payload


def test_start_payload_free_has_no_confirmation():
    preview = {"plan_digest": "d", "estimate": {"cost_rub": 0.0, "unknown_cost": False}}
    payload = EM.start_payload({"path": "x", "shot_key": "", "part": ""}, "abcde", [], preview)
    assert "confirm_cost_rub" not in payload and "confirm_unknown_cost" not in payload


def test_reference_choices_skip_target_and_non_images():
    items = [{"path": "97_shots/s1/a.png", "name": "a.png", "servable": True},
             {"path": "50_images/page_001/b.jpg", "name": "b.jpg", "servable": True},
             {"path": "x/readme.txt", "name": "readme.txt", "servable": True}, {"path": "", "name": "e.png", "servable": True}]
    assert EM.reference_choices(items, "97_shots/s1/a.png") == [{"path": "50_images/page_001/b.jpg",
                                                                  "label": "page_001/b.jpg"}]


def test_reference_choices_only_servable_sorted_by_label_like_react():
    def item(path, servable):
        return {"path": path, "name": path.rsplit("/", 1)[-1], "servable": servable}

    items = [item("50_images/page_10/a.png", True), item("50_images/page_2/b.png", True),
             item("50_images/page_3/hidden.png", False), {"path": "50_images/page_4/c.png", "name": "c.png"},
             item("50_images/page_1/z.png", True)]
    got = EM.reference_choices(items, "none")
    assert [c["label"] for c in got] == ["page_1/z.png", "page_2/b.png", "page_10/a.png"]


# --- галерея ---

def test_filtered_groups_keep_same_names_on_different_pages():
    items = [{"path": "50_images/page_001/a.png", "name": "a.png", "page": 1},
             {"path": "50_images/page_002/a.png", "name": "a.png", "page": 2},
             {"path": "50_images/page_002/b.png", "name": "b.png", "page": 2}]
    groups = GM.filtered_groups(items, "A.")
    assert [(g["page"], [i["path"] for i in g["items"]]) for g in groups] == [
        (1, ["50_images/page_001/a.png"]), (2, ["50_images/page_002/a.png"])]
    assert GM.filtered_groups(items, "zzz") == []
