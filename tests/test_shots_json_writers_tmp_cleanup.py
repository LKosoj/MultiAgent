"""A38 (раздел 10.2 ТЗ): писатели `97_shots/shots.json` / `93_blockout/report.json`
не должны оставлять осиротевший временный файл вида `<path>.<pid>.tmp`, если
запись оборвалась исключением между созданием tmp-файла и `os.replace()`
(диск кончился, права, EXDEV). Уникальное имя с PID не даёт коллизий между
писателями, но сам файл раньше оставался на диске навсегда.

Покрывает три из шести писателей, у которых нет своего локального теста на этот
сценарий: `screenplay_shots_generator._merge_write_shots`,
`blockout_renderer._merge_write_shots_blockout_fields`,
`blockout_scene_builder.merge_write_report`. Остальные два (`shots_prompt_qa`,
`video_generator_common`) покрыты в своих собственных тестовых файлах.
Шестой (`file_manager.merge_write_shots_json`) — в
tests/test_file_manager_shots_protocol.py: тот файл уже настраивает sys.path
для импорта StoryBookManager (нужен `config.settings`), поэтому тест по тому
же сценарию логичнее держать рядом, чем дублировать здесь настройку sys.path.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

# --- stubs before importing the module under test (see other tests в tests/) ---
# agent_command НЕ подменяется: реальный модуль импортируется быстро и без сети
# (model_hard/model_code/... — ленивые атрибуты через __getattr__, реального
# запроса к LLM тесты этого файла не делают). Неполная заглушка здесь раньше
# «выигрывала» sys.modules у настоящего модуля для всех тестов, собранных позже
# в этой же сессии pytest, и ломала им импорт model_mapping/AGENT_PROFILES/etc.
utils_stub = types.ModuleType("utils")
utils_stub.call_openai_api = lambda *a, **k: "{}"
utils_stub.extract_json_from_markdown = lambda t: t
utils_stub.parse_llm_json = lambda t: json.loads(t)
utils_stub.translate_prompts_in_items = lambda *a, **k: a[0]
sys.modules.setdefault("utils", utils_stub)

import custom_tools.storybook.screenplay_shots_generator as gen
from custom_tools.storybook import blockout_renderer as renderer
from custom_tools.storybook import blockout_scene_builder as sb


def _boom(*_args, **_kwargs):
    raise OSError("disk full")


def test_screenplay_merge_write_shots_removes_tmp_on_replace_failure(tmp_path, monkeypatch):
    shots_path = tmp_path / "shots.json"

    monkeypatch.setattr(gen.os, "replace", _boom)
    with pytest.raises(OSError):
        gen._merge_write_shots(str(shots_path), shots_data={"items": []}, partial=False)

    assert list(tmp_path.glob("*.tmp")) == []


def test_blockout_renderer_merge_write_shots_blockout_fields_removes_tmp_on_replace_failure(
    tmp_path, monkeypatch
):
    shots_path = tmp_path / "shots.json"
    shots_path.write_text(
        json.dumps({"items": [{"scene_number": 1, "shot_number": 1, "shot_type": "start"}]}),
        encoding="utf-8",
    )

    monkeypatch.setattr(renderer.os, "replace", _boom)
    with pytest.raises(OSError):
        renderer._merge_write_shots_blockout_fields(
            shots_path, {(1, 1, "start"): {"blockout_ref_image": "x.png"}}
        )

    assert list(tmp_path.glob("*.tmp")) == []


def test_blockout_scene_builder_merge_write_report_removes_tmp_on_replace_failure(tmp_path, monkeypatch):
    report_path = tmp_path / "report.json"

    monkeypatch.setattr(sb.os, "replace", _boom)
    with pytest.raises(OSError):
        sb.merge_write_report(report_path, "step_a", lambda section: {"x": 1})

    assert list(tmp_path.glob("*.tmp")) == []
