"""Каталог визуальных стилей storybook (config/storybook/prompts/style_library.yaml).

Зеркало tests/test_qa_prompt_domain_neutralization.py:
  * конфиг грузится, профиль ``default`` обязателен, пресеты валидируются;
  * подмена yaml через env-путь подменяет источник; нет файла -> FileNotFoundError;
    неизвестный профиль -> KeyError;
  * неизвестный style_preset_id от LLM -> fallback-пресет, без падения;
  * merge: пресет — база, LLM правит только разрешённые поля;
  * negative seed не запрещает то, что сам пресет обязан показать;
  * style_keeper пишет style_images.json с материальностью и негатив с seed'ом;
  * prompt_engineer подставляет блок материальности в system-промпт.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from custom_tools.storybook import style_library_config as sl


_ENV_PATH = "STORYBOOK_STYLE_LIBRARY_PATH"
_ENV_PROFILE = "STORYBOOK_STYLE_PROFILE"

_REPO = Path(__file__).resolve().parents[1]
_YAML = _REPO / "config" / "storybook" / "prompts" / "style_library.yaml"
_PROMPT_ENGINEER_PY = _REPO / "custom_tools" / "storybook" / "prompt_engineer.py"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    sl.reset_cache()
    yield
    monkeypatch.delenv(_ENV_PATH, raising=False)
    monkeypatch.delenv(_ENV_PROFILE, raising=False)
    sl.reset_cache()


# --------------------------------------------------------------------------
# загрузка и валидация
# --------------------------------------------------------------------------

def test_default_profile_loads_with_presets():
    profile = sl.get_active_style_profile()
    assert profile.name == "default"
    assert len(profile.presets) >= 5
    assert profile.fallback_preset_id in profile.presets


def test_every_preset_carries_material_signature_and_negatives():
    for preset in sl.get_active_style_profile().presets.values():
        assert preset.material_signature, preset.id
        assert preset.negative_seed, preset.id
        assert preset.pitfalls, preset.id


def test_missing_config_raises(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_PATH, str(tmp_path / "absent.yaml"))
    sl.reset_cache()
    with pytest.raises(FileNotFoundError):
        sl.load_style_library_config()


def test_unknown_profile_raises(monkeypatch):
    monkeypatch.setenv(_ENV_PROFILE, "no_such_profile")
    sl.reset_cache()
    with pytest.raises(KeyError):
        sl.get_active_style_profile()


def test_env_path_overrides_source(monkeypatch, tmp_path):
    custom = tmp_path / "custom.yaml"
    custom.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "profiles": {
                    "default": {
                        "fallback_preset_id": "only",
                        "presets": [
                            {
                                "id": "only",
                                "title": "Only",
                                "when_to_use": "always",
                                "art_style": "test style",
                                "color_palette": ["mono"],
                                "lighting": "flat",
                                "texture": "none",
                                "detail_density": "low",
                                "material_signature": "test signature",
                                "negative_seed": ["blur"],
                                "pitfalls": ["test pitfall"],
                            }
                        ],
                    }
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV_PATH, str(custom))
    sl.reset_cache()
    assert sl.get_active_style_profile().preset_ids() == ["only"]


def test_preset_without_material_signature_is_rejected(monkeypatch, tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "profiles": {
                    "default": {
                        "fallback_preset_id": "x",
                        "presets": [
                            {
                                "id": "x",
                                "title": "X",
                                "when_to_use": "n/a",
                                "art_style": "s",
                                "color_palette": ["c"],
                                "lighting": "l",
                                "texture": "t",
                                "detail_density": "d",
                                "negative_seed": ["n"],
                                "pitfalls": ["p"],
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV_PATH, str(broken))
    sl.reset_cache()
    with pytest.raises(ValueError, match="material_signature"):
        sl.load_style_library_config()


# --------------------------------------------------------------------------
# выбор пресета и merge
# --------------------------------------------------------------------------

def test_known_preset_id_resolves():
    assert sl.resolve_preset("cinematic_photoreal").id == "cinematic_photoreal"


@pytest.mark.parametrize("bad_id", ["", None, "does_not_exist", 42])
def test_unknown_preset_id_falls_back(bad_id):
    profile = sl.get_active_style_profile()
    assert sl.resolve_preset(bad_id).id == profile.fallback_preset_id


def test_merge_keeps_preset_as_base():
    preset = sl.resolve_preset("cinematic_photoreal")
    merged = sl.merge_preset_into_style_images(preset, None)
    assert merged["style_preset_id"] == "cinematic_photoreal"
    assert merged["art_style"] == preset.art_style
    assert merged["material_signature"] == preset.material_signature
    assert merged["camera_defaults"] == preset.camera_defaults
    assert preset.pitfalls[0] in merged["style_pitfalls"]


def test_merge_applies_only_allowed_overrides():
    preset = sl.resolve_preset("storybook_watercolor")
    merged = sl.merge_preset_into_style_images(
        preset,
        {
            "lighting": "cold moonlight",
            "color_palette": ["ice blue", "slate"],
            "do_not_include": ["modern electronics"],
            "project_note": "зимняя история",
            # запрещённые к переопределению поля
            "art_style": "photorealistic 8k render",
            "material_signature": "smooth digital gradient",
            "camera_defaults": {"close_up": "hacked"},
        },
    )
    assert merged["lighting"] == "cold moonlight"
    assert merged["color_palette"] == ["ice blue", "slate"]
    assert merged["do_not_include"] == ["modern electronics"]
    assert merged["project_note"] == "зимняя история"
    assert merged["art_style"] == preset.art_style
    assert merged["material_signature"] == preset.material_signature
    assert merged["camera_defaults"] == preset.camera_defaults


def test_merge_ignores_empty_overrides():
    preset = sl.resolve_preset("storybook_watercolor")
    merged = sl.merge_preset_into_style_images(
        preset, {"lighting": "   ", "color_palette": [], "detail_density": None}
    )
    assert merged["lighting"] == preset.lighting
    assert merged["color_palette"] == preset.color_palette
    assert merged["detail_density"] == preset.detail_density


# --------------------------------------------------------------------------
# негативы
# --------------------------------------------------------------------------

def test_negative_seed_merges_universal_and_preset_without_duplicates():
    profile = sl.get_active_style_profile()
    preset = sl.resolve_preset("documentary_realism")
    seed = sl.compose_negative_seed(preset)
    assert profile.universal_negative_seed[0] in seed
    assert "studio lighting" in seed
    assert len(seed) == len({term.casefold() for term in seed})


def test_negative_seed_never_forbids_what_the_preset_must_show():
    """Запрет не должен отменять признак носителя самого пресета."""
    for preset in sl.get_active_style_profile().presets.values():
        must_show = " ".join(
            [
                preset.art_style,
                preset.material_signature,
                preset.texture,
                preset.lighting,
            ]
        ).casefold()
        for term in sl.compose_negative_seed(preset):
            assert term.casefold() not in must_show, (
                f"пресет {preset.id}: запрет '{term}' конфликтует с обязательным стилем"
            )


# --------------------------------------------------------------------------
# блок материальности для промпт-инженера
# --------------------------------------------------------------------------

def test_style_directives_expose_materiality_optics_and_pitfalls():
    preset = sl.resolve_preset("cinematic_photoreal")
    block = sl.compose_style_directives(sl.merge_preset_into_style_images(preset, None))
    assert preset.material_signature in block
    assert "85mm f/1.8" in block
    assert preset.pitfalls[0] in block


@pytest.mark.parametrize("payload", [None, {}, "", {"model": "illustration"}])
def test_style_directives_empty_for_styleless_input(payload):
    assert sl.compose_style_directives(payload) == ""


def test_video_branch_carries_material_signature():
    """Признак носителя должен доезжать и до видео-ветки, иначе стиль разъедется."""
    shared = (
        _REPO / "custom_tools" / "storybook"
        / "screenplay_shots_generator_utils" / "shared_utils.py"
    ).read_text(encoding="utf-8")
    screenplay = (
        _REPO / "custom_tools" / "storybook" / "screenplay_generator.py"
    ).read_text(encoding="utf-8")
    assert '"material_signature"' in shared
    assert 'style_data.get("material_signature"' in screenplay


def test_prompt_engineer_substitutes_style_placeholder():
    src = _PROMPT_ENGINEER_PY.read_text(encoding="utf-8")
    assert src.count("__STYLE_DIRECTIVES__") == 2, (
        "плейсхолдер должен и объявляться в system-промпте, и подставляться"
    )
    assert 'system.replace("__STYLE_DIRECTIVES__", compose_style_directives(' in src


# --------------------------------------------------------------------------
# контракт style_keeper
# --------------------------------------------------------------------------

@pytest.fixture
def _storybook_project(tmp_path, monkeypatch):
    project_id = "style_test"
    base = tmp_path / "plots" / "storybooks" / project_id
    (base / "10_synopsis").mkdir(parents=True)
    (base / "20_bible").mkdir(parents=True)
    (base / "10_synopsis" / "synopsis.json").write_text("{}", encoding="utf-8")
    (base / "10_synopsis" / "beats.json").write_text("[]", encoding="utf-8")
    (base / "20_bible" / "characters.json").write_text("[]", encoding="utf-8")
    (base / "20_bible" / "locations.json").write_text("[]", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return project_id, base


def _llm_reply(preset_id: str, negative: str = "city traffic") -> str:
    return json.dumps(
        {
            "style_text": {"narrative_voice": "мягкий"},
            "style_preset_id": preset_id,
            "style_overrides": {"lighting": "low evening sun"},
            "negative_list": negative,
        },
        ensure_ascii=False,
    )


def test_style_keeper_writes_preset_backed_artifacts(_storybook_project, monkeypatch):
    project_id, base = _storybook_project
    from custom_tools.storybook import style_keeper

    captured = {}

    def _fake_call(prompt, system_prompt, **kwargs):
        captured["system"] = system_prompt
        return _llm_reply("documentary_realism")

    monkeypatch.setattr(style_keeper, "call_openai_api", _fake_call)
    style_keeper.style_keeper_tool(session_id="s", project_id=project_id)

    # каталог пресетов уехал в system-промпт
    assert "documentary_realism" in captured["system"]
    assert "style_preset_id" in captured["system"]

    style_images = json.loads(
        (base / "30_style" / "style_images.json").read_text(encoding="utf-8")
    )
    assert style_images["style_preset_id"] == "documentary_realism"
    assert style_images["material_signature"]
    assert style_images["camera_defaults"]
    assert style_images["style_pitfalls"]
    assert style_images["lighting"] == "low evening sun"

    negative = (base / "30_style" / "negative_prompt_list.txt").read_text(
        encoding="utf-8"
    )
    assert "studio lighting" in negative       # стилевой seed пресета
    assert "watermark" in negative             # универсальный seed
    assert "city traffic" in negative          # запрет самой истории


def test_style_keeper_survives_unknown_preset_id(_storybook_project, monkeypatch):
    project_id, base = _storybook_project
    from custom_tools.storybook import style_keeper

    monkeypatch.setattr(
        style_keeper,
        "call_openai_api",
        lambda prompt, system_prompt, **kwargs: _llm_reply("totally_invented_style"),
    )
    style_keeper.style_keeper_tool(session_id="s", project_id=project_id)

    style_images = json.loads(
        (base / "30_style" / "style_images.json").read_text(encoding="utf-8")
    )
    assert style_images["style_preset_id"] == (
        sl.get_active_style_profile().fallback_preset_id
    )
    assert style_images["material_signature"]


def test_style_keeper_negative_has_no_duplicates(_storybook_project, monkeypatch):
    project_id, base = _storybook_project
    from custom_tools.storybook import style_keeper

    monkeypatch.setattr(
        style_keeper,
        "call_openai_api",
        lambda prompt, system_prompt, **kwargs: _llm_reply(
            "storybook_watercolor", negative="watermark, watermark, 3d render"
        ),
    )
    style_keeper.style_keeper_tool(session_id="s", project_id=project_id)

    terms = [
        t.strip().casefold()
        for t in (base / "30_style" / "negative_prompt_list.txt")
        .read_text(encoding="utf-8")
        .split(",")
        if t.strip()
    ]
    assert len(terms) == len(set(terms))
