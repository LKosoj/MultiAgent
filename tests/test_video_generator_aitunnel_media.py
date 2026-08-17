"""Э0.1: тай-брейк _select_best_supported_duration, откат на реестр
_get_active_aitunnel_model_record и регресс _resolve_model_and_size.

Э8 (раздел 11.3): _should_attach_blockout_reference / _build_reference_video_payload /
_payload_aspect_ratio — решение о подаче видео-референса болванки и его payload.
"""

import base64
import json

import pytest

from custom_tools.storybook.video_generator_aitunnel_media import (
    _build_reference_video_payload,
    _payload_aspect_ratio,
    _resolve_model_and_size,
    _select_best_supported_duration,
    _should_attach_blockout_reference,
)
from custom_tools.storybook.video_generator_common import _get_active_aitunnel_model_record


@pytest.mark.parametrize(
    "requested_duration, supported_durations, expected",
    [
        (6, [4, 8], 4),
        (6, [5, 7, 10], 5),
        (8, [6, 10], 6),
        (7, [5, 7, 10], 7),
        (9, [5, 7, 10], 10),
    ],
)
def test_select_best_supported_duration_ties_go_to_smaller(requested_duration, supported_durations, expected):
    assert _select_best_supported_duration(requested_duration, supported_durations) == expected


def test_resolve_model_and_size_raises_value_error_for_unknown_model():
    with pytest.raises(ValueError, match="не найдена"):
        _resolve_model_and_size(
            model_catalog={},
            configured_model="unknown-model",
            width=1920,
            height=1080,
            duration=6,
            requires_last_frame=False,
            seed=None,
        )


def test_get_active_aitunnel_model_record_known_model():
    catalog = {"installed-model": {"supported_durations": [6, 10]}}
    record = _get_active_aitunnel_model_record(catalog, "installed-model")
    assert record == {"supported_durations": [6, 10]}


def test_get_active_aitunnel_model_record_unknown_model():
    catalog = {"installed-model": {"supported_durations": [6, 10]}}
    assert _get_active_aitunnel_model_record(catalog, "missing-model") is None


def test_get_active_aitunnel_model_record_non_dict_entry():
    catalog = {"installed-model": ["not", "a", "dict"]}
    assert _get_active_aitunnel_model_record(catalog, "installed-model") is None


# === Э8: _payload_aspect_ratio (раздел 11.3, условие 4) =======================

def test_payload_aspect_ratio_prefers_explicit_aspect_ratio_key():
    assert _payload_aspect_ratio({"resolution": "720p", "aspect_ratio": "9:16"}) == "9:16"


def test_payload_aspect_ratio_derives_from_size_key():
    assert _payload_aspect_ratio({"size": "1920x1080"}) == "16:9"


def test_payload_aspect_ratio_none_when_nothing_to_compare():
    # раздел 11.3: запрос без сведений о соотношении сторон -> нечего сравнивать.
    assert _payload_aspect_ratio({}) is None
    assert _payload_aspect_ratio(None) is None


# === Э8: _build_reference_video_payload (раздел 11.3) ==========================

def test_build_reference_video_payload_passes_through_url():
    url = "https://cdn.example/blockout_ref.mp4"
    assert _build_reference_video_payload(url) == url


def test_build_reference_video_payload_encodes_local_file_as_data_url(tmp_path):
    video_path = tmp_path / "blockout_ref.mp4"
    video_path.write_bytes(b"fake-mp4-bytes")

    result = _build_reference_video_payload(str(video_path))

    assert result.startswith("data:video/mp4;base64,")
    encoded = result.split(",", 1)[1]
    assert base64.b64decode(encoded) == b"fake-mp4-bytes"


def test_build_reference_video_payload_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        _build_reference_video_payload("/no/such/blockout_ref.mp4")


# === Э8: _should_attach_blockout_reference (пять условий раздела 11.3) ========

def _blockout_item(blockout_video, junction_failed=False):
    item = {"blockout_video": str(blockout_video)}
    if junction_failed:
        item["blockout_junction_failed"] = True
    return item


def _write_manifest(blockout_video, resolution, duration_s):
    blockout_video.parent.mkdir(parents=True, exist_ok=True)
    blockout_video.write_bytes(b"clip")
    (blockout_video.parent / "manifest.json").write_text(
        json.dumps({"resolution": list(resolution), "duration_s": duration_s}), encoding="utf-8"
    )


def test_should_attach_blockout_reference_condition_1_flags(tmp_path):
    blockout_video = tmp_path / "shot" / "blockout_ref.mp4"
    _write_manifest(blockout_video, (1920, 1080), 6)
    item = _blockout_item(blockout_video)

    attach, reason = _should_attach_blockout_reference(item, False, True, 6, {"size": "1920x1080"})
    assert (attach, reason) == (False, "condition_1_reference_disabled")

    attach, reason = _should_attach_blockout_reference(item, True, False, 6, {"size": "1920x1080"})
    assert (attach, reason) == (False, "condition_1_reference_disabled")


def test_should_attach_blockout_reference_condition_2_missing_file(tmp_path):
    item = _blockout_item(tmp_path / "shot" / "does_not_exist.mp4")
    attach, reason = _should_attach_blockout_reference(item, True, True, 6, {"size": "1920x1080"})
    assert (attach, reason) == (False, "condition_2_video_missing")


def test_should_attach_blockout_reference_condition_3_duration_mismatch(tmp_path):
    blockout_video = tmp_path / "shot" / "blockout_ref.mp4"
    _write_manifest(blockout_video, (1920, 1080), 8)
    item = _blockout_item(blockout_video)

    attach, reason = _should_attach_blockout_reference(item, True, True, 6, {"size": "1920x1080"})
    assert (attach, reason) == (False, "condition_3_duration_mismatch")


def test_should_attach_blockout_reference_condition_4_aspect_mismatch(tmp_path):
    blockout_video = tmp_path / "shot" / "blockout_ref.mp4"
    _write_manifest(blockout_video, (1080, 1920), 6)  # 9:16
    item = _blockout_item(blockout_video)

    attach, reason = _should_attach_blockout_reference(item, True, True, 6, {"size": "1920x1080"})  # 16:9
    assert (attach, reason) == (False, "condition_4_aspect_mismatch")


def test_should_attach_blockout_reference_condition_4_satisfied_when_nothing_to_compare(tmp_path):
    """раздел 11.3: если запрос не несёт сведений о соотношении сторон вовсе,
    условие 4 считается выполненным."""
    blockout_video = tmp_path / "shot" / "blockout_ref.mp4"
    _write_manifest(blockout_video, (1080, 1920), 6)
    item = _blockout_item(blockout_video)

    attach, reason = _should_attach_blockout_reference(item, True, True, 6, {})
    assert (attach, reason) == (True, str(blockout_video))


def test_should_attach_blockout_reference_condition_5_junction_failed(tmp_path):
    blockout_video = tmp_path / "shot" / "blockout_ref.mp4"
    _write_manifest(blockout_video, (1920, 1080), 6)
    item = _blockout_item(blockout_video, junction_failed=True)

    attach, reason = _should_attach_blockout_reference(item, True, True, 6, {"size": "1920x1080"})
    assert (attach, reason) == (False, "condition_5_junction_failed")


def test_should_attach_blockout_reference_all_conditions_met(tmp_path):
    blockout_video = tmp_path / "shot" / "blockout_ref.mp4"
    _write_manifest(blockout_video, (1920, 1080), 6)
    item = _blockout_item(blockout_video)

    attach, path = _should_attach_blockout_reference(item, True, True, 6, {"size": "1920x1080"})
    assert (attach, path) == (True, str(blockout_video))
