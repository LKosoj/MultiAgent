"""Э0.3: интеграционные тесты на уровне screenplay_shots_generator_tool.

Покрывает мандатный минимум ТЗ (docs/tz-blockout-reference-pipeline.md, раздел 6.1/6.2):
- B15/P14 развилка (пустой/недоступный supported_durations) в обе стороны;
- принудительная пересборка FCPXML на каждом прогоне, даже когда файлы уже есть
  (обе ранние ветки short-circuit);
- "запись 2" video_model_caps.json — предупреждения дописываются, а не заменяют файл.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import custom_tools.storybook.screenplay_shots_generator as shots_generator
from custom_tools.storybook.screenplay_shots_generator_utils import shared_utils

# Как в test_storybook_shots_resume_merge.py/test_storybook_paths_env.py: без этого
# патча black_screen_storyboard_shot внутри _process_scene_worker пытается сделать
# реальный HTTP-вызов (и падает с ошибкой сети), когда файл запускается отдельно от
# файлов, которые патчат shared_utils.call_openai_api на уровне модуля первыми.
shared_utils.call_openai_api = lambda *args, **kwargs: '{"is_black_screen": false}'


class _FakeFuture:
    def __init__(self, fn, *a, **k):
        self._exc = None
        self._result = None
        try:
            self._result = fn(*a, **k)
        except Exception as exc:  # noqa: BLE001
            self._exc = exc

    def result(self):
        if self._exc is not None:
            raise self._exc
        return self._result


class _FakeExecutor:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def submit(self, fn, *a, **k):
        return _FakeFuture(fn, *a, **k)


def _fake_as_completed(futures):
    return list(futures)


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _setup_project(root: Path, project_id: str, scenes):
    screenplay = [
        {
            "scene_number": sn,
            "action": f"action {sn}",
            "characters": ["Герой"],
            "storyboard": [
                {"shot_number": shot, "description": f"desc {sn}-{shot}", "camera_plan": "Close-up", "timing": "5s"}
                for shot in shots
            ],
        }
        for sn, shots in scenes
    ]
    base = Path(root) / project_id
    _write_json(base / "91_screenplay" / "screenplay.json", {"screenplay": screenplay})
    _write_json(base / "20_bible" / "characters.json", [{"name": "Герой"}])
    _write_json(base / "20_bible" / "locations.json", [])
    return base


def _install_fakes(monkeypatch, *, fcpxml_calls=None, photo_fcpxml_calls=None, photo_fcpxml_return=True):
    def fake_build_ctx(**kwargs):
        scene = kwargs.get("scene") or {}
        return {
            "scene_number": scene.get("scene_number"),
            "shot_number": kwargs.get("shot_number"),
            "shot_frame_spec": {"primary_subject": "Герой", "must_show": ["Герой"]},
            "full_shot_frame_spec": {"primary_subject": "Герой", "must_show": ["Герой"]},
            "shot_frame_spec_cache_key": f"s{scene.get('scene_number')}_{kwargs.get('shot_number')}",
            "scene_continuity_facts": {},
            "location_time": "",
            "location_canon_name": "",
        }

    def fake_prompt(extended_context, shot_type, video_prompt="", start_llm_result=None, language="en"):
        sn = extended_context.get("scene_number")
        return {
            "english_prompt": f"prompt {sn} {shot_type}",
            "negative_prompt": "neg",
            "characters": ["Герой"],
            "main_subject": "Герой",
            "camera_position": "front",
            "character_orientation": "front",
            "spatial_composition": "c",
            "point_of_view": "objective",
            "initial_state_summary": "s",
            "reference_image_paths": [],
            "reference_roles_instruction": "",
            "video_prompt": "",
            "add_end_shot": "false",
            "should_link_as_next_start": "false",
            "should_use_prev_end_as_reference": "false",
        }

    def fake_fcpxml(*a, **k):
        if fcpxml_calls is not None:
            fcpxml_calls.append(1)
        return None

    def fake_photo_fcpxml(*a, **k):
        if photo_fcpxml_calls is not None:
            photo_fcpxml_calls.append(1)
        return photo_fcpxml_return

    monkeypatch.setattr(shots_generator, "ThreadPoolExecutor", _FakeExecutor)
    monkeypatch.setattr(shots_generator, "as_completed", _fake_as_completed)
    monkeypatch.setattr(shots_generator, "_build_extended_context", fake_build_ctx)
    monkeypatch.setattr(shots_generator, "_generate_shot_prompt", fake_prompt)
    monkeypatch.setattr(shots_generator, "_generate_transition_video_prompt", lambda **k: "transition")
    monkeypatch.setattr(shots_generator, "_generate_fcpxml", fake_fcpxml)
    monkeypatch.setattr(shots_generator, "_generate_photo_fcpxml", fake_photo_fcpxml)


def _load(base: Path):
    return json.loads((base / "97_shots" / "shots.json").read_text(encoding="utf-8"))


def _load_caps(base: Path):
    return json.loads((base / "97_shots" / "video_model_caps.json").read_text(encoding="utf-8"))


# === B15/P14 развилка ============================================================

def test_b15_blocks_when_capabilities_unresolved_and_generate_blockout_true(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    _setup_project(root, pid, [(1, [1])])

    _install_fakes(monkeypatch)
    monkeypatch.setattr(shots_generator, "_active_video_tool_name", lambda: None)  # неизвестная модель

    result = shots_generator.screenplay_shots_generator_tool(
        session_id="s1", project_id=pid, generate_end_shots=False, language="ru",
        generate_blockout=True,
    )
    assert "error" in result
    assert result["error"].startswith("B15")
    assert result["items"] == []
    # Генерация не запускалась вовсе.
    shots_path = root / pid / "97_shots" / "shots.json"
    assert not shots_path.exists()


def test_p14_continues_without_normalization_when_generate_blockout_false(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    base = _setup_project(root, pid, [(1, [1])])

    _install_fakes(monkeypatch)
    monkeypatch.setattr(shots_generator, "_active_video_tool_name", lambda: None)

    result = shots_generator.screenplay_shots_generator_tool(
        session_id="s1", project_id=pid, generate_end_shots=False, language="ru",
        generate_blockout=False,
    )
    assert "error" not in result
    assert len(result["items"]) == 1
    # duration_s НЕ проставлен нормализацией (набор длительностей недоступен).
    assert "duration_s" not in result["items"][0]

    caps = _load_caps(base)
    assert any(w["code"] == "P14" for w in caps["warnings"])


# === Принудительная пересборка FCPXML (блок 9) ===================================

def test_fcpxml_rebuilt_on_legacy_short_circuit_even_if_files_exist(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    base = _setup_project(root, pid, [(1, [1])])

    shots_dir = base / "97_shots"
    _write_json(shots_dir / "shots.json", {
        "items": [{"scene_number": 1, "shot_number": 1, "shot_type": "start", "timing": "00:05"}],
        "seed": 1,
    })  # legacy: без generation_completed
    (shots_dir / "shots_timeline.fcpxml").write_text("<old/>", encoding="utf-8")
    (shots_dir / "photo_shots_timeline.fcpxml").write_text("<old/>", encoding="utf-8")

    fcpxml_calls: list = []
    photo_calls: list = []
    _install_fakes(monkeypatch, fcpxml_calls=fcpxml_calls, photo_fcpxml_calls=photo_calls)
    monkeypatch.setattr(
        shots_generator, "resolve_video_model_capabilities",
        lambda *a, **k: {"supported_durations": [5, 7, 10], "warnings": []},
    )

    shots_generator.screenplay_shots_generator_tool(
        session_id="s1", project_id=pid, generate_end_shots=False, language="ru",
    )
    assert fcpxml_calls == [1]
    assert photo_calls == [1]


def test_fcpxml_rebuilt_on_generation_completed_short_circuit_even_if_files_exist(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    base = _setup_project(root, pid, [(1, [1])])
    screenplay = json.loads((base / "91_screenplay" / "screenplay.json").read_text(encoding="utf-8"))

    seed = 42
    inputs_hash = shots_generator._compute_inputs_hash(
        screenplay_data=screenplay, characters_data=[{"name": "Герой"}], locations_data=[],
        consistency_rules=[], style_images_data={}, seed=seed, language="ru", generate_end_shots=False,
    )
    shots_dir = base / "97_shots"
    _write_json(shots_dir / "shots.json", {
        "items": [{"scene_number": 1, "shot_number": 1, "shot_type": "start", "timing": "00:05"}],
        "seed": seed, "generation_completed": True, "completed_scenes": [1], "inputs_hash": inputs_hash,
    })
    (shots_dir / "shots_timeline.fcpxml").write_text("<old/>", encoding="utf-8")
    (shots_dir / "photo_shots_timeline.fcpxml").write_text("<old/>", encoding="utf-8")

    fcpxml_calls: list = []
    photo_calls: list = []
    _install_fakes(monkeypatch, fcpxml_calls=fcpxml_calls, photo_fcpxml_calls=photo_calls)
    monkeypatch.setattr(
        shots_generator, "resolve_video_model_capabilities",
        lambda *a, **k: {"supported_durations": [5, 7, 10], "warnings": []},
    )

    result = shots_generator.screenplay_shots_generator_tool(
        session_id="s1", project_id=pid, generate_end_shots=False, language="ru",
    )
    assert result["generation_completed"] is True
    assert fcpxml_calls == [1]
    assert photo_calls == [1]


# === "Запись 2" video_model_caps.json дописывает, а не заменяет =================

def test_second_caps_write_appends_p17_without_erasing_write1_content(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    base = _setup_project(root, pid, [(1, [1])])

    _install_fakes(monkeypatch)
    # Реальный resolve_video_model_capabilities (константная ветка реестра, без
    # сети) — нужен настоящий побочный эффект "записи 1" (файл на диске), чтобы
    # append_video_model_caps_warnings ("запись 2") было куда дописывать.
    monkeypatch.setattr(shots_generator, "_active_video_tool_name", lambda: "video_generator_mm_tool")

    shots_generator.screenplay_shots_generator_tool(
        session_id="s1", project_id=pid, generate_end_shots=False, language="ru",
        screenplay_time=100,
    )
    caps = _load_caps(base)
    # write-1 поля сохранены (не заменены записью 2).
    assert caps["model"] == "MiniMax-Hailuo-02"
    assert caps["source"] == "constant"
    # write-2 дописала P17 (справочная строка, т.к. передан screenplay_time).
    assert any(w["code"] == "P17" for w in caps["warnings"])


def test_p19_appended_when_no_paired_shots(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    base = _setup_project(root, pid, [(1, [1])])

    _install_fakes(monkeypatch, photo_fcpxml_return=False)  # ни одной пары start/end
    monkeypatch.setattr(shots_generator, "_active_video_tool_name", lambda: "video_generator_mm_tool")

    shots_generator.screenplay_shots_generator_tool(
        session_id="s1", project_id=pid, generate_end_shots=False, language="ru",
    )
    caps = _load_caps(base)
    assert any(w["code"] == "P19" for w in caps["warnings"])
