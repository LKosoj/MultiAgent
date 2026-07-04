"""WS-A / M-1 + M-2 + H-2: контракт надёжности storybook_pipeline.yaml.

Проверяем на самом yaml (декларативный контракт):
  * M-2: удалён декоративный no-op global_resource_limits.max_duration_seconds
    (но max_api_calls_per_minute сохранён);
  * M-1: удалён декоративный error_handling.save_checkpoint_interval;
  * M-1: error_handling.auto_retry_transient присутствует (реализованный ключ);
  * H-2: дорогие side-effect-шаги (video_generator, storybook_music_generator,
    montage_assembler) помечены metadata.retryable=false (движковый retry запрещён).
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "workflow_pipelines" / "storybook_pipeline.yaml"

_NON_RETRYABLE_STEPS = {"video_generator", "storybook_music_generator", "montage_assembler"}


def _load_raw():
    with open(PIPELINE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_max_duration_seconds_removed_from_resource_limits():
    data = _load_raw()
    grl = data.get("global_resource_limits", {})
    # M-2: no-op wall-clock дедлайн удалён из yaml.
    assert "max_duration_seconds" not in grl, (
        "max_duration_seconds не enforce-ится (M-2) и должен быть убран из yaml"
    )
    # Реально применяемый лимит сохранён.
    assert grl.get("max_api_calls_per_minute") == 15


def test_save_checkpoint_interval_removed_from_error_handling():
    data = _load_raw()
    eh = data.get("error_handling", {})
    # M-1: декоративный интервал чекпойнта удалён (чекпойнт пишется after_each_step).
    assert "save_checkpoint_interval" not in eh, (
        "save_checkpoint_interval декоративен (M-1) и должен быть убран из yaml"
    )


def test_auto_retry_transient_declared():
    data = _load_raw()
    eh = data.get("error_handling", {})
    # M-1: реализованный ключ — движок читает его и при false выставляет max_retries=0.
    assert "auto_retry_transient" in eh
    assert isinstance(eh["auto_retry_transient"], bool)


def test_expensive_side_effect_steps_marked_non_retryable():
    data = _load_raw()
    steps = {s["id"]: s for s in data.get("steps", [])}
    for step_id in _NON_RETRYABLE_STEPS:
        assert step_id in steps, f"шаг {step_id} пропал из пайплайна"
        metadata = steps[step_id].get("metadata", {})
        assert metadata.get("retryable") is False, (
            f"дорогой side-effect-шаг {step_id} должен быть metadata.retryable=false (H-2)"
        )
