"""Runtime-контракты PP01--PP05 и PL01 без сети или реальных моделей."""
from __future__ import annotations

from pathlib import Path

import pytest
import requests
import yaml

import utils
from custom_tools.storybook import artist_batch_edit as artist
from custom_tools.storybook import protagonist_initializer as protagonist


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "agent_profiles/artist_agent.yaml"


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    def _offline(*_args, **_kwargs):
        raise AssertionError("network access is forbidden in prompt contract tests")

    monkeypatch.setattr(requests.sessions.Session, "request", _offline)


class _RecordingAgent:
    def __init__(self, output=""):
        self.output = output

    def run(self, _task, stream=False):
        assert stream is False
        return self.output


class _RecordingFactory:
    def __init__(self, output=""):
        self.output = output
        self.calls = []

    def create_agent(self, **kwargs):
        self.calls.append(kwargs)
        return _RecordingAgent(self.output)


def _assert_generation_contract(task: str):
    for unsupported in ("true_cfg_scale:", "num_inference_steps:", "output_path:", "seed:"):
        assert unsupported not in task
    for supported in ("prompt:", "session_id:", "number:", "negative_prompt:", "width:", "height:"):
        assert supported in task


def _character_item(reference_path: str):
    return {
        "reference_image_paths": [reference_path],
        "characters": [{
            "reference_image_path": reference_path,
            "name": "Mira",
            "immutable_attributes": {"face_shape": "oval", "eye_color": "emerald"},
            "variable_attributes": {"base_clothing": "red coat", "accessories": ["silver pin"]},
        }],
    }


def test_pp01_direct_edit_receives_built_negative_prompt(monkeypatch, tmp_path):
    reference = tmp_path / "reference.png"
    output = tmp_path / "result.png"
    reference.write_bytes(b"png")
    captured = []

    def fake_edit(**kwargs):
        captured.append(kwargs)
        Path(kwargs["output_path"]).write_bytes(b"png")
        return "ok"

    monkeypatch.setattr(artist, "edit_image_vse_tool", fake_edit)
    monkeypatch.setattr(artist, "_preprocess_canon_references", lambda **_kwargs: [])
    monkeypatch.setattr(artist, "_collect_visible_text_bindings", lambda *_args: [])
    monkeypatch.setattr(artist, "log_smolagents_panel", lambda **_kwargs: None)
    artist.artist_agent_batch_edit_tool(
        session_id="session-pp01",
        items={"items": [{
            "english_prompt": "A blue room",
            "negative_prompt": "UNIQUE_NEGATIVE_MARKER",
            "reference_image_paths": [str(reference)],
            "output_path": str(output),
            "width": 111,
            "height": 222,
        }], "consistency_rules": []},
        max_concurrency=1,
        language="en",
    )

    assert len(captured) == 1
    call = captured[0]
    assert "UNIQUE_NEGATIVE_MARKER" in call["negative_prompt"]
    assert call["image_paths"] == [str(reference)]
    assert call["width"] == 111
    assert call["height"] == 222


def test_pp02_identity_is_retained_once_in_english_and_sent_to_translation(monkeypatch):
    item = _character_item("/tmp/mira-reference.png")
    translations = []
    monkeypatch.setattr(artist, "_collect_visible_text_bindings", lambda *_args: [])
    monkeypatch.setattr(utils, "needs_translation_to_english", lambda _item: False)
    monkeypatch.setattr(utils, "translate_prompts_in_items", lambda *_args, **_kwargs: pytest.fail("unexpected translation"))

    english_prompt, _negative = artist._build_image_generation_prompts(item, "A detective enters", "en")
    assert english_prompt.count("emerald") == 1

    def translate(prompt_item, language, **kwargs):
        translations.append((prompt_item, language, kwargs))
        return dict(prompt_item)

    monkeypatch.setattr(utils, "needs_translation_to_english", lambda _item: True)
    monkeypatch.setattr(utils, "translate_prompts_in_items", translate)
    translated_prompt, _negative = artist._build_image_generation_prompts(item, "Детектив входит", "ru")
    assert translations[0][1] == "en"
    assert translations[0][0]["english_prompt"].count("emerald") == 1
    assert translated_prompt.count("emerald") == 1


def test_pp03_canon_translation_reaches_direct_edit_and_agent_task(monkeypatch, tmp_path):
    reference = tmp_path / "reference.png"
    output = tmp_path / "canon.png"
    reference.write_bytes(b"png")
    translations = []
    direct_calls = []
    factory = _RecordingFactory()

    def translate(prompt_item, language, **_kwargs):
        translations.append((prompt_item, language))
        assert set(prompt_item) == {"english_prompt"}
        return {"english_prompt": "CONTROL_TRANSLATION"}

    def fake_edit(**kwargs):
        direct_calls.append(kwargs)
        Path(kwargs["output_path"]).write_bytes(b"png")
        return "ok"

    monkeypatch.setattr(utils, "translate_prompts_in_items", translate)
    monkeypatch.setattr(artist, "build_canon_image_prompt", lambda *_args: "смешанный canon")
    monkeypatch.setattr(artist, "edit_image_vse_tool", fake_edit)
    monkeypatch.setattr(artist, "AgentFactory", lambda: factory)

    assert artist._create_canon_reference(
        "session-pp03", "character", {"name": "Mira"}, [str(reference)],
        str(output), [],
    )
    assert direct_calls[0]["prompt"] == "CONTROL_TRANSLATION"

    artist._generate_canon_reference_from_scratch(
        "session-pp03", "character", {"name": "Mira"}, str(tmp_path / "new.png"), [], "workflow",
    )
    assert "CONTROL_TRANSLATION" in factory.calls[0]["task"]
    assert len(translations) == 2


def test_pp04_runtime_generation_tasks_match_registered_schema(monkeypatch, tmp_path):
    factory = _RecordingFactory()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(artist, "AgentFactory", lambda: factory)
    monkeypatch.setattr(artist, "edit_image_vse_tool", lambda **_kwargs: "ok")
    monkeypatch.setattr(artist, "_preprocess_canon_references", lambda **_kwargs: [])
    monkeypatch.setattr(artist, "_collect_visible_text_bindings", lambda *_args: [])
    monkeypatch.setattr(artist, "log_smolagents_panel", lambda **_kwargs: None)
    monkeypatch.setattr(utils, "translate_prompts_in_items", lambda item, _language, **_kwargs: item)

    artist.artist_agent_batch_edit_tool(
        session_id="session-base",
        items={"items": [{"english_prompt": "A room", "output_path": str(tmp_path / "image.png")}], "consistency_rules": []},
        max_concurrency=1,
        language="en",
    )
    artist._generate_image_from_scratch(
        "session-protagonist", "project", {"name": "Mira"}, str(tmp_path / "hero.png"), "workflow",
    )
    monkeypatch.setattr(artist, "build_canon_image_prompt", lambda *_args: "canon")
    artist._generate_canon_reference_from_scratch(
        "session-canon", "character", {"name": "Mira"}, str(tmp_path / "canon.png"), [], "workflow",
    )

    assert len(factory.calls) == 3
    for call in factory.calls:
        _assert_generation_contract(call["task"])


def test_pp05_artist_profile_uses_registered_tool_and_task_dimensions():
    profile_text = PROFILE.read_text(encoding="utf-8")
    profile = yaml.safe_load(profile_text)
    assert "generate_image_tool" in profile["tools"]
    assert "generate_image(" not in profile_text
    assert "1024x1024" not in profile_text
    assert "width" in profile["prompt_templates"]
    assert "height" in profile["prompt_templates"]


@pytest.mark.parametrize("characters", [None, []])
def test_pl01_fallback_prompt_reaches_agent_for_absent_or_empty_canon(monkeypatch, tmp_path, characters):
    factory = _RecordingFactory()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(protagonist, "AgentFactory", lambda: factory)
    project_dir = tmp_path / "plots" / "storybooks" / "project"
    if characters is not None:
        characters_path = project_dir / "20_bible" / "characters.json"
        characters_path.parent.mkdir(parents=True)
        characters_path.write_text("[]", encoding="utf-8")

    protagonist.protagonist_initializer_tool("session-pl01", "project")
    assert len(factory.calls) == 1
    _assert_generation_contract(factory.calls[0]["task"])
    assert "Hero protagonist full-body, neutral pose, clean background" in factory.calls[0]["task"]


def test_pl01_existing_brief_image_skips_agent(monkeypatch, tmp_path):
    factory = _RecordingFactory()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(protagonist, "AgentFactory", lambda: factory)
    source = tmp_path / "source.png"
    source.write_bytes(b"png")
    brief = tmp_path / "plots" / "storybooks" / "project" / "00_brief.json"
    brief.parent.mkdir(parents=True)
    brief.write_text('{"hero_image": "' + str(source) + '"}', encoding="utf-8")

    output = protagonist.protagonist_initializer_tool("session-pl01", "project")
    assert Path(output).read_bytes() == b"png"
    assert factory.calls == []
