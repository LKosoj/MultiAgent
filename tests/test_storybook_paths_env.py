"""WS-D / M-23: единый резолвер путей (safe_storybook_project_dir), как в
audio_subtitle/montage. При заданном STORYBOOK_PROJECTS_DIR проект берётся из него;
при незаданном — поведение ИДЕНТИЧНО прежнему CWD-relative plots/storybooks/.

Тесты in-process; пишем только в tmp_path (env-root или CWD под tmp_path).
"""

import json
import sys
import types
from pathlib import Path


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

from custom_tools.storybook import project_paths
from custom_tools.storybook import screenplay_shots_generator as shots_generator
from custom_tools.storybook.screenplay_shots_generator_utils import shared_utils

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


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _setup_project(base: Path):
    _write_json(
        base / "91_screenplay" / "screenplay.json",
        {
            "screenplay": [
                {
                    "scene_number": 1,
                    "action": "a",
                    "characters": ["Герой"],
                    "storyboard": [
                        {"shot_number": 1, "description": "d", "camera_plan": "Close-up", "timing": "1s"}
                    ],
                }
            ]
        },
    )
    _write_json(base / "20_bible" / "characters.json", [{"name": "Герой"}])
    _write_json(base / "20_bible" / "locations.json", [])


def _install_fakes(monkeypatch):
    def fake_build_ctx(**kwargs):
        scene = kwargs.get("scene") or {}
        return {
            "scene_number": scene.get("scene_number"),
            "shot_number": kwargs.get("shot_number"),
            "shot_frame_spec": {"primary_subject": "Герой", "must_show": ["Герой"]},
            "full_shot_frame_spec": {"primary_subject": "Герой", "must_show": ["Герой"]},
            "shot_frame_spec_cache_key": "s1_1",
            "scene_continuity_facts": {},
            "location_time": "",
            "location_canon_name": "",
        }

    def fake_prompt(extended_context, shot_type, video_prompt="", start_llm_result=None, language="en"):
        return {
            "english_prompt": "prompt start",
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
        }

    monkeypatch.setattr(shots_generator, "ThreadPoolExecutor", _FakeExecutor)
    monkeypatch.setattr(shots_generator, "as_completed", lambda futures: list(futures))
    monkeypatch.setattr(shots_generator, "_build_extended_context", fake_build_ctx)
    monkeypatch.setattr(shots_generator, "_generate_shot_prompt", fake_prompt)
    monkeypatch.setattr(shots_generator, "_generate_fcpxml", lambda *a, **k: None)
    monkeypatch.setattr(shots_generator, "_generate_photo_fcpxml", lambda *a, **k: None)


# --------------------------------------------------------------------------------
def test_safe_dir_uses_env_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    resolved = project_paths.safe_storybook_project_dir("proj")
    assert resolved == (root / "proj").resolve()


def test_safe_dir_cwd_fallback_matches_legacy_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("STORYBOOK_PROJECTS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    resolved = project_paths.safe_storybook_project_dir("proj")
    # Идентично прежнему CWD-relative пути plots/storybooks/<id>
    assert resolved == (Path("plots") / "storybooks" / "proj").resolve()


def test_tool_writes_under_env_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    # CWD отличается от env-root, чтобы поймать утечку в CWD
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    base = root / "proj"
    _setup_project(base)
    _install_fakes(monkeypatch)

    shots_generator.screenplay_shots_generator_tool(
        session_id="s", project_id="proj", generate_end_shots=False, language="ru"
    )

    assert (base / "97_shots" / "shots.json").exists()
    # Ничего не утекло в CWD-relative plots/storybooks
    assert not (workdir / "plots" / "storybooks" / "proj").exists()


def test_tool_writes_cwd_relative_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("STORYBOOK_PROJECTS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    base = tmp_path / "plots" / "storybooks" / "proj"
    _setup_project(base)
    _install_fakes(monkeypatch)

    shots_generator.screenplay_shots_generator_tool(
        session_id="s", project_id="proj", generate_end_shots=False, language="ru"
    )

    assert (base / "97_shots" / "shots.json").exists()
