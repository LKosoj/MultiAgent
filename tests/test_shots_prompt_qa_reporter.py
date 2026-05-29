import json
from pathlib import Path

import pytest

from custom_tools.storybook.shots_prompt_qa_reporter import generate_before_after_qa_reports


@pytest.mark.skip(reason="Requires LLM access for shots_prompt_qa_tool; enable manually when running in configured environment.")
def test_generate_before_after_qa_reports(tmp_path: Path):
    """
    Интеграционный тест-репортёр:
    - генерирует shots.json (можно ограничить scope),
    - пишет текстовый отчёт ДО QA (на основании сгенерированных промптов),
    - запускает QA (dry_run),
    - пишет текстовый отчёт ПОСЛЕ QA.

    По умолчанию пропущен, т.к. требует доступ к LLM-инфраструктуре.
    """
    project_id = "dolboyazher11"
    base = Path("plots/storybooks") / project_id
    shots_json = base / "97_shots" / "shots.json"
    screenplay_json = base / "91_screenplay" / "screenplay.json"
    assert shots_json.exists()
    assert screenplay_json.exists()

    out_before = tmp_path / "before_qa.txt"
    out_after = tmp_path / "after_qa.txt"

    # Small scope to keep it practical if user enables it
    generate_before_after_qa_reports(
        project_id=project_id,
        shots_json_path=str(shots_json),
        screenplay_json_path=str(screenplay_json),
        out_before_path=str(out_before),
        out_after_path=str(out_after),
        gen_session_id="test-shots-gen",
        gen_scene_numbers=[2, 3],
        gen_force=True,
        qa_model="ultimate",
        qa_temperature=0.1,
        qa_scene_numbers=[2, 3],
        qa_max_scenes=None,
        qa_force=True,
        qa_global_max_repairs=0,
    )

    assert out_before.exists() and out_before.stat().st_size > 0
    assert out_after.exists() and out_after.stat().st_size > 0


