"""Э11: секции artist_batch_shots (P12, P18) и video_generator (P09) в
93_blockout/report.json (ТЗ docs/tz-blockout-reference-pipeline.md, раздел
20.3), включая критерий приёмки A38 -- конкурентная дозапись двумя писателями
(blockout_preview/blockout_renderer и artist_batch_shots/video_generator)
не должна терять чужие секции.
"""
from __future__ import annotations

import json
import multiprocessing
import threading
from pathlib import Path

import pytest

from custom_tools.storybook import artist_batch_edit as abe
from custom_tools.storybook import blockout_preview as bp
from custom_tools.storybook import video_generator_aitunnel_tool as vgt
from unittest.mock import patch

# Переиспользуем готовые фикстуры Э8-тестов video_generator_aitunnel_tool, чтобы
# не задваивать сборку project/shots.json/AITUNNEL-моков (тот же приём, что уже
# используется в репозитории для межфайловых импортов тестовых хелперов).
from tests.test_video_generator_aitunnel_tool import (  # noqa: E402
    _FakeResponse,
    _completed_job_payload,
    _make_project_item,
    _patch_project_mode,
    _write_blockout_reference,
    _write_project,
)


def _touch(path) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b"\x89PNG\r\n")
    return str(path)


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# =============================================================================
# artist_batch_shots -- P12 (откат копирования связанного кадра)
# =============================================================================


def test_p12_written_when_link_copy_rollback_triggers(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id = "p12proj"
    report_path = tmp_path / project_id / "93_blockout" / "report.json"
    _write_json(report_path, {})

    # source_end_path отсутствует на диске -> _handle_linked_shot() откатывается
    # (artist_batch_edit.py), а output_path уже существует -> _worker() возвращает
    # запись раньше edit_image_vse_tool, мокать генерацию не нужно.
    existing_output = _touch(tmp_path / "already_there.png")
    item = {
        "copy_from_previous_end": True,
        "source_end_path": str(tmp_path / "missing_source_end.png"),
        "output_path": existing_output,
        "project_id": project_id,
        "scene_number": 3,
        "shot_number": 4,
        "shot_type": "end",
    }

    raw = abe.artist_agent_batch_edit_tool(
        session_id="sess-p12",
        items={"items": [item], "consistency_rules": []},
        max_concurrency=1,
        enable=True,
        language="en",
        generate_blockout=True,
    )
    results = json.loads(raw)
    assert results[0]["link_copy_failed"] is True

    report = _read_json(report_path)
    checks = report["artist_batch_shots"]["checks"]
    p12_checks = [c for c in checks if c["code"] == "P12"]
    assert len(p12_checks) == 1
    assert p12_checks[0]["scene_number"] == 3
    assert p12_checks[0]["shot_number"] == 4
    assert p12_checks[0]["details"]["source_end_path"] == item["source_end_path"]

    assert list(report_path.parent.glob("*.tmp")) == []
    assert (report_path.parent / "report.json.lock").is_file()


def test_p12_not_written_when_link_copy_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id = "p12ok"
    report_path = tmp_path / project_id / "93_blockout" / "report.json"
    _write_json(report_path, {})

    source_end = _touch(tmp_path / "prev_end.png")
    item = {
        "copy_from_previous_end": True,
        "source_end_path": source_end,
        "output_path": str(tmp_path / "out.png"),
        "project_id": project_id,
        "scene_number": 1,
        "shot_number": 1,
        "shot_type": "end",
    }

    raw = abe.artist_agent_batch_edit_tool(
        session_id="sess-p12-ok",
        items={"items": [item], "consistency_rules": []},
        max_concurrency=1,
        enable=True,
        language="en",
        generate_blockout=True,
    )
    results = json.loads(raw)
    assert results[0]["link_copy_failed"] is False

    report = _read_json(report_path)
    checks = report.get("artist_batch_shots", {}).get("checks", [])
    assert [c for c in checks if c["code"] == "P12"] == []


# =============================================================================
# artist_batch_shots -- P18 (состав/порядок референсов)
# =============================================================================


def test_check_p18_reference_order_none_when_no_blockout_expected():
    item = {"scene_number": 1, "shot_number": 1}
    assert abe._check_p18_reference_order(item, ["a.png"]) is None


def test_check_p18_reference_order_none_when_position_matches():
    item = {
        "scene_number": 1,
        "shot_number": 1,
        "reference_image_paths": ["continuity.png", "blockout.png", "other.png"],
        "_blockout_ref_position": 2,
    }
    assert abe._check_p18_reference_order(item, ["continuity.png", "blockout.png", "other.png"]) is None


def test_check_p18_reference_order_flags_position_dropped_by_truncation():
    item = {
        "scene_number": 2,
        "shot_number": 5,
        "shot_type": "start",
        "reference_image_paths": ["continuity.png", "blockout.png", "other.png"],
        "_blockout_ref_position": 2,
    }
    # Итоговый список запроса не содержит болванку на позиции 2 (обрезан/переупорядочен).
    finding = abe._check_p18_reference_order(item, ["continuity.png", "other.png"])
    assert finding["code"] == "P18"
    assert finding["scene_number"] == 2
    assert finding["shot_number"] == 5
    assert finding["details"]["reason"] == "reference_dropped_or_reordered"
    assert finding["details"]["actual_reference_paths"] == ["continuity.png", "other.png"]


def test_check_p18_reference_order_flags_tagged_reason_directly():
    item = {
        "scene_number": 7,
        "shot_number": 1,
        "shot_type": "start",
        "_p18_reason": "single_reference_exception",
    }
    finding = abe._check_p18_reference_order(item, ["only.png"])
    assert finding["details"]["reason"] == "single_reference_exception"


def test_p18_single_reference_exception_recorded_end_to_end(tmp_path, monkeypatch):
    """ТЗ раздел 11.2: единственный референс в списке -- болванка осознанно не
    подаётся, но это фиксируется предупреждением P18 в отчёте."""
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id = "p18single"
    report_path = tmp_path / project_id / "93_blockout" / "report.json"
    _write_json(report_path, {})

    only_ref = _touch(tmp_path / "only.png")
    blockout_ref = _touch(tmp_path / "blockout.png")
    output_path = str(tmp_path / "out.png")

    item = {
        "project_id": project_id,
        "scene_number": 1,
        "shot_number": 1,
        "shot_type": "start",
        "english_prompt": "A knight stands in a courtyard.",
        "reference_image_paths": [only_ref],
        "output_path": output_path,
    }

    def _fake_edit_image_vse_tool(**kwargs):
        out = kwargs.get("output_path")
        if out:
            Path(out).write_bytes(b"\x89PNG\r\n")
        return "ok"

    with patch(
        "custom_tools.storybook.artist_batch_edit._load_blockout_ref_image_cache",
        return_value={(project_id, 1, 1, "start"): blockout_ref},
    ), patch(
        "custom_tools.storybook.artist_batch_edit.edit_image_vse_tool",
        side_effect=_fake_edit_image_vse_tool,
    ):
        abe.artist_agent_batch_edit_tool(
            session_id="sess-p18-single",
            items={"items": [item], "consistency_rules": []},
            max_concurrency=1,
            enable=True,
            language="en",
            use_blockout_reference=True,
            generate_blockout=True,
        )

    report = _read_json(report_path)
    checks = report["artist_batch_shots"]["checks"]
    p18_checks = [c for c in checks if c["code"] == "P18"]
    assert len(p18_checks) == 1
    assert p18_checks[0]["details"]["reason"] == "single_reference_exception"


def test_artist_batch_no_section_when_generate_blockout_false(tmp_path, monkeypatch):
    """Раздел 20.3: при выключенном слое болванок artist_batch_shots свою
    секцию не формирует вовсе, даже если report.json уже существует и P12
    сработал бы при включённом слое."""
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id = "p12disabled"
    report_path = tmp_path / project_id / "93_blockout" / "report.json"
    _write_json(report_path, {})

    existing_output = _touch(tmp_path / "already_there.png")
    item = {
        "copy_from_previous_end": True,
        "source_end_path": str(tmp_path / "missing_source_end.png"),
        "output_path": existing_output,
        "project_id": project_id,
        "scene_number": 1,
        "shot_number": 1,
        "shot_type": "end",
    }

    abe.artist_agent_batch_edit_tool(
        session_id="sess-p12-disabled",
        items={"items": [item], "consistency_rules": []},
        max_concurrency=1,
        enable=True,
        language="en",
        generate_blockout=False,
    )

    report = _read_json(report_path)
    assert "artist_batch_shots" not in report


# =============================================================================
# artist_batch_shots writer -- отсутствие report.json / project_id (раздел 20.3)
# =============================================================================


def test_write_artist_batch_report_section_noop_when_report_file_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id = "noreport1"
    report_dir = tmp_path / project_id / "93_blockout"
    report_dir.mkdir(parents=True)
    # report.json умышленно отсутствует -- blockout_scene_builder не запускался
    # (generate_blockout: false в разделе 10.4).

    abe._write_artist_batch_report_section(
        project_id,
        items_data=[{"scene_number": 1, "shot_number": 1, "shot_type": "start"}],
        p12_findings=[{"code": "P12", "scene_number": 1, "shot_number": 1, "shot_type": "start"}],
        p18_findings=[],
    )

    assert not (report_dir / "report.json").exists()


def test_write_artist_batch_report_section_noop_when_no_project_id():
    # Не должно падать и не должно требовать окружения STORYBOOK_PROJECTS_DIR.
    abe._write_artist_batch_report_section(None, items_data=[], p12_findings=[], p18_findings=[])


# =============================================================================
# video_generator -- P09 (видео-референс болванки не подан)
# =============================================================================


@pytest.mark.parametrize(
    "reason",
    [
        "condition_2_video_missing",
        "condition_3_manifest_missing",
        "condition_3_duration_mismatch",
        "condition_4_aspect_mismatch",
        "condition_5_junction_failed",
    ],
)
def test_write_video_generator_report_section_records_rejection_reason(tmp_path, monkeypatch, reason):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id = "p09proj"
    report_path = tmp_path / project_id / "93_blockout" / "report.json"
    _write_json(report_path, {})

    results = [
        {
            "success": True,
            "scene_number": 1,
            "shot_number": 2,
            "video_reference_rejected_reason": reason,
        }
    ]
    vgt._write_video_generator_report_section(project_id, results, generate_blockout=True)

    report = _read_json(report_path)
    checks = report["video_generator"]["checks"]
    assert len(checks) == 1
    assert checks[0]["code"] == "P09"
    assert checks[0]["scene_number"] == 1
    assert checks[0]["shot_number"] == 2
    assert checks[0]["details"]["reason"] == reason

    assert list(report_path.parent.glob("*.tmp")) == []
    assert (report_path.parent / "report.json.lock").is_file()


def test_write_video_generator_report_section_skips_condition_1_and_success(tmp_path, monkeypatch):
    """Условие 1 §11.3 (слой/референс намеренно выключены) не является
    наблюдением P09 (раздел 20.2: фиксируются только условия 2-5); успешно
    поданный референс (reason=None) тоже не порождает находку."""
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id = "p09skip"
    report_path = tmp_path / project_id / "93_blockout" / "report.json"
    _write_json(report_path, {})

    results = [
        {"scene_number": 1, "shot_number": 1, "video_reference_rejected_reason": "condition_1_reference_disabled"},
        {"scene_number": 1, "shot_number": 2, "video_reference_rejected_reason": None},
    ]
    vgt._write_video_generator_report_section(project_id, results, generate_blockout=True)

    report = _read_json(report_path)
    assert report["video_generator"]["checks"] == []


def test_p09_written_end_to_end_when_video_reference_missing(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    item["blockout_video"] = str(tmp_path / "does_not_exist.mp4")  # условие 2
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    report_path = tmp_path / "plots" / "storybooks" / project_id / "93_blockout" / "report.json"
    _write_json(report_path, {})

    def fake_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        return _FakeResponse(status_code=202, json_payload={"id": "job-1", "status": "pending"})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            return _FakeResponse(status_code=200, json_payload=_completed_job_payload("job-1", "https://cdn.example/job-1.mp4"))
        if url == "https://cdn.example/job-1.mp4":
            return _FakeResponse(status_code=200, content=b"video")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(vgt.requests, "post", fake_post)
    monkeypatch.setattr(vgt.requests, "get", fake_get)

    result = vgt.video_generator_aitunnel_tool(
        session_id="session-p09",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
        generate_blockout=True,
        use_blockout_reference=True,
    )
    assert result["status"] == "success"

    report = _read_json(report_path)
    checks = report["video_generator"]["checks"]
    assert len(checks) == 1
    assert checks[0]["code"] == "P09"
    assert checks[0]["details"]["reason"] == "condition_2_video_missing"


def test_write_video_generator_report_section_noop_when_report_file_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id = "noreport2"
    report_dir = tmp_path / project_id / "93_blockout"
    report_dir.mkdir(parents=True)

    vgt._write_video_generator_report_section(
        project_id,
        results=[{"scene_number": 1, "shot_number": 1, "video_reference_rejected_reason": "condition_2_video_missing"}],
        generate_blockout=True,
    )

    assert not (report_dir / "report.json").exists()


def test_write_video_generator_report_section_noop_when_generate_blockout_false(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id = "p09disabled"
    report_path = tmp_path / project_id / "93_blockout" / "report.json"
    _write_json(report_path, {})

    vgt._write_video_generator_report_section(
        project_id,
        results=[{"scene_number": 1, "shot_number": 1, "video_reference_rejected_reason": "condition_2_video_missing"}],
        generate_blockout=False,
    )

    report = _read_json(report_path)
    assert "video_generator" not in report


# =============================================================================
# A38 (раздел 22): параллельная дозапись двумя писателями не теряет секции
# =============================================================================


def test_a38_concurrent_threads_do_not_lose_either_section(tmp_path, monkeypatch):
    """blockout_preview и artist_batch_shots зависят от одного и того же
    blockout_renderer и становятся готовы одновременно (раздел 20.3) --
    планировщик запускает их параллельно. Здесь -- реальная гонка двух
    потоков (barrier форсирует одновременный старт), а не последовательные
    вызовы."""
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id = "a38threads"
    report_path = tmp_path / project_id / "93_blockout" / "report.json"
    _write_json(report_path, {})

    p12_findings = [{
        "code": "P12", "level": "warning", "scene_number": 1, "shot_number": 1, "shot_type": "start",
        "message": "m", "details": {},
    }]
    preview_summary = {"segments_total": 3, "total_duration_s": 15.0}

    barrier = threading.Barrier(2)
    errors = []

    def _run_artist():
        barrier.wait(timeout=5)
        try:
            abe._write_artist_batch_report_section(
                project_id,
                items_data=[{"scene_number": 1, "shot_number": 1, "shot_type": "start"}],
                p12_findings=p12_findings,
                p18_findings=[],
            )
        except Exception as exc:  # pragma: no cover -- surfaced via errors list below
            errors.append(exc)

    def _run_preview():
        barrier.wait(timeout=5)
        try:
            bp._report_write_summary(report_path, preview_summary)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=_run_artist)
    t2 = threading.Thread(target=_run_preview)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors
    report = _read_json(report_path)
    assert report["artist_batch_shots"]["checks"][0]["code"] == "P12"
    assert report["blockout_preview"] == preview_summary

    assert list(report_path.parent.glob("*.tmp")) == []
    assert (report_path.parent / "report.json.lock").is_file()


def _mp_write_artist_batch(project_id, projects_dir, p12_findings, barrier):
    import os
    os.environ["STORYBOOK_PROJECTS_DIR"] = projects_dir
    from custom_tools.storybook import artist_batch_edit as _abe
    barrier.wait(timeout=10)
    _abe._write_artist_batch_report_section(
        project_id,
        items_data=[{"scene_number": 9, "shot_number": 1, "shot_type": "start"}],
        p12_findings=p12_findings,
        p18_findings=[],
    )


def _mp_write_video_generator(project_id, projects_dir, results, barrier):
    import os
    os.environ["STORYBOOK_PROJECTS_DIR"] = projects_dir
    from custom_tools.storybook import video_generator_aitunnel_tool as _vgt
    barrier.wait(timeout=10)
    _vgt._write_video_generator_report_section(project_id, results, generate_blockout=True)


def test_a38_concurrent_processes_do_not_lose_either_section(tmp_path):
    """Тот же критерий A38, но двумя настоящими ОС-процессами (разные pid) --
    проверяет и sidecar-flock через границу процессов, и то, что уникальный
    временный файл '{path}.{pid}.tmp' не сталкивается между писателями."""
    project_id = "a38procs"
    report_path = tmp_path / project_id / "93_blockout" / "report.json"
    _write_json(report_path, {})

    p12_findings = [{
        "code": "P12", "level": "warning", "scene_number": 9, "shot_number": 1, "shot_type": "start",
        "message": "m", "details": {},
    }]
    video_results = [{
        "scene_number": 2, "shot_number": 7,
        "video_reference_rejected_reason": "condition_2_video_missing",
    }]

    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    p1 = ctx.Process(target=_mp_write_artist_batch, args=(project_id, str(tmp_path), p12_findings, barrier))
    p2 = ctx.Process(target=_mp_write_video_generator, args=(project_id, str(tmp_path), video_results, barrier))
    p1.start()
    p2.start()
    p1.join(timeout=30)
    p2.join(timeout=30)

    assert p1.exitcode == 0
    assert p2.exitcode == 0

    report = _read_json(report_path)
    assert report["artist_batch_shots"]["checks"][0]["code"] == "P12"
    assert report["video_generator"]["checks"][0]["code"] == "P09"

    assert list(report_path.parent.glob("*.tmp")) == []
    assert (report_path.parent / "report.json.lock").is_file()
