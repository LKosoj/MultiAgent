"""WS-D: resume/merge honesty for screenplay_shots_generator.

Покрывает C-2 (маркер завершённости + resume), H-4 (union-merge superset без потери
фильма), H-5 (fail-closed на пустом START), M-26 (inputs_hash), L-10/L-11/L-12.
Тесты гоняются только in-process; проект пишется под STORYBOOK_PROJECTS_DIR=tmp_path.
"""

import json
import logging
import sys
import types
from pathlib import Path

import pytest

# --- stubs before importing the module under test -------------------------------
agent_command_stub = types.ModuleType("agent_command")
agent_command_stub.model_hard = "test-model-hard"
agent_command_stub.model_code = "test-model-code"
agent_command_stub.model_ultimate = "test-model-ultimate"
agent_command_stub.model_lite = "test-model-lite"
sys.modules.setdefault("agent_command", agent_command_stub)

utils_stub = types.ModuleType("utils")
utils_stub.call_openai_api = lambda *args, **kwargs: '{"is_black_screen": false}'
utils_stub.extract_json_from_markdown = lambda text: text
utils_stub.parse_llm_json = lambda text: json.loads(text)
utils_stub.translate_prompts_in_items = lambda *args, **kwargs: args[0]
sys.modules.setdefault("utils", utils_stub)

from custom_tools.storybook import screenplay_shots_generator as shots_generator
from custom_tools.storybook.screenplay_shots_generator_utils import shared_utils

shared_utils.call_openai_api = lambda *args, **kwargs: '{"is_black_screen": false}'


# --- deterministic single-threaded executor -------------------------------------
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
    """scenes: list of (scene_number, [shot_numbers])."""
    screenplay = [
        {
            "scene_number": sn,
            "action": f"action {sn}",
            "characters": ["Герой"],
            "storyboard": [
                {
                    "shot_number": shot,
                    "description": f"desc {sn}-{shot}",
                    "camera_plan": "Close-up",
                    "timing": "1s",
                }
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


def _install_fakes(
    monkeypatch,
    *,
    generate_end_shots=False,
    recorder=None,
    empty_start_scene=None,
    add_end_bool=False,
    transition="video prompt",
):
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
        if recorder is not None and shot_type == "start":
            recorder.append(sn)
        if empty_start_scene is not None and sn == empty_start_scene and shot_type == "start":
            return {}
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
            "add_end_shot": True if add_end_bool else ("true" if generate_end_shots else "false"),
            "should_link_as_next_start": "false",
            "should_use_prev_end_as_reference": "false",
        }

    monkeypatch.setattr(shots_generator, "ThreadPoolExecutor", _FakeExecutor)
    monkeypatch.setattr(shots_generator, "as_completed", _fake_as_completed)
    monkeypatch.setattr(shots_generator, "_build_extended_context", fake_build_ctx)
    monkeypatch.setattr(shots_generator, "_generate_shot_prompt", fake_prompt)
    monkeypatch.setattr(shots_generator, "_generate_transition_video_prompt", lambda **k: transition)
    monkeypatch.setattr(shots_generator, "_generate_fcpxml", lambda *a, **k: None)
    monkeypatch.setattr(shots_generator, "_generate_photo_fcpxml", lambda *a, **k: None)
    # Э0.3: этот файл не про нормализацию длительности видео — фиксируем набор
    # supported_durations, чтобы новый блокирующий B15 (пустой набор + generate_blockout=True
    # по умолчанию) не мешал существующим сценариям resume/merge.
    monkeypatch.setattr(
        shots_generator, "resolve_video_model_capabilities",
        lambda *a, **k: {"supported_durations": [1, 5, 10], "warnings": []},
    )


def _load(base: Path):
    return json.loads((base / "97_shots" / "shots.json").read_text(encoding="utf-8"))


def _scene_set(saved):
    return {it["scene_number"] for it in saved["items"]}


# --------------------------------------------------------------------------------
def test_resume_regenerates_only_missing_scenes(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    base = _setup_project(root, pid, [(1, [1]), (2, [1]), (3, [1])])

    # run 1: scene 3 START пустой → fail-closed после чекпойнта сцен 1,2
    _install_fakes(monkeypatch, empty_start_scene=3)
    with pytest.raises(RuntimeError):
        shots_generator.screenplay_shots_generator_tool(
            session_id="s1", project_id=pid, generate_end_shots=False, language="ru"
        )
    saved = _load(base)
    assert saved["generation_completed"] is False
    assert saved["completed_scenes"] == [1, 2]
    assert _scene_set(saved) == {1, 2}

    # run 2 (resume): регенерируется только сцена 3, затем merge
    recorder = []
    _install_fakes(monkeypatch, recorder=recorder)
    shots_generator.screenplay_shots_generator_tool(
        session_id="s2", project_id=pid, generate_end_shots=False, language="ru"
    )
    assert recorder == [3]
    saved2 = _load(base)
    assert saved2["generation_completed"] is True
    assert saved2["completed_scenes"] == [1, 2, 3]
    assert _scene_set(saved2) == {1, 2, 3}


def test_partial_scope_merges_not_overwrites(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    base = _setup_project(root, pid, [(1, [1]), (2, [1]), (3, [1])])

    _install_fakes(monkeypatch)
    shots_generator.screenplay_shots_generator_tool(
        session_id="s1", project_id=pid, generate_end_shots=False, language="ru"
    )
    assert _scene_set(_load(base)) == {1, 2, 3}

    # partial scope: догенерация только сцены 2 не должна стереть 1 и 3 (риск потери фильма)
    recorder = []
    _install_fakes(monkeypatch, recorder=recorder)
    shots_generator.screenplay_shots_generator_tool(
        session_id="s2", project_id=pid, generate_end_shots=False, language="ru", scene_numbers=[2]
    )
    assert recorder == [2]
    saved = _load(base)
    assert _scene_set(saved) == {1, 2, 3}


def test_completed_marker_only_on_full_success(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    base = _setup_project(root, pid, [(1, [1]), (2, [1])])

    _install_fakes(monkeypatch)
    # partial-scope прогон (только сцена 1) не должен помечать фильм завершённым
    shots_generator.screenplay_shots_generator_tool(
        session_id="s1", project_id=pid, generate_end_shots=False, language="ru", scene_numbers=[1]
    )
    saved = _load(base)
    assert saved["generation_completed"] is False
    assert saved["completed_scenes"] == [1]
    assert _scene_set(saved) == {1}

    # полный прогон дозаполняет сцену 2 → фильм завершён
    _install_fakes(monkeypatch)
    shots_generator.screenplay_shots_generator_tool(
        session_id="s2", project_id=pid, generate_end_shots=False, language="ru"
    )
    saved2 = _load(base)
    assert saved2["generation_completed"] is True
    assert saved2["completed_scenes"] == [1, 2]


def test_short_circuit_requires_completed_marker(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    base = _setup_project(root, pid, [(1, [1]), (2, [1])])
    screenplay = json.loads((base / "91_screenplay" / "screenplay.json").read_text(encoding="utf-8"))

    seed = 12345
    good_hash = shots_generator._compute_inputs_hash(
        screenplay_data=screenplay,
        characters_data=[{"name": "Герой"}],
        locations_data=[],
        consistency_rules=[],
        style_images_data={},
        seed=seed,
        language="ru",
        generate_end_shots=False,
    )
    shots_path = base / "97_shots" / "shots.json"

    # (A) complete marker + совпавший hash → short-circuit, без генерации
    _write_json(
        shots_path,
        {
            "items": [{"scene_number": 1, "shot_number": 1, "shot_type": "start"}],
            "seed": seed,
            "generation_completed": True,
            "completed_scenes": [1, 2],
            "inputs_hash": good_hash,
        },
    )
    (base / "97_shots" / "shots_timeline.fcpxml").write_text("x", encoding="utf-8")
    recorder = []
    _install_fakes(monkeypatch, recorder=recorder)
    result = shots_generator.screenplay_shots_generator_tool(
        session_id="s1", project_id=pid, generate_end_shots=False, language="ru"
    )
    assert recorder == []
    assert result["generation_completed"] is True

    # (B) generation_completed=False (частичный checkpoint) → НЕ short-circuit, догенерация недостающей сцены
    _write_json(
        shots_path,
        {
            "items": [{"scene_number": 1, "shot_number": 1, "shot_type": "start"}],
            "seed": seed,
            "generation_completed": False,
            "completed_scenes": [1],
            "inputs_hash": good_hash,
        },
    )
    recorder_b = []
    _install_fakes(monkeypatch, recorder=recorder_b)
    shots_generator.screenplay_shots_generator_tool(
        session_id="s2", project_id=pid, generate_end_shots=False, language="ru"
    )
    assert recorder_b == [2]


def test_input_hash_invalidates_stale_shortcircuit(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    base = _setup_project(root, pid, [(1, [1])])
    shots_path = base / "97_shots" / "shots.json"

    _write_json(
        shots_path,
        {
            "items": [{"scene_number": 1, "shot_number": 1, "shot_type": "start"}],
            "seed": 555,
            "generation_completed": True,
            "completed_scenes": [1],
            "inputs_hash": "stale-hash",
        },
    )
    (base / "97_shots" / "shots_timeline.fcpxml").write_text("x", encoding="utf-8")
    recorder = []
    _install_fakes(monkeypatch, recorder=recorder)
    shots_generator.screenplay_shots_generator_tool(
        session_id="s1", project_id=pid, generate_end_shots=False, language="ru"
    )
    assert recorder != []  # регенерация из-за расхождения inputs_hash
    saved = _load(base)
    assert saved["inputs_hash"] != "stale-hash"


def test_partial_scope_discards_stale_on_disk_on_hash_mismatch(tmp_path, monkeypatch):
    """R3: под requested_partial блок resume/short-circuit пропускается, поэтому
    inputs_hash on-disk раньше НЕ сверялся. Если on-disk shots.json от ДРУГИХ входов
    (hash mismatch), его completed_scenes/items нельзя ни засчитывать завершёнными
    (ложный generation_completed), ни мёржить superset'ом (смешанный фильм)."""
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    base = _setup_project(root, pid, [(1, [1]), (2, [1]), (3, [1])])
    shots_path = base / "97_shots" / "shots.json"

    # На диске «полный» фильм, но от ДРУГИХ входов (inputs_hash заведомо не совпадёт).
    _write_json(
        shots_path,
        {
            "items": [
                {"scene_number": 1, "shot_number": 1, "shot_type": "start"},
                {"scene_number": 2, "shot_number": 1, "shot_type": "start"},
                {"scene_number": 3, "shot_number": 1, "shot_type": "start"},
            ],
            "seed": 555,
            "generation_completed": True,
            "completed_scenes": [1, 2, 3],
            "inputs_hash": "stale-hash",
        },
    )

    recorder = []
    _install_fakes(monkeypatch, recorder=recorder)
    saved_result = shots_generator.screenplay_shots_generator_tool(
        session_id="s1", project_id=pid, generate_end_shots=False, language="ru",
        scene_numbers=[2],
    )
    assert saved_result  # tool вернул payload, без short-circuit

    # Регенерируется только запрошенная сцена 2.
    assert recorder == [2]
    saved = _load(base)
    # Устаревшие сцены 1/3 НЕ подмёржены superset'ом — в файле только свежая сцена 2.
    assert _scene_set(saved) == {2}
    # Ложный маркер завершённости НЕ выставлен: фильм {1,2,3} не покрыт свежими входами.
    assert saved["generation_completed"] is False
    assert saved["completed_scenes"] == [2]
    assert saved["inputs_hash"] != "stale-hash"


def test_partial_regen_interrupt_does_not_falsely_complete(tmp_path, monkeypatch):
    """WS-D1: точечная перегенерация уже завершённой сцены, прерванная на полпути, НЕ должна
    пометить эту сцену завершённой в checkpoint. Иначе последующий обычный resume отфильтрует
    её как готовую и выставит ложный generation_completed при недоделанной сцене."""
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    base = _setup_project(root, pid, [(1, [1]), (2, [1, 2]), (3, [1])])

    # Полный прогон → фильм готов.
    _install_fakes(monkeypatch)
    shots_generator.screenplay_shots_generator_tool(
        session_id="s1", project_id=pid, generate_end_shots=False, language="ru"
    )
    full = _load(base)
    assert full["generation_completed"] is True
    assert set(full["completed_scenes"]) == {1, 2, 3}

    # Точечная перегенерация scene 2 (тот же hash → partial_write=True): shot 1 успешен
    # (пишет checkpoint), shot 2 падает.
    _install_fakes(monkeypatch)
    orig_prompt = shots_generator._generate_shot_prompt

    def failing_prompt(extended_context, shot_type, language="en", **kw):
        if (
            extended_context.get("scene_number") == 2
            and extended_context.get("shot_number") == 2
            and shot_type == "start"
        ):
            raise RuntimeError("boom shot 2")
        return orig_prompt(extended_context, shot_type, language=language, **kw)

    monkeypatch.setattr(shots_generator, "_generate_shot_prompt", failing_prompt)

    with pytest.raises(RuntimeError):
        shots_generator.screenplay_shots_generator_tool(
            session_id="s2", project_id=pid, generate_end_shots=False, language="ru",
            scene_numbers=[2],
        )

    interrupted = _load(base)
    # Недоделанная сцена 2 НЕ значится завершённой; фильм НЕ помечен готовым.
    assert 2 not in set(interrupted["completed_scenes"]), interrupted["completed_scenes"]
    assert interrupted["generation_completed"] is False

    # Обычный полный resume честно перегенерирует сцену 2 и завершит фильм.
    recorder = []
    _install_fakes(monkeypatch, recorder=recorder)
    shots_generator.screenplay_shots_generator_tool(
        session_id="s3", project_id=pid, generate_end_shots=False, language="ru"
    )
    assert 2 in recorder
    healed = _load(base)
    assert healed["generation_completed"] is True
    assert set(healed["completed_scenes"]) == {1, 2, 3}


def test_missing_start_prompt_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    base = _setup_project(root, pid, [(1, [1])])

    _install_fakes(monkeypatch, empty_start_scene=1)
    with pytest.raises(RuntimeError, match="START"):
        shots_generator.screenplay_shots_generator_tool(
            session_id="s1", project_id=pid, generate_end_shots=False, language="ru"
        )
    shots_path = base / "97_shots" / "shots.json"
    if shots_path.exists():
        saved = json.loads(shots_path.read_text(encoding="utf-8"))
        assert saved.get("generation_completed") is not True


def test_add_end_shot_boolean_no_attributeerror(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    base = _setup_project(root, pid, [(1, [1])])

    # add_end_shot приходит как python-bool True → str().strip().lower() без AttributeError
    _install_fakes(monkeypatch, generate_end_shots=True, add_end_bool=True)
    result = shots_generator.screenplay_shots_generator_tool(
        session_id="s1", project_id=pid, generate_end_shots=True, language="ru"
    )
    types_seen = {(it["scene_number"], it["shot_number"], it["shot_type"]) for it in result["items"]}
    assert (1, 1, "start") in types_seen
    assert (1, 1, "end") in types_seen


def test_seed_stable_on_resume(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    base = _setup_project(root, pid, [(1, [1]), (2, [1])])

    _install_fakes(monkeypatch)
    shots_generator.screenplay_shots_generator_tool(
        session_id="s1", project_id=pid, generate_end_shots=False, language="ru"
    )
    seed1 = _load(base)["seed"]

    _install_fakes(monkeypatch)
    shots_generator.screenplay_shots_generator_tool(
        session_id="s2", project_id=pid, generate_end_shots=False, language="ru", scene_numbers=[1]
    )
    seed2 = _load(base)["seed"]
    assert seed2 == seed1


def test_total_shots_count_reported(tmp_path, monkeypatch, caplog):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    pid = "proj"
    _setup_project(root, pid, [(1, [1]), (2, [1])])

    _install_fakes(monkeypatch)
    with caplog.at_level(logging.INFO, logger="custom_tools.storybook.screenplay_shots_generator"):
        result = shots_generator.screenplay_shots_generator_tool(
            session_id="s1", project_id=pid, generate_end_shots=False, language="ru"
        )
    assert len(result["items"]) == 2
    assert any("2 кадров" in rec.message for rec in caplog.records)
