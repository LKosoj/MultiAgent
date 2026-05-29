"""W9-A3: тесты pipeline-registry валидации.

Проверяют, что:
1. Для каждого pipeline в ``PIPELINE_VALIDATORS`` существует Pydantic-модель.
2. Каждая модель ОТКАЗЫВАЕТ на bad payload (отсутствуют required-поля /
   неверные типы) -- fail-fast.
3. Каждая модель ПРОПУСКАЕТ корректный payload (smoke).
4. Существующий контракт ``text_to_sql_pipeline`` не сломан.

Тесты НЕ проверяют интеграцию через ``workflows.start`` (это сделано в
test_text_to_sql_agui_workflow_contract). Здесь — unit-уровень моделей.
"""
from __future__ import annotations

import os
from pathlib import Path

# Чтобы импорт PIPELINE_VALIDATORS не падал на validators (sqlglot etc.),
# выставляем USE_SQLGLOT=1 как другие safety-тесты делают.
os.environ.setdefault("USE_SQLGLOT", "1")

import pytest  # noqa: E402
import yaml  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from backend.fastapi_app.agui._t2s_requests import PIPELINE_VALIDATORS  # noqa: E402


def _yaml_inputs(workflow_name: str) -> dict:
    path = Path(__file__).resolve().parents[1] / "workflow_pipelines" / f"{workflow_name}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["inputs"]


# === smoke: реестр покрывает все workflow_pipelines/*.yaml ====================
def test_registry_covers_all_yaml_pipelines() -> None:
    """W9-A3: каждый yaml в workflow_pipelines/ должен быть в реестре."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    pipeline_dir = repo_root / "workflow_pipelines"
    yaml_names = {p.stem for p in pipeline_dir.glob("*.yaml")}
    registry_names = set(PIPELINE_VALIDATORS.keys())
    missing = yaml_names - registry_names
    assert not missing, (
        f"Pipelines на диске без валидатора в реестре: {sorted(missing)}. "
        "Добавьте Pydantic-модель в _pipeline_requests.py и зарегистрируйте."
    )


# === bad-payload отказы для каждого validator =================================
# Параметризованный набор: (workflow_name, bad_payload, error_substring).
# Каждый кейс — минимальный, проверяющий отказ Pydantic при отсутствии
# required-поля или неверном типе.
_BAD_PAYLOADS: list[tuple[str, dict, str]] = [
    ("architecture_review", {}, "project_path"),
    ("architecture_review", {"project_path": ""}, "project_path"),
    ("architecture_review", {"project_path": "   "}, "project_path"),
    ("content_creation", {}, "topic"),
    ("content_creation", {"topic": None}, "topic"),
    ("data_analysis", {}, "analysis_request"),
    ("data_analysis", {"analysis_request": ""}, "analysis_request"),
    ("manager_team_demo", {}, "topic"),
    # ruble_analysis_tool: end_date optional, но non-string -> отказ.
    ("ruble_analysis_tool", {"end_date": 123}, "end_date"),
    ("simple_research", {}, "topic"),
    # step_results_demo: session_id не-строка -> отказ; topic optional.
    ("step_results_demo", {"session_id": 42}, "session_id"),
    ("storybook_pipeline", {}, "task"),
    ("storybook_pipeline", {"task": ""}, "task"),
    ("storybook_pipeline", {"task": "ok", "pages_min": "not_a_number"}, "pages"),
    ("tool_demo", {}, "image_prompt"),
    ("tool_demo", {"image_prompt": "p"}, "research_topic"),
    # text_to_sql_pipeline: query/dsn обязательны (контракт не сломан).
    ("text_to_sql_pipeline", {}, "query"),
    ("text_to_sql_pipeline", {"query": "select 1"}, "dsn"),
]


@pytest.mark.parametrize(
    "workflow_name,payload,error_substring", _BAD_PAYLOADS
)
def test_pipeline_validator_rejects_bad_payload(
    workflow_name: str, payload: dict, error_substring: str
) -> None:
    """Bad payload -> ValidationError; сообщение содержит проблемное поле."""
    model = PIPELINE_VALIDATORS.get(workflow_name)
    assert model is not None, f"no validator for {workflow_name}"
    with pytest.raises(ValidationError) as exc_info:
        model.model_validate(payload)
    err_text = str(exc_info.value).lower()
    assert error_substring.lower() in err_text, (
        f"expected '{error_substring}' in error for {workflow_name}, "
        f"got: {exc_info.value}"
    )


# === smoke: валидный payload пропускается =====================================
_GOOD_PAYLOADS: list[tuple[str, dict]] = [
    ("architecture_review", {"project_path": "/srv/proj"}),
    ("content_creation", {"topic": "Биология"}),
    ("data_analysis", {"analysis_request": "Сравнить регионы"}),
    ("manager_team_demo", {"topic": "AI ethics"}),
    ("ruble_analysis_tool", {}),  # end_date optional
    ("ruble_analysis_tool", {"end_date": "01/01/2025"}),
    ("simple_research", {"topic": "квантовые компьютеры"}),
    ("step_results_demo", {}),
    ("step_results_demo", {"topic": "AI", "session_id": "s1"}),
    (
        "storybook_pipeline",
        {"task": "Сказка про кота", "pages_min": 1, "pages_max": 3},
    ),
    (
        "tool_demo",
        {"image_prompt": "cat", "research_topic": "felines"},
    ),
]


@pytest.mark.parametrize("workflow_name,payload", _GOOD_PAYLOADS)
def test_pipeline_validator_accepts_good_payload(
    workflow_name: str, payload: dict
) -> None:
    model = PIPELINE_VALIDATORS.get(workflow_name)
    assert model is not None
    instance = model.model_validate(payload)
    dumped = instance.model_dump()
    # Минимальная sanity: dumped — dict, содержит ключи payload.
    assert isinstance(dumped, dict)
    for key, value in payload.items():
        # ruble_analysis_tool сэмпл с end_date="01/01/2025" — strip не меняет.
        if isinstance(value, str):
            assert dumped[key].strip() == value.strip()


# === backward-compat: text_to_sql_pipeline не сломан =========================
def test_text_to_sql_validator_unchanged_contract() -> None:
    """W1-T2-регрессия: text_to_sql_pipeline должен оставаться TextToSqlGenerateRequest."""
    from backend.fastapi_app.agui._t2s_requests import TextToSqlGenerateRequest

    assert PIPELINE_VALIDATORS["text_to_sql_pipeline"] is TextToSqlGenerateRequest


def test_text_to_sql_valid_payload_dumps_required_fields() -> None:
    """W1-T2-регрессия: успешный payload содержит обязательные поля."""
    model = PIPELINE_VALIDATORS["text_to_sql_pipeline"]
    payload = {
        "query": "Сколько территорий?",
        "dsn": "postgresql://u:p@h:5432/db",
        "max_rows": 50,
    }
    inst = model.model_validate(payload)
    dumped = inst.model_dump()
    assert dumped["query"] == "Сколько территорий?"
    assert dumped["dsn"].startswith("postgresql://")
    assert dumped["max_rows"] == 50


def test_storybook_validator_requires_task_but_uses_yaml_defaults_for_other_inputs() -> None:
    model = PIPELINE_VALIDATORS["storybook_pipeline"]
    expected_inputs = _yaml_inputs("storybook_pipeline")

    dumped = model.model_validate({"task": "Сказка про кота"}).model_dump()

    for key, value in expected_inputs.items():
        if key == "task":
            assert dumped[key] == "Сказка про кота"
        else:
            assert dumped[key] == value


def test_storybook_validator_reads_yaml_defaults_after_file_change(tmp_path, monkeypatch) -> None:
    import backend.fastapi_app.agui._pipeline_requests as pipeline_requests

    root = tmp_path
    fake_module = root / "backend" / "fastapi_app" / "agui" / "_pipeline_requests.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("", encoding="utf-8")
    pipelines_dir = root / "workflow_pipelines"
    pipelines_dir.mkdir()
    pipeline_path = pipelines_dir / "storybook_pipeline.yaml"

    def write_storybook_yaml(pages_min: int) -> None:
        pipeline_path.write_text(
            "\n".join([
                "name: storybook_pipeline",
                "inputs:",
                "  task: demo",
                "  project_id: storybook_project",
                f"  pages_min: {pages_min}",
                "  pages_max: 2",
                "  words_per_page_min: 200",
                "  words_per_page_max: 300",
                "  generate_screenplay: true",
                "  generate_end_shots: true",
                "  language: ru",
                "  screenplay_time: 120",
                "  force_update_prompts: false",
                "  skip_prompt_enhancement: true",
            ]),
            encoding="utf-8",
        )

    monkeypatch.setattr(pipeline_requests, "__file__", str(fake_module))
    model = PIPELINE_VALIDATORS["storybook_pipeline"]

    write_storybook_yaml(1)
    assert model.model_validate({"task": "Сказка"}).pages_min == 1

    write_storybook_yaml(3)
    assert model.model_validate({"task": "Сказка"}).pages_min == 3
