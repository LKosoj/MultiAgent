import json
from pathlib import Path
import pytest

from PIL import Image

from custom_tools import image_tools
from custom_tools.storybook import style_keeper as styles


def test_style_selection_uses_saved_bindings_without_model(monkeypatch):
    monkeypatch.setattr(styles, "call_openai_api", lambda **kwargs: (_ for _ in ()).throw(AssertionError("No per-frame model")))
    catalog = {"default_style_id": "base", "styles": {"base": {}, "cold": {}, "warm": {}},
               "location_bindings": {"Yard": "cold", "Island": "warm"}, "frame_bindings": {"1:2:start": "base"}}
    assert styles.resolve_image_style_id(catalog, "1:1:start", [{"name": "Yard"}], "base") == "cold"
    assert styles.resolve_image_style_id(catalog, "1:2:start", [{"name": "Yard"}], "base") == "base"
    assert styles.resolve_image_style_id(catalog, "2:1:start", [{"name": "Island"}]) == "warm"


def test_legacy_catalog_upgrades_regional_styles_and_removes_automatic_default_bindings(tmp_path, monkeypatch):
    (tmp_path / "30_style").mkdir()
    (tmp_path / "20_bible").mkdir()
    (tmp_path / "30_style/image_styles.json").write_text(json.dumps({
        "default_style_id": "base", "styles": {"base": {"prompt": "Ink"}},
        "frame_bindings": {"1:1:start": "base"}, "page_bindings": {"page_1": "base"}}))
    (tmp_path / "20_bible/locations.json").write_text(json.dumps([{"name": "Yard"}, {"name": "Island"}]))
    monkeypatch.setattr(styles, "call_openai_api", lambda **kwargs: json.dumps({
        "default_style_id": "base", "styles": {"base": {"prompt": "Ink"}, "cold": {"prompt": "Cold ink"},
                                                 "warm": {"prompt": "Warm ink"}},
        "location_bindings": {"Yard": "cold", "Island": "warm"}}))
    result = styles.ensure_image_style_catalog(str(tmp_path), {"art_style": "Ink", "color_palette": ["cold", "warm"]})
    assert result["frame_bindings"] == {}
    assert result["page_bindings"] == {"page_1": "base"}
    assert result["location_bindings"] == {"Yard": "cold", "Island": "warm"}
    assert styles.ensure_image_style_catalog(str(tmp_path), {}) == result


def test_style_images_are_dedicated_cached_and_regenerated_only_for_changed_style(tmp_path, monkeypatch):
    (tmp_path / "30_style").mkdir()
    catalog = {"default_style_id": "cold", "styles": {"cold": {"prompt": "Cold ink on paper"},
                                                      "warm": {"prompt": "Warm gouache"}}, "frame_bindings": {},
               "location_bindings": {"ColdRoom": "cold", "WarmRoom": "warm"}}
    assets = {}
    for name in ("cold_hero", "warm_hero", "cold_location", "warm_location", "other_hero"):
        path = tmp_path / f"{name}.png"
        Image.new("RGB", (16, 16), "gray").save(path)
        assets[name] = str(path)
    items = [{"scene_number": 1, "shot_number": n, "style_id": style_id,
              "characters": [{"reference_image_path": assets[hero]}],
              "locations": [{"name": location, "reference_image_path": assets[style_id + "_location"]}]}
             for n, style_id, hero, location in [(1, "cold", "other_hero", "ColdRoom"),
                                                (2, "cold", "cold_hero", "ColdRoom"),
                                                (3, "cold", "cold_hero", "ColdRoom"),
                                                (4, "warm", "warm_hero", "WarmRoom")]]
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        path = Path(kwargs["output_path"])
        Image.new("RGB", (16, 16), "gray").save(path)
        return str(path)

    monkeypatch.setattr(image_tools, "edit_image_vse_tool", generate)
    monkeypatch.setattr(image_tools, "generate_image_tool", lambda **kwargs: pytest.fail("Style requires source images"))
    assert styles.ensure_video_style_references(tmp_path, catalog, [], "s", 42, items) == {}
    assert calls == []
    with pytest.raises(ValueError, match="character and one location"):
        styles.ensure_video_style_references(tmp_path, catalog, ["cold"], "s", 42, [])
    for missing in ("characters", "locations"):
        incomplete = [{**item, missing: []} for item in items]
        with pytest.raises(ValueError, match="character and one location"):
            styles.ensure_video_style_references(tmp_path, catalog, ["cold"], "s", 42, incomplete)
    assert calls == []
    first = styles.ensure_video_style_references(tmp_path, catalog, ["cold", "warm"], "s", 42, items)
    assert len(calls) == 2 and all(Path(p).is_file() for p in first.values())
    assert all(c["width"] == 1920 and c["height"] == 1088 and c["seed"] == 42 for c in calls)
    assert {tuple(c["image_paths"]) for c in calls} == {
        (assets["cold_hero"], assets["cold_location"]), (assets["warm_hero"], assets["warm_location"])}
    assert first["cold"] != first["warm"]
    assert styles.ensure_video_style_references(tmp_path, catalog, ["cold", "warm"], "s", 42, items) == first
    assert len(calls) == 2
    catalog["styles"]["warm"]["prompt"] = "Warm ink wash"
    changed = styles.ensure_video_style_references(tmp_path, catalog, ["cold", "warm"], "s", 42, items)
    assert len(calls) == 3
    assert changed["cold"] == first["cold"] and changed["warm"] != first["warm"]
    Image.new("RGB", (16, 16), "blue").save(assets["warm_hero"])
    new_source = styles.ensure_video_style_references(tmp_path, catalog, ["cold", "warm"], "s", 42, items)
    assert len(calls) == 4
    assert new_source["cold"] == changed["cold"] and new_source["warm"] != changed["warm"]
    assert json.loads((tmp_path / "30_style/image_styles.json").read_text())["reference_images"] == new_source
