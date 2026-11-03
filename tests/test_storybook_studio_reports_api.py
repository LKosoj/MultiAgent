"""Сводка и замечания отчётов (внутренние функции) и возможность reports_for_shot."""

import json
import os
from pathlib import Path

import pytest

from custom_tools.storybook.studio import capabilities as caps
from custom_tools.storybook.studio import reports
from custom_tools.storybook.studio.errors import StudioError
from tests import storybook_report_samples as S


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    (tmp_path / "demo").mkdir()
    reports._clear_cache()
    p = tmp_path / "demo"
    S.write_json(p, "provider_jobs", S.provider_jobs([
        S.provider_job("2-3", "failed", error="timeout"), S.provider_job("10-1", "failed", error="e"),
        S.provider_job("9-6", "stale"), S.provider_job("1-1", "running")]))
    S.write_json(p, "continuity_videos", S.continuity([
        S.continuity_record("2-2|2-3", violations=[S.violation("VIDEO.3 t=2.50s", "по группе"),
                                                   S.violation("2:3:end", "по кадру")], uncertain=["сомнение"])]))
    S.write_json(p, "shots_prompt_qa", S.qa_report(rendered_image_findings=[
        {"scene_number": 2, "shot_number": 3, "shot_type": "start", "observed": "o", "required": "r"}]))
    yield p
    reports._clear_cache()


def call(cap, **payload):
    """Возможность через реестр (reports_for_shot)."""
    return caps.invoke(cap, {"project_id": "demo", **payload})


def findings(project_id="demo", **payload):
    return reports.reports_findings(project_id, **payload)


def snapshot(project_id="demo"):
    return reports.collect(project_id)


def test_registered_read_only():
    reg = caps.registry()
    assert reg["reports_for_shot"].access == "read" and reg["reports_for_shot"].category == "reports"
    assert reg["reports_for_shot"].project_scoped
    assert "reports_summary" not in reg and "reports_findings" not in reg


def test_summary_shape_and_totals(proj):
    snap = snapshot()
    entries = [e.to_dict() for e in snap.entries]
    json.dumps(entries)
    assert len(entries) == 12
    assert [e["id"] for e in entries] == list(reports.SOURCE_IDS)
    for level in reports.LEVELS:
        assert snap.totals[level] == sum(e["counts"][level] for e in entries)
    jobs = next(e for e in entries if e["id"] == "provider_jobs")
    assert jobs["status"] == "error" and jobs["status_title"] == "Есть ошибки" and jobs["updated_at"].endswith("Z")
    assert jobs["meta"]["shots"] == 4
    missing = next(e for e in entries if e["id"] == "preflight")
    assert missing["status"] == "missing" and missing["status_title"] == "Нет файла" and missing["updated_at"] is None


def test_summary_by_shot_includes_group_shots(proj):
    by = snapshot().by_shot
    assert by["2-2"]["warning"] == 1 and by["2-2"]["error"] == 1  # группа: VIDEO.3 + сомнение
    assert by["2-3"]["error"] == 3  # провайдер, кадр, группа
    assert list(by) == sorted(by, key=reports.shot_sort_key)
    assert by["9-6"]["warning"] == 1 and "1-1" in by


def test_findings_defaults_exclude_info_and_sorted(proj):
    r = findings()
    json.dumps(r)
    assert r["status"] == "success" and r["truncated"] is False
    levels = [f["level"] for f in r["findings"]]
    assert "info" not in levels and levels == sorted(levels, key=lambda x: reports.LEVELS.index(x))
    errors = [f for f in r["findings"] if f["level"] == "error"]
    order = [reports.SOURCE_IDS.index(f["source"]) for f in errors]
    assert order == sorted(order)
    jobs = [f["shot_key"] for f in errors if f["source"] == "provider_jobs"]
    assert jobs == ["2-3", "10-1"]  # числовой порядок
    assert r["total"] == len(r["findings"]) and r["counts"]["info"] == 0


def test_findings_filters(proj):
    only = findings(shot_key="2-3")
    assert {f["shot_key"] is None or f["shot_key"] == "2-3" for f in only["findings"]} == {True}
    assert findings(shot_key="scene_02_shot_03")["total"] == only["total"]
    assert findings(shot_key="2:3:end")["total"] == only["total"]
    info = findings(levels=["info"], sources=["provider_jobs"])
    assert [f["code"] for f in info["findings"]] == ["provider_job_in_progress"]
    both = findings(sources=["shots_prompt_qa"], levels=["warning", "error"])
    assert {f["source"] for f in both["findings"]} == {"shots_prompt_qa"}


def test_findings_paging(proj):
    full = findings()["findings"]
    page = findings(limit=2, offset=1)
    assert [f["id"] for f in page["findings"]] == [f["id"] for f in full[1:3]]
    assert page["truncated"] is True and page["total"] == len(full) and page["counts"]["error"] > 0
    last = findings(limit=1000, offset=len(full) - 1)
    assert last["truncated"] is False and len(last["findings"]) == 1


@pytest.mark.parametrize("payload", [
    {"levels": ["fatal"]}, {"levels": "error"}, {"sources": ["nope"]}, {"sources": "x"},
    {"shot_key": "abc"}, {"shot_key": 5}, {"limit": 0}, {"limit": 1001}, {"limit": "5"}, {"offset": -1},
    {"limit": True},
])
def test_findings_invalid_arguments(proj, payload):
    with pytest.raises(StudioError) as exc:
        findings(**payload)
    assert exc.value.code == "invalid_argument"


def test_for_shot_groups(proj):
    r = call("reports_for_shot", shot_key="2-3")
    json.dumps(r)
    assert r["status"] == "success" and r["shot_key"] == "2-3"
    assert [g["source"] for g in r["groups"]] == ["shots_prompt_qa", "provider_jobs", "continuity_videos"]
    assert sum(len(g["findings"]) for g in r["groups"]) == sum(r["counts"].values())
    videos = next(g for g in r["groups"] if g["source"] == "continuity_videos")
    assert {f["scope"] for f in videos["findings"]} == {"shot", "group"}
    assert videos["status"] == "error" and videos["title"] == "Проверка видео"


def test_for_shot_unknown_shot_is_empty_and_bad_key_is_error(proj):
    r = call("reports_for_shot", shot_key="77-7")
    assert r["status"] == "success" and r["groups"] == [] and r["counts"] == {"error": 0, "warning": 0, "info": 0}
    assert call("reports_for_shot", shot_key="zzz")["error"]["code"] == "invalid_argument"
    assert call("reports_for_shot", shot_key="scene_02_shot_03")["shot_key"] == "2-3"


def test_project_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    for project_id, code in (("ghost", "not_found"), ("..", "invalid_argument")):
        with pytest.raises(StudioError) as exc:
            reports.collect(project_id)
        assert exc.value.code == code
        with pytest.raises(StudioError) as exc:
            findings(project_id)
        assert exc.value.code == code


def test_empty_project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    (tmp_path / "empty").mkdir()
    reports._clear_cache()
    snap = snapshot("empty")
    assert snap.totals == {"error": 0, "warning": 0, "info": 0} and snap.by_shot == {}
    assert {e.status for e in snap.entries} == {"missing"}


def test_shot_counts_matches_snapshot(proj):
    assert reports.shot_counts("demo") == snapshot().by_shot


def test_finding_ids_are_stable(proj):
    a = [f["id"] for f in findings(levels=["error", "warning", "info"])["findings"]]
    reports._clear_cache()
    b = [f["id"] for f in findings(levels=["error", "warning", "info"])["findings"]]
    assert a == b and len(set(a)) == len(a)


REAL = Path(__file__).resolve().parents[1] / "plots/storybooks/platonov_20261024"


@pytest.mark.skipif(not REAL.is_dir(), reason="нет реального проекта platonov_20261024")
def test_smoke_on_real_project(monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(REAL.parent))
    reports._clear_cache()
    entries = [e.to_dict() for e in snapshot(REAL.name).entries]
    json.dumps(entries)
    assert len(entries) == 12
    jobs = next(e for e in entries if e["id"] == "provider_jobs")
    assert jobs["meta"]["shots"] == 50 and jobs["meta"]["by_latest_status"].get("failed") == 1
    found = findings(REAL.name, sources=["continuity_videos"], levels=["error"], limit=1000)
    assert any(f["shot_key"] is None and f["related_shot_keys"] for f in found["findings"])
    assert reports.shot_counts(REAL.name)
