import json

import pytest

from custom_tools.storybook import style_keeper


def test_ensure_image_style_catalog_creates_once_then_uses_cache(tmp_path, monkeypatch):
    base = str(tmp_path / "project")
    catalog = {
        "default_style_id": "main",
        "styles": {"main": {"prompt": "paper and ink"}},
    }
    calls = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        return json.dumps(catalog, ensure_ascii=False)

    monkeypatch.setattr(style_keeper, "call_openai_api", fake_call_openai_api)
    first = style_keeper.ensure_image_style_catalog(base, {"art_style": "paper"})
    assert len(calls) == 1
    assert first["default_style_id"] == "main"
    assert first["styles"] == catalog["styles"]
    assert first["frame_bindings"] == {}

    def fail_call(**kwargs):
        raise AssertionError("cached catalog must not call the model")

    monkeypatch.setattr(style_keeper, "call_openai_api", fail_call)
    second = style_keeper.ensure_image_style_catalog(base, {"art_style": "paper"})
    assert second == first

    saved = json.loads(
        (tmp_path / "project" / "30_style" / "image_styles.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["default_style_id"] == "main"
    assert saved["styles"] == catalog["styles"]
    assert saved["frame_bindings"] == {}


@pytest.mark.parametrize(
    "response",
    [
        {"styles": {"main": {"prompt": "paper"}}},
        {"default_style_id": "main", "styles": {"main": {"prompt": ""}}},
    ],
)
def test_ensure_image_style_catalog_rejects_invalid_profile(
    tmp_path, monkeypatch, response
):
    monkeypatch.setattr(
        style_keeper,
        "call_openai_api",
        lambda **kwargs: json.dumps(response, ensure_ascii=False),
    )
    with pytest.raises(ValueError):
        style_keeper.ensure_image_style_catalog(
            str(tmp_path / "project"), {"art_style": "paper"}
        )


def test_ensure_image_style_catalog_empty_input_skips_model(tmp_path, monkeypatch):
    monkeypatch.setattr(
        style_keeper,
        "call_openai_api",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("empty style input must not call the model")
        ),
    )
    assert style_keeper.ensure_image_style_catalog(str(tmp_path / "project"), {}) == {}


def test_style_change_rebuilds_cached_catalog(tmp_path, monkeypatch):
    prompts = []
    def generate(**kwargs):
        prompts.append(kwargs["prompt"])
        return json.dumps({"default_style_id": "main", "styles": {"main": {"prompt": f"STYLE {len(prompts)}"}}})
    monkeypatch.setattr(style_keeper, "call_openai_api", generate)
    first = style_keeper.ensure_image_style_catalog(str(tmp_path), {"art_style": "old"})
    second = style_keeper.ensure_image_style_catalog(str(tmp_path), {"art_style": "new"})
    assert len(prompts) == 2
    assert first["source_hash"] != second["source_hash"]
    assert second["styles"]["main"]["prompt"] == "STYLE 2"


def test_catalog_uses_project_style_instead_of_stale_episode_input(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    source = root / 'shared/style/main/current/data.json'
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({'style_images': {'art_style': 'current clean photography'}}))
    (root / 'project.json').write_text(json.dumps({'resources': {'style': {'main': 'current'}}}))
    requests = []
    def generate(**kwargs):
        requests.append(json.loads(kwargs['prompt']))
        return json.dumps({'default_style_id': 'main', 'styles': {'main': {'prompt': 'clean photography'}}})
    monkeypatch.setattr(style_keeper, 'call_openai_api', generate)
    style_keeper.ensure_image_style_catalog(str(root / 'series/one'), {'art_style': 'old VHS'})
    assert requests[0]['style_images'] == {'art_style': 'current clean photography'}
