"""style_keeper: профиль и пресет из настроек прогона."""

import json
import logging
import shutil

import pytest

from custom_tools.storybook import style_keeper

SOURCE_QUOTE = "Визуальный стиль: акварель и коллаж."


@pytest.fixture
def project(tmp_path, monkeypatch):
    base = tmp_path / "st"
    (base / "10_synopsis").mkdir(parents=True)
    (base / "20_bible").mkdir(parents=True)
    (base / "10_synopsis" / "synopsis.json").write_text("{}", encoding="utf-8")
    (base / "10_synopsis" / "beats.json").write_text("[]", encoding="utf-8")
    (base / "20_bible" / "characters.json").write_text("[]", encoding="utf-8")
    (base / "20_bible" / "locations.json").write_text("[]", encoding="utf-8")
    (base / "00_brief.json").write_text(json.dumps({"storybook_prompt": SOURCE_QUOTE}), encoding="utf-8")
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setenv("TEXT2IMAGE_MODEL", "m")
    return base


def reply(preset, quote=""):
    payload = {"style_preset_id": preset, "style_text": {}, "negative_list": "", "source_style_quote": quote,
               "style_overrides": {"art_style": "a", "texture": "t", "material_signature": "m"} if quote else {}}
    return json.dumps(payload, ensure_ascii=False)


def run(monkeypatch, project, preset_from_model, quote="", **kwargs):
    monkeypatch.setattr(style_keeper, "call_openai_api", lambda **kw: reply(preset_from_model, quote))
    shutil.rmtree(project / "30_style", ignore_errors=True)  # готовый стиль инструмент не пересоздаёт
    style_keeper.style_keeper_tool(session_id="s", project_id="st", **kwargs)
    return json.loads((project / "30_style" / "style_images.json").read_text(encoding="utf-8"))


def two_presets():
    from custom_tools.storybook.style_library_config import get_active_style_profile
    ids = list(get_active_style_profile().presets)
    assert len(ids) >= 2
    return ids[0], ids[1]


def test_chosen_preset_replaces_model_choice(project, monkeypatch):
    first, second = two_presets()
    chosen = run(monkeypatch, project, first, style_preset=second)
    default = run(monkeypatch, project, first)
    forced = run(monkeypatch, project, first, style_preset=second)
    assert forced == chosen
    assert forced != default


def test_unknown_preset_keeps_model_choice_with_warning(project, monkeypatch, caplog):
    first, _ = two_presets()
    default = run(monkeypatch, project, first)
    with caplog.at_level(logging.WARNING):
        result = run(monkeypatch, project, first, style_preset="no_such_preset")
    assert result == default
    assert any("no_such_preset" in r.getMessage() for r in caplog.records)


def test_preset_ignored_with_warning_when_style_from_quote(project, monkeypatch, caplog):
    first, second = two_presets()
    with caplog.at_level(logging.WARNING):
        result = run(monkeypatch, project, first, style_preset=second, quote=SOURCE_QUOTE)
    assert result["source_style_quote"] == SOURCE_QUOTE
    assert any("игнорируется" in r.getMessage() for r in caplog.records)


def test_empty_params_do_not_change_behavior(project, monkeypatch):
    first, _ = two_presets()
    assert run(monkeypatch, project, first, style_profile="", style_preset="") == run(monkeypatch, project, first)
