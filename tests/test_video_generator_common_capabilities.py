"""Э0.1: parse_duration_seconds_from_timing, реэкспорт каталога AITunnel,
video_generator_common.py как лист графа импортов, resolve_video_model_capabilities.
"""

import ast
import json
import re

import pytest

import custom_tools.storybook.video_generator_common as vgc
import custom_tools.storybook.video_generator_aitunnel_tool as aitunnel_module

_RESOLVED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_EXPECTED_KEYS = {
    "tool_name",
    "model",
    "supported_durations",
    "supported_sizes",
    "supported_resolutions",
    "supported_aspect_ratios",
    "supported_modes",
    "source",
    "resolved_at",
    "warnings",
}


# ---------------------------------------------------------------------------
# parse_duration_seconds_from_timing (починка дефекта раздела 2.5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "timing, expected",
    [
        ("00:04", 4),
        ("00:00 - 00:06", 6),
        ("6s", 6),
        ("6", 6),
        ("garbage", 5),
    ],
)
def test_parse_duration_seconds_from_timing(timing, expected):
    assert vgc.parse_duration_seconds_from_timing(timing) == expected


# ---------------------------------------------------------------------------
# Перенос загрузчика каталога AITunnel: реэкспорт, не дубль
# ---------------------------------------------------------------------------

def test_get_aitunnel_video_models_reexported_same_object():
    assert aitunnel_module._get_aitunnel_video_models is vgc._get_aitunnel_video_models


# ---------------------------------------------------------------------------
# video_generator_common.py остаётся листом графа импортов
# ---------------------------------------------------------------------------

def test_video_generator_common_has_no_storybook_imports_at_module_level():
    import custom_tools.storybook.video_generator_common as module

    source = ast.parse(open(module.__file__, encoding="utf-8").read())
    for node in source.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("custom_tools.storybook"), alias.name
        elif isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            assert not module_name.startswith("custom_tools.storybook"), module_name


# ---------------------------------------------------------------------------
# resolve_video_model_capabilities
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tool_name, expected",
    [
        (
            "video_generator_mm_tool",
            {
                "model": "MiniMax-Hailuo-02",
                "supported_durations": [6, 10],
                "supported_sizes": [],
                "supported_resolutions": ["768P"],
                "supported_aspect_ratios": [],
                "supported_modes": [],
            },
        ),
        (
            "video_generator_tool",
            {
                "model": "kling-v2-1",
                "supported_durations": [5, 10],
                "supported_sizes": [],
                "supported_resolutions": [],
                "supported_aspect_ratios": ["16:9"],
                "supported_modes": ["pro"],
            },
        ),
        (
            "video_generator_veo_tool",
            {
                "model": "veo-3.1-generate-preview",
                "supported_durations": [4, 6, 8],
                "supported_sizes": [],
                "supported_resolutions": ["720p"],
                "supported_aspect_ratios": ["16:9"],
                "supported_modes": [],
            },
        ),
    ],
)
def test_resolve_video_model_capabilities_constant_branch(tmp_path, tool_name, expected):
    caps_path = tmp_path / "97_shots" / "video_model_caps.json"

    result = vgc.resolve_video_model_capabilities(tool_name, str(caps_path))

    assert result["tool_name"] == tool_name
    for key, value in expected.items():
        assert result[key] == value
    assert result["source"] == "constant"
    assert result["warnings"] == []
    assert _RESOLVED_AT_RE.match(result["resolved_at"])
    assert set(result.keys()) == _EXPECTED_KEYS

    on_disk = json.loads(caps_path.read_text(encoding="utf-8"))
    assert on_disk == result


def test_resolve_video_model_capabilities_unknown_tool_name(tmp_path):
    caps_path = tmp_path / "97_shots" / "video_model_caps.json"

    result = vgc.resolve_video_model_capabilities("video_generator_totally_unknown", str(caps_path))

    assert result["tool_name"] == "video_generator_totally_unknown"
    assert result["model"] is None
    assert result["supported_durations"] == []
    assert result["source"] is None
    assert len(result["warnings"]) == 1
    warning = result["warnings"][0]
    assert warning["code"] == "P14"
    assert warning["level"] == "warning"
    assert isinstance(warning["message"], str) and warning["message"]
    assert isinstance(warning["details"], dict)
    assert caps_path.exists()


def test_resolve_video_model_capabilities_aitunnel_without_env_var_skips_network(tmp_path, monkeypatch):
    monkeypatch.delenv("AITUNNEL_VIDEO_MODEL", raising=False)
    caps_path = tmp_path / "97_shots" / "video_model_caps.json"

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("сеть не должна дёргаться без AITUNNEL_VIDEO_MODEL")

    monkeypatch.setattr(vgc, "_get_aitunnel_video_models", _forbidden)

    result = vgc.resolve_video_model_capabilities("video_generator_aitunnel_tool", str(caps_path))

    assert result["source"] is None
    assert result["model"] is None
    assert result["supported_durations"] == []
    assert len(result["warnings"]) == 1
    assert result["warnings"][0]["code"] == "P14"
    assert caps_path.exists()


def test_resolve_video_model_capabilities_aitunnel_catalog_success(tmp_path, monkeypatch):
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "installed-model")
    caps_path = tmp_path / "97_shots" / "video_model_caps.json"

    catalog = {
        "installed-model": {
            "supported_durations": [4, 8],
            "supported_sizes": ["1280x720"],
            "supported_resolutions": ["720p"],
            "supported_aspect_ratios": ["16:9"],
            "supported_frame_images": ["first_frame", "last_frame"],
        },
    }
    monkeypatch.setattr(vgc, "_get_aitunnel_video_models", lambda *a, **k: catalog)

    result = vgc.resolve_video_model_capabilities("video_generator_aitunnel_tool", str(caps_path))

    assert result["tool_name"] == "video_generator_aitunnel_tool"
    assert result["model"] == "installed-model"
    assert result["supported_durations"] == [4, 8]
    assert result["supported_sizes"] == ["1280x720"]
    assert result["supported_resolutions"] == ["720p"]
    assert result["supported_aspect_ratios"] == ["16:9"]
    assert result["supported_modes"] == []
    assert result["source"] == "catalog"
    assert result["warnings"] == []
    assert _RESOLVED_AT_RE.match(result["resolved_at"])
    assert set(result.keys()) == _EXPECTED_KEYS

    on_disk = json.loads(caps_path.read_text(encoding="utf-8"))
    assert on_disk == result


def test_resolve_video_model_capabilities_aitunnel_catalog_fails_falls_back_to_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "installed-model")
    caps_path = tmp_path / "97_shots" / "video_model_caps.json"
    caps_path.parent.mkdir(parents=True)
    previous = {
        "tool_name": "video_generator_aitunnel_tool",
        "model": "installed-model",
        "supported_durations": [5, 7, 10],
        "supported_sizes": ["1280x720"],
        "supported_resolutions": ["720p"],
        "supported_aspect_ratios": ["16:9"],
        "supported_modes": [],
        "source": "catalog",
        "resolved_at": "2020-01-01T00:00:00Z",
        "warnings": [],
    }
    caps_path.write_text(json.dumps(previous), encoding="utf-8")

    def _failing(*_args, **_kwargs):
        raise RuntimeError("AITUNNEL models endpoint failed: 500 - boom")

    monkeypatch.setattr(vgc, "_get_aitunnel_video_models", _failing)

    result = vgc.resolve_video_model_capabilities("video_generator_aitunnel_tool", str(caps_path))

    assert result["source"] == "cache"
    assert result["resolved_at"] == "2020-01-01T00:00:00Z"
    assert result["supported_durations"] == [5, 7, 10]
    assert result["model"] == "installed-model"
    assert len(result["warnings"]) == 1
    assert result["warnings"][0]["code"] == "P14"

    on_disk = json.loads(caps_path.read_text(encoding="utf-8"))
    assert on_disk == result


def test_resolve_video_model_capabilities_aitunnel_catalog_fails_no_previous_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "installed-model")
    caps_path = tmp_path / "97_shots" / "video_model_caps.json"

    def _failing(*_args, **_kwargs):
        raise RuntimeError("AITUNNEL models endpoint failed: 500 - boom")

    monkeypatch.setattr(vgc, "_get_aitunnel_video_models", _failing)

    result = vgc.resolve_video_model_capabilities("video_generator_aitunnel_tool", str(caps_path))

    assert result["source"] is None
    assert result["model"] is None
    assert result["supported_durations"] == []
    assert len(result["warnings"]) == 1
    assert result["warnings"][0]["code"] == "P14"
    assert caps_path.exists()


def test_resolve_video_model_capabilities_aitunnel_previous_caps_with_empty_durations_not_used_as_cache(
    tmp_path, monkeypatch
):
    # Регресс: прежний video_model_caps.json существует, но его supported_durations
    # пуст (записан предыдущей неудачной попыткой) — откат на "cache" запрещён,
    # иначе пустой набор самоподдерживался бы как штатный успех вечно (раздел 6.1 ТЗ).
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "installed-model")
    caps_path = tmp_path / "97_shots" / "video_model_caps.json"
    caps_path.parent.mkdir(parents=True)
    previous = {
        "tool_name": "video_generator_aitunnel_tool",
        "model": None,
        "supported_durations": [],
        "supported_sizes": [],
        "supported_resolutions": [],
        "supported_aspect_ratios": [],
        "supported_modes": [],
        "source": None,
        "resolved_at": "2020-01-01T00:00:00Z",
        "warnings": [],
    }
    caps_path.write_text(json.dumps(previous), encoding="utf-8")

    def _failing(*_args, **_kwargs):
        raise RuntimeError("AITUNNEL models endpoint failed: 500 - boom")

    monkeypatch.setattr(vgc, "_get_aitunnel_video_models", _failing)

    result = vgc.resolve_video_model_capabilities("video_generator_aitunnel_tool", str(caps_path))

    assert result["source"] is None
    assert result["supported_durations"] == []
    # resolved_at не скопирован из пустого кэша: это неудачная попытка, а не source=cache.
    assert result["resolved_at"] != "2020-01-01T00:00:00Z"
    assert len(result["warnings"]) == 1
    assert result["warnings"][0]["code"] == "P14"

    on_disk = json.loads(caps_path.read_text(encoding="utf-8"))
    assert on_disk == result


def test_resolve_video_model_capabilities_aitunnel_p14_message_distinguishes_causes(tmp_path, monkeypatch):
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "installed-model")

    def _failing(*_args, **_kwargs):
        raise RuntimeError("AITUNNEL models endpoint failed: 500 - boom")

    monkeypatch.setattr(vgc, "_get_aitunnel_video_models", _failing)
    caps_path_unreachable = tmp_path / "unreachable" / "video_model_caps.json"
    unreachable_result = vgc.resolve_video_model_capabilities(
        "video_generator_aitunnel_tool", str(caps_path_unreachable)
    )
    unreachable_message = unreachable_result["warnings"][0]["message"]
    assert "недоступен" in unreachable_message
    assert "отсутствует в каталоге" not in unreachable_message

    monkeypatch.setattr(vgc, "_get_aitunnel_video_models", lambda *a, **k: {"other-model": {}})
    caps_path_missing_model = tmp_path / "missing_model" / "video_model_caps.json"
    missing_model_result = vgc.resolve_video_model_capabilities(
        "video_generator_aitunnel_tool", str(caps_path_missing_model)
    )
    missing_model_message = missing_model_result["warnings"][0]["message"]
    assert "отсутствует в каталоге" in missing_model_message
    assert "недоступен" not in missing_model_message

    assert unreachable_message != missing_model_message
