"""Раздел 6.2/10.2 ТЗ: shots_prompt_qa обязан сливать shots.json по полям, а не
замещать элемент целиком, и не должен оставлять осиротевший .tmp при сбое записи.

Сценарий отказа (раздел 6.2, абзац про shots_prompt_qa): QA снимает `items` в
память при входе в шаг, долго проверяет их через LLM (в реальности — в пуле
потоков), а тем временем другой писатель (например `blockout_renderer`) честно
берёт блокировку, перечитывает диск и дописывает свои поля. QA заканчивает,
сам берёт блокировку, перечитывает диск (видит уже свежие данные) и раньше
накладывал ВЕСЬ элемент из памяти (`{**it, **in_memory_it}`) поверх дискового —
устаревший снимок побеждал целиком. Раздел 6.2 отдельно требует, чтобы длительность
(`duration_s`/`duration_source`/`duration_requested_s`) при этом никогда не
терялась и не откатывалась.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import custom_tools.storybook.shots_prompt_qa as sq


def _write_screenplay(root: Path, pid: str, scenes):
    base = root / pid / "91_screenplay"
    base.mkdir(parents=True, exist_ok=True)
    (base / "screenplay.json").write_text(
        json.dumps({"screenplay": scenes}, ensure_ascii=False), encoding="utf-8"
    )


def _scene(n, shots=(1,)):
    return {
        "scene_number": n,
        "action": "",
        "characters": [],
        "location_time": "",
        "storyboard": [
            {"shot_number": s, "camera_plan": "MEDIUM SHOT", "description": "", "timing": "0-1"}
            for s in shots
        ],
    }


def _item(scene, shot, shot_type="start", **extra):
    item = {
        "scene_number": scene,
        "shot_number": shot,
        "shot_type": shot_type,
        "video_prompt": "",
        "english_prompt": "x",
        "negative_prompt": "",
        "reference_image_paths": [],
        "_shot_frame_spec": {},
    }
    item.update(extra)
    return item


@pytest.fixture
def stub_llm(monkeypatch):
    calls = {"n": 0}

    def fake_api(**kwargs):
        calls["n"] += 1
        return '{"repairs": [], "notes": "ok"}'

    monkeypatch.setattr(sq, "call_openai_api", fake_api)
    monkeypatch.setattr(sq, "_extract_shot_frame_spec_llm", lambda **k: {})
    return calls


def test_qa_merge_preserves_concurrent_writer_fields_and_duration(tmp_path, monkeypatch, stub_llm):
    """До фикса: `{**it, **in_memory_it}` возвращает устаревший снимок целиком,
    потому что весь элемент из памяти побеждает поверх дискового. После фикса —
    накладываются только поля, которые реально пишет QA (english_prompt /
    video_prompt / negative_prompt / reference_image_paths / characters /
    locations), а всё остальное (включая длительность и поля болванки) остаётся
    тем, что лежит на диске."""
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    _write_screenplay(tmp_path, "proj", [_scene(1)])

    shots_dir = tmp_path / "proj" / "97_shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    shots_path = shots_dir / "shots.json"

    # Снимок, с которым стартует QA (пришёл параметром shots_data — как из
    # выхода предыдущего шага пайплайна).
    snapshot_item = _item(
        1, 1, "start",
        duration_s=5, duration_source="manual", duration_requested_s=5,
        blockout_rendered_at="2026-08-16T10:00:00Z",
        blockout_video="old.mp4",
    )
    shots_data = {"items": [dict(snapshot_item)], "consistency_rules": []}

    # На диске вначале лежит тот же снимок.
    shots_path.write_text(json.dumps(shots_data, ensure_ascii=False), encoding="utf-8")

    # Пока QA "думает" (гоняет LLM в пуле потоков), другой писатель честно берёт
    # блокировку, перечитывает и дописывает свежие данные: новый рендер
    # болванки и изменённую длительность (например, правка со вкладки
    # "Болванка").
    on_disk = json.loads(shots_path.read_text(encoding="utf-8"))
    on_disk["items"][0]["blockout_rendered_at"] = "2026-08-16T11:00:00Z"
    on_disk["items"][0]["blockout_video"] = "new.mp4"
    on_disk["items"][0]["duration_s"] = 7
    shots_path.write_text(json.dumps(on_disk, ensure_ascii=False), encoding="utf-8")

    sq.shots_prompt_qa_tool(
        session_id="s", project_id="proj", shots_data=shots_data,
        enable=True, force=True, model="hard", global_max_repairs=0, dry_run=False,
    )

    saved = json.loads(shots_path.read_text(encoding="utf-8"))
    saved_item = saved["items"][0]
    # Свежая запись конкурентного писателя не откатывается.
    assert saved_item["blockout_rendered_at"] == "2026-08-16T11:00:00Z"
    assert saved_item["blockout_video"] == "new.mp4"
    # Длительность не теряется и не откатывается к устаревшему снимку.
    assert saved_item["duration_s"] == 7
    assert saved_item["duration_source"] == "manual"
    assert saved_item["duration_requested_s"] == 5


def test_write_json_atomic_removes_tmp_on_failure(tmp_path):
    """A38: временный файл вида *.tmp не должен оставаться на диске, если
    запись оборвалась исключением между созданием tmp-файла и os.replace."""
    path = tmp_path / "sub" / "out.json"
    with pytest.raises(TypeError):
        sq._write_json_atomic(str(path), {"bad": {1, 2, 3}})  # set -> не сериализуется в JSON
    assert list(tmp_path.rglob("*.tmp")) == []
    assert not path.exists()


def test_qa_persist_failure_marks_shots_data_instead_of_silent_success(tmp_path, monkeypatch, stub_llm):
    """До фикса `except Exception as e: logger.error(...)` в конце
    shots_prompt_qa_tool глушил ошибку записи shots.json/report целиком:
    вызывающий получал shots_data как при обычном успехе и не мог отличить
    "правки сохранены на диск" от "правки только в памяти". Инструмент
    по-прежнему не поднимает исключение и не выставляет status="error"
    (money-path, шаг не должен падать там, где раньше продолжал — и это не
    шаг с output_schema/required-полями) — вместо этого returned shots_data
    несёт маркер `_qa_persist_error`, тем же приёмом, что уже используется
    для `_qa_report` в dry_run-ветке."""
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    _write_screenplay(tmp_path, "proj", [_scene(1)])

    shots_dir = tmp_path / "proj" / "97_shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    shots_path = shots_dir / "shots.json"

    shots_data = {"items": [_item(1, 1, "start")], "consistency_rules": []}
    shots_path.write_text(json.dumps(shots_data, ensure_ascii=False), encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(sq, "_write_json_atomic", boom)

    result = sq.shots_prompt_qa_tool(
        session_id="s", project_id="proj", shots_data=json.loads(json.dumps(shots_data)),
        enable=True, force=True, model="hard", global_max_repairs=0, dry_run=False,
    )

    assert "disk full" in result.get("_qa_persist_error", "")
    # Атомарная запись ничего не тронула — на диске всё ещё исходная версия.
    assert json.loads(shots_path.read_text(encoding="utf-8")) == shots_data


def test_qa_report_write_failure_does_not_mark_shots_data(tmp_path, monkeypatch, stub_llm, caplog):
    """Частичный сбой: запись shots.json проходит успешно, а запись
    вспомогательного отчёта (shots_prompt_qa_report.json) падает. Раз
    shots.json на диске уже актуален, маркер `_qa_persist_error` ставить
    нельзя — но сбой записи отчёта обязан попасть в журнал."""
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    _write_screenplay(tmp_path, "proj", [_scene(1)])

    shots_dir = tmp_path / "proj" / "97_shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    shots_path = shots_dir / "shots.json"

    shots_data = {"items": [_item(1, 1, "start")], "consistency_rules": []}
    shots_path.write_text(json.dumps(shots_data, ensure_ascii=False), encoding="utf-8")

    real_write = sq._write_json_atomic

    def flaky_write(path, data):
        if str(path).endswith("shots_prompt_qa_report.json"):
            raise OSError("disk full (report)")
        return real_write(path, data)

    monkeypatch.setattr(sq, "_write_json_atomic", flaky_write)

    with caplog.at_level("ERROR"):
        result = sq.shots_prompt_qa_tool(
            session_id="s", project_id="proj", shots_data=json.loads(json.dumps(shots_data)),
            enable=True, force=True, model="hard", global_max_repairs=0, dry_run=False,
        )

    assert "_qa_persist_error" not in result
    # shots.json на диске содержит правки (prompts_validated_at появляется
    # только после того, как этот прогон QA реально записал файл).
    saved = json.loads(shots_path.read_text(encoding="utf-8"))
    assert saved.get("prompts_validated_at") == result.get("prompts_validated_at")
    assert saved.get("prompts_validated_at")
    # Сбой записи отчёта не проглочен молча.
    assert "disk full (report)" in caplog.text


def test_qa_successful_persist_has_no_error_marker(tmp_path, monkeypatch, stub_llm):
    """Контроль: при успешной записи поведение не меняется ни на йоту —
    новый маркер `_qa_persist_error` не появляется в возвращаемом shots_data."""
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    _write_screenplay(tmp_path, "proj", [_scene(1)])

    shots_dir = tmp_path / "proj" / "97_shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    shots_path = shots_dir / "shots.json"

    shots_data = {"items": [_item(1, 1, "start")], "consistency_rules": []}
    shots_path.write_text(json.dumps(shots_data, ensure_ascii=False), encoding="utf-8")

    result = sq.shots_prompt_qa_tool(
        session_id="s", project_id="proj", shots_data=json.loads(json.dumps(shots_data)),
        enable=True, force=True, model="hard", global_max_repairs=0, dry_run=False,
    )

    assert "_qa_persist_error" not in result
