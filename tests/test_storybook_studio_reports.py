"""Модуль studio.reports: ключи шотов, парсеры, устойчивость, кэш."""

import copy
import json
import os

import pytest

from custom_tools.storybook.studio import reports
from custom_tools.storybook.studio.errors import StudioError
from tests import storybook_report_samples as S


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    (tmp_path / "demo").mkdir()
    reports._clear_cache()
    yield tmp_path / "demo"
    reports._clear_cache()


def fs(proj, source=None):
    snap = reports.collect("demo")
    return [f for f in snap.findings if source is None or f.source == source]


def codes(proj, source):
    return [f.code for f in fs(proj, source)]


def entry(proj, source):
    return next(e for e in reports.collect("demo").entries if e.id == source)


# ---- ключи шотов ----

@pytest.mark.parametrize("value,expected", [
    ("3-2", "3-2"), ("03-02", "3-2"), ("scene_01_shot_02", "1-2"), ("scene:2:shot:1:0.00s", "2-1"),
    ("scene:2:shot:1:sample:2.50s", "2-1"), ("scene:2:shot:1:t=2.50s", "2-1"), ("1:2:end", "1-2"),
    ("2:1:0.50s", "2-1"), ("2:1:t=2.50s", "2-1"), ("2:1:9.999999999999999", "2-1"),
    ("4:1:start", "4-1"), ("VIDEO.3 t=2.50s", None), ("VIDEO.3", None), ("2-2|2-3", None),
    ("x", None), ("", None), (None, None), (5, None),
    ("/p/97_shots/scene_01_shot_01/video_final_01_01.mp4", "1-1"),
    ("video_units/2-2_2-3.mp4", None),
])
def test_parse_shot_key(value, expected):
    assert reports.parse_shot_key(value) == expected


def test_parse_shot_keys():
    assert reports.parse_shot_keys("2-2|2-3|2-2") == ["2-2", "2-3"]
    assert reports.parse_shot_keys(["1-1", "scene_01_shot_02", "bad"]) == ["1-1", "1-2"]
    assert reports.parse_shot_keys(5) == []


@pytest.mark.parametrize("scene,shot,expected", [
    (10, 1, "10-1"), ("10", "1", "10-1"), (10.0, 1, "10-1"), (True, 1, None), (None, 1, None),
    (-1, 1, None), ("x", 1, None), (1.5, 1, None), (float("nan"), 1, None),
])
def test_make_shot_key(scene, shot, expected):
    assert reports.make_shot_key(scene, shot) == expected


@pytest.mark.parametrize("value,expected", [
    ("1:2:end", ("1-2", "end")), ("4:1:start", ("4-1", "start")), ("2:1:t=2.50s", ("2-1", None)),
    ("VIDEO.3 t=2.50s (END)", (None, "end")), ("VIDEO.3", (None, None)), ("scene:2:shot:1:end", ("2-1", "end")),
    (None, (None, None)),
])
def test_parse_frame_key(value, expected):
    assert reports.parse_frame_key(value) == expected


def test_shot_sort_numeric():
    keys = ["10-1", "9-6", "2-10", "2-2", "bad"]
    assert sorted(keys, key=reports.shot_sort_key) == ["2-2", "2-10", "9-6", "10-1", "bad"]


def test_clean_text_strips_html_and_truncates():
    html_page = "<!DOCTYPE html><html><head><style>a{}</style><title>504 Gateway Timeout</title></head><body>x</body></html>"
    assert reports._clean_text(html_page) == "504 Gateway Timeout x"
    assert reports._clean_text("a" * 500).endswith("…") and len(reports._clean_text("a" * 500)) == 400
    assert reports._clean_text(None) == "" and reports._clean_text({"a": 1}) == ""


# ---- preflight ----

def test_preflight_ok(proj):
    S.write_json(proj, "preflight", S.preflight())
    assert codes(proj, "preflight") == ["provider_selected"]


@pytest.mark.parametrize("over,code,level", [
    ({"generation_enabled": False}, "generation_disabled", "info"),
    ({"status": "error"}, "preflight_failed", "error"),
    ({"shots_error": "bad json"}, "shots_json_invalid", "error"),
    ({"provider": "none", "capabilities": {"image": True, "video": False, "audio": True, "render": True}},
     "video_provider_unresolved", "error"),
    ({"capabilities": {"image": True, "video": False, "audio": True, "render": True}},
     "video_provider_unavailable", "error"),
    ({"capabilities": {"image": True, "video": True, "audio": True, "render": False}},
     "render_capability_unavailable", "error"),
    ({"expected_video_count": 0}, "no_expected_video_clips", "error"),
    ({"shots_exists": False}, "shots_json_missing_before_run", "warning"),
    ({"capabilities": {"image": False, "video": True, "audio": True, "render": True}},
     "image_provider_unconfigured", "warning"),
    ({"capabilities": {"image": True, "video": True, "audio": False, "render": True}},
     "audio_provider_unconfigured", "info"),
    ({"blockout": {"enabled": True, "use_reference": True,
                   "reference_forecast": {"eligible": 3, "not_eligible": 2}}},
     "blockout_reference_not_eligible", "info"),
])
def test_preflight_rules(proj, over, code, level):
    S.write_json(proj, "preflight", S.preflight(**over))
    found = [f for f in fs(proj, "preflight") if f.code == code]
    assert found and found[0].level == level


def test_preflight_required_env_in_message(proj):
    S.write_json(proj, "preflight", S.preflight(
        provider="veo", capabilities={"image": True, "video": False, "audio": True, "render": True}))
    msg = next(f.message for f in fs(proj, "preflight") if f.code == "video_provider_unavailable")
    assert "veo" in msg and "GEMINI_API_KEY" in msg


@pytest.mark.parametrize("over", [
    {}, {"generation_enabled": False}, {"shots_error": "x"}, {"provider": "none", "capabilities": {"video": False, "render": True}},
    {"capabilities": {"video": True, "render": False}}, {"expected_video_count": 0}, {"shots_exists": False},
])
def test_preflight_parity_with_video_contract(proj, over):
    """Коды ошибок совпадают с video_contract._readiness_blockers."""
    from custom_tools.storybook import video_contract
    summary = S.preflight(**over)
    S.write_json(proj, "preflight", summary)
    mine = {f.code for f in fs(proj, "preflight") if f.level == "error" and f.code != "preflight_failed"}
    original = set(video_contract._readiness_blockers(summary, summary["capabilities"]))
    assert mine == original
    warn = {f.code for f in fs(proj, "preflight") if f.code == "shots_json_missing_before_run"}
    assert warn == set(video_contract._readiness_warnings(summary, False, True, True))


# ---- delivery_promise ----

def test_delivery_promise_error(proj):
    S.write_json(proj, "delivery_promise", S.delivery_promise(
        status="error", will_generate_video=False,
        blocking_reasons=["video_input_invalid:2-3", "render_capability_unavailable", "weird_thing", 5, ""]))
    found = fs(proj, "delivery_promise")
    by = {f.code: f for f in found}
    assert sorted(by) == ["delivery_blocked", "render_capability_unavailable", "video_input_invalid"]
    assert by["video_input_invalid"].shot_key == "2-3" and by["video_input_invalid"].level == "error"
    assert by["delivery_blocked"].details["raw_code"] == "weird_thing"


def test_delivery_promise_error_without_reasons(proj):
    S.write_json(proj, "delivery_promise", S.delivery_promise(status="error", blocking_reasons=[], error="boom"))
    assert codes(proj, "delivery_promise") == ["delivery_blocked"]


def test_delivery_promise_skipped_and_success(proj):
    S.write_json(proj, "delivery_promise", S.delivery_promise(status="skipped", will_generate_video=False))
    assert codes(proj, "delivery_promise") == ["delivery_skipped"]
    S.write_json(proj, "delivery_promise", S.delivery_promise())
    f = fs(proj, "delivery_promise")
    assert [x.code for x in f] == ["delivery_promised"] and "3" in f[0].message


# ---- decision_log ----

def test_decision_log_rules(proj):
    S.write_json(proj, "decision_log", S.decision_log(
        status="incomplete", approved=False,
        decisions=[{"name": "render_runtime", "selected": "ffmpeg", "confidence": 0.2}, {"selected": "x"}, "junk"]))
    c = codes(proj, "decision_log")
    assert c.count("decision") == 2
    assert "decision_low_confidence" in c and "decision_incomplete" in c and "decision_not_approved" in c
    assert entry(proj, "decision_log").meta["skipped"] == 1


# ---- continuity ----

def test_continuity_takes_latest_version_and_last_record(proj):
    old = S.continuity_record("2-2|2-3", version=7, violations=[S.violation("2:2:end", "старое")])
    first = S.continuity_record("2-2|2-3", version=8, violations=[S.violation("2:2:end", "промежуточное")])
    last = S.continuity_record("2-2|2-3", version=8, violations=[S.violation("2:3:start", "актуальное")])
    S.write_json(proj, "continuity_images", S.continuity([old, first, last]))
    found = [f for f in fs(proj, "continuity_images") if f.code == "continuity_violation"]
    assert [f.message for f in found] == ["актуальное"]
    assert found[0].shot_key == "2-3" and found[0].phase == "start"
    assert found[0].details["required"] == "Герой в шапке"


def test_continuity_dismissed_uncertain_dedup_summary(proj):
    a = S.continuity_record("1-1|1-2", violations=[S.violation("1:1:end", "одно и то же")],
                            uncertain=["сомнение"], dismissed=[{"index": 0, "reason": "r"}])
    b = S.continuity_record("1-1|1-3", violations=[S.violation("1:1:end", "одно и то же")])
    ok = S.continuity_record("5-1", passed=True)
    S.write_json(proj, "continuity_images", S.continuity([a, b, ok]))
    found = fs(proj, "continuity_images")
    assert [f.code for f in found].count("continuity_violation") == 1
    unc = next(f for f in found if f.code == "continuity_uncertain")
    assert unc.level == "warning" and unc.related_shot_keys == ("1-1", "1-2")
    summary = next(f for f in found if f.code == "continuity_summary")
    assert "3" in summary.message and "1" in summary.message
    meta = entry(proj, "continuity_images").meta
    assert meta["dismissed"] == 1 and meta["passed"] == 1 and meta["failed"] == 2



def test_continuity_auto_accepted_keeps_findings_as_warnings(proj):
    # Автопринятая группа не должна выглядеть чистой: замечания остаются видны.
    rec = S.continuity_record("4-4|4-5", passed=True, version=reports._review_version()[0],
                              violations=[S.violation("4:5:end", "пылесос на полу")])
    rec["auto_accepted"] = {"failed_rounds": 2, "at": "2026-11-13T09:00:00"}
    S.write_json(proj, "continuity_images", S.continuity([rec]))
    found = fs(proj, "continuity_images")
    violation = next(f for f in found if f.code == "continuity_violation")
    assert violation.level == "warning" and violation.message == "пылесос на полу"
    assert next(f for f in found if f.code == "continuity_auto_accepted").level == "warning"
    assert entry(proj, "continuity_images").meta["passed"] == 1

def test_continuity_failed_without_details_still_reported(proj):
    S.write_json(proj, "continuity_images", S.continuity([S.continuity_record("3-1|3-2")]))
    assert "continuity_failed" in codes(proj, "continuity_images")


def test_continuity_stale_when_media_gone(proj):
    media = proj / "97_shots" / "a.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"x")
    rec = S.continuity_record("2-2", violations=[S.violation("2:2:end")], media={str(media): "h"})
    S.write_json(proj, "continuity_images", S.continuity([rec]))
    assert "continuity_violation" in codes(proj, "continuity_images")
    media.unlink()
    c = codes(proj, "continuity_images")
    assert "continuity_stale" in c and "continuity_violation" not in c


def test_continuity_verify_hashes(proj):
    import hashlib
    media = proj / "a.bin"
    media.write_bytes(b"one")
    good = hashlib.sha256(b"one").hexdigest()
    rec = S.continuity_record("2-2", violations=[S.violation("2:2:end")], media={str(media): good})
    S.write_json(proj, "continuity_images", S.continuity([rec]))
    assert "continuity_violation" in [f.code for f in reports.collect("demo", True).findings]
    media.write_bytes(b"two")
    assert "continuity_violation" in [f.code for f in reports.collect("demo", False).findings]
    assert "continuity_stale" in [f.code for f in reports.collect("demo", True).findings]


@pytest.mark.parametrize("frame,shot,phase", [
    ("4:1:start", "4-1", "start"), ("2:1:0.50s", "2-1", None), ("2:1:t=2.50s", "2-1", None),
    ("scene:2:shot:1:0.00s", "2-1", None), ("scene:2:shot:1:sample:2.50s", "2-1", None),
    ("scene:2:shot:1:end", "2-1", "end"), ("VIDEO.3 t=2.50s", None, None), ("VIDEO.3", None, None),
])
def test_continuity_videos_frame_keys(proj, frame, shot, phase):
    rec = S.continuity_record("2-2|2-3", violations=[S.violation(frame, "x")])
    S.write_json(proj, "continuity_videos", S.continuity([rec]))
    f = next(f for f in fs(proj, "continuity_videos") if f.code == "continuity_violation")
    assert f.shot_key == shot and f.phase == phase
    if shot is None:
        assert f.related_shot_keys == ("2-2", "2-3") and f.scope == "group"


# ---- shots_prompt_qa ----

def test_qa_rules(proj):
    S.write_json(proj, "shots_prompt_qa", S.qa_report(
        dry_run=True,
        missing_shots=[{"scene_number": 3, "shot_number": 1}],
        rendered_image_findings=[{"scene_number": "10", "shot_number": 2, "shot_type": "start",
                                  "observed": "лопата не там", "required": "в правой руке"}],
        rejected=[
            {"scene_number": 1, "shot_number": 5, "shot_type": "end", "reasons": ["a", "b"], "reject_reason": "not_contradictory"},
            {"scene_number": 1, "shot_number": 6, "reject_reason": "x:no_changes_after_patch"},
            {"scene_number": 1, "shot_number": 7, "reject_reason": "judge_failed_no_rewrite"},
            {"scene_number": 1, "shot_number": 8, "reject_reason": "contains_room_markers"},
            {"scene_number": 1, "shot_number": 9, "reject_reason": "shot_not_found"},
            {"scene_number": 1, "shot_number": 10, "reject_reason": "no_allowed_fields:video_prompt"},
        ],
        global_rejected=[{"scene_number": 2, "shot_number": 1, "reject_reason": "not_contradictory"}]))
    found = {(f.code, f.shot_key): f for f in fs(proj, "shots_prompt_qa")}
    assert found[("qa_shot_missing", "3-1")].level == "error"
    img = found[("qa_rendered_image", "10-2")]
    assert img.level == "warning" and img.phase == "start" and img.details["required"] == "в правой руке"
    assert found[("qa_repair_rejected", "1-5")].level == "info" and "a; b" in found[("qa_repair_rejected", "1-5")].message
    assert found[("qa_repair_rejected", "1-6")].level == "info"
    for n in (7, 8, 9, 10):
        assert found[("qa_repair_rejected", f"1-{n}")].level == "warning"
    assert found[("qa_global_repair_rejected", "2-1")].message.startswith("Сквозная проверка")
    assert ("qa_dry_run", None) in found and ("qa_summary", None) in found


# ---- provider_jobs ----

def test_provider_jobs_latest_attempt_wins(proj):
    html_err = "<!DOCTYPE html><html><body><h1>504 Gateway Timeout</h1>" + "z" * 400 + "</body></html>"
    jobs = [
        S.provider_job("2-3", "failed", "2026-10-27T09:00:00Z", error=html_err, cost=10, currency="RUB"),
        S.provider_job("2-3", "downloaded", "2026-10-27T10:00:00Z", cost=20, currency="RUB"),
        S.provider_job("3-1", "downloaded", "2026-10-27T10:00:00Z"),
        S.provider_job("3-1", "failed", "2026-10-27T11:00:00Z", error=html_err),
        S.provider_job("4-1", "stale"),
        S.provider_job("4-2", "running"),
        S.provider_job("4-3", "weird"),
        S.provider_job("", "failed", output_path="/p/scene_05_shot_01/v.mp4"),
        S.provider_job("", "failed", output_path="nothing"),
    ]
    S.write_json(proj, "provider_jobs", S.provider_jobs(jobs))
    found = {f.shot_key: f for f in fs(proj, "provider_jobs")}
    assert "2-3" not in found
    assert found["3-1"].code == "provider_job_failed" and found["3-1"].level == "error"
    assert "504 Gateway Timeout" in found["3-1"].message and "<" not in found["3-1"].message
    assert len(found["3-1"].message) < 300
    assert found["4-1"].code == "provider_job_stale" and found["4-1"].level == "warning"
    assert found["4-2"].code == "provider_job_in_progress" and found["4-3"].code == "provider_job_status_unknown"
    assert found["5-1"].code == "provider_job_failed" and "причина не указана" in found["5-1"].message
    meta = entry(proj, "provider_jobs").meta
    assert meta["jobs"] == 9 and meta["unkeyed"] == 1 and meta["failed_attempts_total"] == 4
    assert meta["cost_rub"] == 30.0 and meta["shots"] == 6


def test_provider_jobs_not_a_list(proj):
    S.write_json(proj, "provider_jobs", {"jobs": "oops"})
    assert fs(proj, "provider_jobs") == [] and entry(proj, "provider_jobs").status == "ok"


# ---- blockout ----

def test_blockout_key_forms_levels_titles(proj):
    S.write_json(proj, "blockout", {
        "blockout_renderer": {"status": "ok", "checks": [
            {"code": "B06", "level": "error", "message": "gap", "shot_key": "scene_01_shot_02"},
            {"code": "P13", "level": "warning", "scene_number": 2, "shot_number": 3, "shot_type": "end"},
            {"code": "note", "message": "английская заметка"},
            {"code": "ZZ9", "level": "garbage", "message": "что-то"},
            {"level": "info"},
            "junk",
        ]},
        "blockout_preview": "not a dict",
        "artist_batch_shots": {"checks": "not a list"},
    })
    found = fs(proj, "blockout")
    assert sorted(f.message for f in found if f.code == "note") == ["Замечание note", "английская заметка"]
    by_code = {f.code: f for f in found if f.message != "Замечание note"}
    assert by_code["B06"].shot_key == "1-2" and by_code["B06"].level == "error"
    assert by_code["B06"].message == reports.CODE_TITLES["B06"] and by_code["B06"].details["raw"] == "gap"
    assert by_code["P13"].shot_key == "2-3" and by_code["P13"].phase == "end"
    assert by_code["note"].message == "английская заметка" and by_code["note"].level == "info"
    assert by_code["ZZ9"].level == "info" and by_code["ZZ9"].details["raw_code"] == "ZZ9"
    assert by_code["ZZ9"].details["section"] == "blockout_renderer"
    assert entry(proj, "blockout").meta["sections"] == {"blockout_renderer": "ok", "artist_batch_shots": ""}


def test_blockout_chain_id_with_and_without_chains_json(proj):
    S.write_json(proj, "blockout", S.blockout_report([{"code": "B06", "level": "error", "chain_id": "chain_03"}]))
    f = fs(proj, "blockout")[0]
    assert f.shot_key is None and f.related_shot_keys == () and f.details["chain_id"] == "chain_03"
    S.write_json(proj, "chains", {"chains": [{"chain_id": "chain_03", "scene_number": 4,
                                              "shots": [{"shot_number": 1}, {"shot_number": 2}]}]})
    f = fs(proj, "blockout")[0]
    assert f.related_shot_keys == ("4-1", "4-2") and f.scope == "group"


def test_blockout_empty_object(proj):
    S.write_json(proj, "blockout", {})
    assert fs(proj, "blockout") == [] and entry(proj, "blockout").status == "ok"


# ---- render_report / final_review ----

def test_render_report_rules(proj):
    S.write_json(proj, "render_report", S.render_report(
        errors=["Missing video clips: 3", "ffmpeg render failed", "Something new"],
        warnings=["SFX files missing for Final Cut XML"],
        ffprobe_available=False, render_result={"returncode": 1},
        clips_actual_vs_planned=[
            {"scene_number": 2, "shot_number": 1, "planned_duration_seconds": 5, "actual_duration_seconds": 3.5},
            {"scene_number": 2, "shot_number": 2, "planned_duration_seconds": 5, "actual_duration_seconds": 5.06},
            {"scene_number": 2, "shot_number": 3, "planned_duration_seconds": None},
        ]))
    found = fs(proj, "render_report")
    c = {f.code: f for f in found}
    assert c["render_missing_clips"].message == "Не хватает видеоклипов: 3."
    assert c["render_error"].message == "Something new" and c["render_error"].level == "error"
    assert c["render_no_sfx"].level == "warning"
    assert c["render_clip_drift"].shot_key == "2-1" and sum(f.code == "render_clip_drift" for f in found) == 1
    assert "render_ffmpeg_missing" in c and "render_failed" in c


def test_final_review_each_failed_check(proj):
    names = ["container", "duration", "black_frame", "audio", "subtitles", "monotony", "freeze", "continuity", "other"]
    checks = {n: {"passed": False} for n in names}
    checks["duration"].update(drift_seconds=4.2, tolerance_seconds=3.2)
    checks["freeze"].update(freeze_ratio=0.2, threshold=0.1)
    checks["audio"]["status"] = "silent"
    S.write_json(proj, "final_review", S.final_review(passed=False, checks=checks))
    found = {f.code: f for f in fs(proj, "final_review")}
    for n in names:
        assert found[f"final_check_{n}"].level == "error"
    assert "4.2" in found["final_check_duration"].message and "20%" in found["final_check_freeze"].message
    assert "silent" in found["final_check_audio"].message
    assert "other" in found["final_check_other"].message


def test_final_review_passed_and_failed_without_details(proj):
    S.write_json(proj, "final_review", S.final_review())
    assert codes(proj, "final_review") == ["final_review_passed"]
    assert "Техническая проверка" in fs(proj, "final_review")[0].message
    S.write_json(proj, "final_review", S.final_review(passed=False, checks={}))
    assert codes(proj, "final_review") == ["final_review_failed"]


def test_render_and_final_review_duplicates_shown_once(proj):
    S.write_json(proj, "render_report", S.render_report(errors=["Missing video clips: 2"]))
    S.write_json(proj, "final_review", S.final_review(passed=False, errors=["Missing video clips: 2"]))
    missing = [f for f in fs(proj) if f.code == "render_missing_clips"]
    assert len(missing) == 1 and missing[0].source == "final_review"


# ---- quality_review / video_plan ----

def test_quality_review_statuses(proj):
    S.write_json(proj, "quality_review", S.quality_review([
        {"step": "artist_batch", "status": "passed"},
        {"step": "artist_batch", "status": "repaired_passed"},
        {"step": "artist_batch", "status": "failed", "issues": ["плохо", "совсем"]},
        {"step": "shots_prompt_qa", "status": "rejected_regenerating", "issues": "не список"},
        {"step": "story_writer", "status": "brand_new_status"},
        {"step": "", "status": "failed"},
        "junk",
    ]))
    found = fs(proj, "quality_review")
    assert [f.code for f in found] == ["quality_step_failed", "quality_step_note", "quality_step_note"]
    assert found[0].level == "error" and "плохо; совсем" in found[0].message
    assert "отклонён, перегенерируется" in found[1].message and "brand_new_status" in found[2].message
    assert all(f.level == "info" for f in found[1:])
    assert entry(proj, "quality_review").meta["passed"] == 2


def test_quality_review_no_checks(proj):
    S.write_json(proj, "quality_review", {"image_provider": "x"})
    assert fs(proj, "quality_review") == []


def test_video_plan(proj):
    S.write_json(proj, "video_plan", S.video_plan())
    found = {f.code: f for f in fs(proj, "video_plan")}
    assert found["video_unit_group"].related_shot_keys == ("2-2", "2-3") and found["video_unit_group"].shot_key is None
    assert found["video_method_references"].shot_key == "1-1"
    assert all(f.level == "info" for f in found.values())
    assert entry(proj, "video_plan").meta["by_method"] == {"references": 1, "keyframes": 1}
    S.write_json(proj, "video_plan", {})
    assert fs(proj, "video_plan") == []


# ---- устойчивость ----

def test_no_project():
    with pytest.raises(StudioError) as exc:
        reports.collect("ghost-project-xyz")
    assert exc.value.code == "not_found"


def test_empty_project_has_twelve_missing(proj):
    snap = reports.collect("demo")
    assert [e.id for e in snap.entries] == list(reports.SOURCE_IDS) and len(snap.entries) == 12
    assert {e.status for e in snap.entries} == {"missing"} and snap.findings == []


@pytest.mark.parametrize("content,reason", [
    (b"", "пуст"), (b"   \n", "пуст"), (b'{"a": ', "повреждён"), (b"[1, 2]", "структура"),
    (b"\xff\xfe\x00bad", "UTF-8"), (b"5", "структура"),
])
def test_broken_files_become_unreadable_warning(proj, content, reason):
    path = proj / S.FILES["provider_jobs"]
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    e = entry(proj, "provider_jobs")
    assert e.status == "invalid" and reason in e.error
    f = fs(proj, "provider_jobs")
    assert [x.code for x in f] == ["report_unreadable"] and f[0].level == "warning"
    assert e.counts["warning"] == 1


def test_too_large_file(proj, monkeypatch):
    S.write_json(proj, "provider_jobs", S.provider_jobs([]))
    monkeypatch.setattr(reports, "_MAX_FILE_BYTES", 5)
    assert "слишком большой" in entry(proj, "provider_jobs").error


def test_symlink_outside_is_missing(proj, tmp_path_factory):
    outside = tmp_path_factory.mktemp("out") / "x.json"
    outside.write_text("{}")
    link = proj / S.FILES["provider_jobs"]
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    assert entry(proj, "provider_jobs").status == "missing"


def test_parser_crash_marks_only_that_report(proj, monkeypatch):
    S.write_json(proj, "provider_jobs", S.provider_jobs([S.provider_job("1-1", "failed")]))
    S.write_json(proj, "video_plan", S.video_plan())

    def boom(data, ctx):
        raise RuntimeError("bug")

    specs = tuple(reports._Source(s.id, s.file, s.step_ids, boom if s.id == "provider_jobs" else s.parser, s.deps)
                  for s in reports.SOURCES)
    monkeypatch.setattr(reports, "SOURCES", specs)
    assert entry(proj, "provider_jobs").status == "invalid"
    assert entry(proj, "provider_jobs").error == "не удалось разобрать"
    assert entry(proj, "video_plan").status == "ok"


SAMPLES = {
    "preflight": S.preflight(), "delivery_promise": S.delivery_promise(status="error", blocking_reasons=["video_input_invalid:1-1"]),
    "decision_log": S.decision_log(), "shots_prompt_qa": S.qa_report(
        rejected=[{"scene_number": 1, "shot_number": 1, "reject_reason": "shot_not_found", "reasons": ["r"]}],
        rendered_image_findings=[{"scene_number": 1, "shot_number": 1, "shot_type": "end", "observed": "o", "required": "r"}]),
    "blockout": S.blockout_report([{"code": "B06", "level": "error", "shot_key": "scene_01_shot_01"}]),
    "continuity_images": S.continuity([S.continuity_record("1-1|1-2", violations=[S.violation("1:1:end")], uncertain=["u"])]),
    "provider_jobs": S.provider_jobs([S.provider_job("1-1", "failed", error="e", cost=1, currency="RUB")]),
    "render_report": S.render_report(errors=["Missing video clips: 1"], clips_actual_vs_planned=[
        {"scene_number": 1, "shot_number": 1, "planned_duration_seconds": 5, "actual_duration_seconds": 1}]),
    "final_review": S.final_review(passed=False, checks={"duration": {"passed": False, "drift_seconds": 1}}),
    "quality_review": S.quality_review([{"step": "artist_batch", "status": "retrying", "issues": ["i"]}]),
    "video_plan": S.video_plan(),
}


def _mutations():
    for name, sample in SAMPLES.items():
        def paths(node, prefix=()):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield prefix + (k,)
                    yield from paths(v, prefix + (k,))
            elif isinstance(node, list) and node:
                yield prefix + (0,)
                yield from paths(node[0], prefix + (0,))
        for p in paths(sample):
            for repl in (None, [], {}, "x", 123):
                yield name, p, repl


@pytest.mark.parametrize("name,path,repl", list(_mutations()))
def test_mutated_fields_never_crash(proj, name, path, repl):
    data = copy.deepcopy(SAMPLES[name])
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = repl
    S.write_json(proj, name, data)
    snap = reports.collect("demo")
    entry_ = next(e for e in snap.entries if e.id == name)
    assert entry_.status in ("ok", "warning", "error")  # разобрался, а не invalid
    json.dumps([f.to_dict() for f in snap.findings])


# ---- кэш ----

def _counting_parser(monkeypatch, source):
    calls = {"n": 0}
    spec = next(s for s in reports.SOURCES if s.id == source)

    def counted(data, ctx):
        calls["n"] += 1
        return spec.parser(data, ctx)

    specs = tuple(reports._Source(s.id, s.file, s.step_ids, counted if s.id == source else s.parser, s.deps)
                  for s in reports.SOURCES)
    monkeypatch.setattr(reports, "SOURCES", specs)
    return calls


def test_cache_reuses_parse(proj, monkeypatch):
    S.write_json(proj, "video_plan", S.video_plan())
    calls = _counting_parser(monkeypatch, "video_plan")
    reports.collect("demo")
    reports.collect("demo")
    assert calls["n"] == 1
    reports._clear_cache()
    reports.collect("demo")
    assert calls["n"] == 2


def test_cache_detects_atomic_replace_with_same_size_and_mtime(proj, monkeypatch):
    path = S.write_json(proj, "quality_review", S.quality_review([{"step": "a1", "status": "retrying"}]))
    calls = _counting_parser(monkeypatch, "quality_review")
    st = path.stat()
    reports.collect("demo")
    tmp = path.with_name("q.tmp")
    tmp.write_text(path.read_text().replace("retrying", "retryinG"))
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, path)
    assert path.stat().st_size == st.st_size and path.stat().st_mtime_ns == st.st_mtime_ns
    reports.collect("demo")
    assert calls["n"] == 2


def test_cache_invalid_result_is_cached(proj, monkeypatch):
    path = proj / S.FILES["video_plan"]
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    calls = {"n": 0}
    real = reports._read_json

    def counted(p):
        calls["n"] += 1
        return real(p)

    monkeypatch.setattr(reports, "_read_json", counted)
    reports.collect("demo")
    reports.collect("demo")
    assert calls["n"] == 1


def test_chains_change_invalidates_blockout(proj):
    S.write_json(proj, "blockout", S.blockout_report([{"code": "B06", "level": "error", "chain_id": "c1"}]))
    assert fs(proj, "blockout")[0].related_shot_keys == ()
    S.write_json(proj, "chains", {"chains": [{"chain_id": "c1", "scene_number": 1, "shots": [{"shot_number": 1}]}]})
    assert fs(proj, "blockout")[0].related_shot_keys == ("1-1",)


def test_deleted_file_becomes_missing(proj):
    path = S.write_json(proj, "video_plan", S.video_plan())
    assert entry(proj, "video_plan").status == "ok"
    path.unlink()
    assert entry(proj, "video_plan").status == "missing"


def test_cache_eviction(proj, monkeypatch):
    monkeypatch.setattr(reports, "_CACHE_LIMIT", 3)
    for i in range(6):
        p = proj / f"f{i}.json"
        p.write_text("{}")
        spec = next(s for s in reports.SOURCES if s.id == "video_plan")
        reports._load("demo", spec, p)
    assert len(reports._CACHE) == 3


def test_old_review_version_records_are_not_live(proj):
    old = S.continuity_record("2-2", version=reports._review_version()[0] - 1, violations=[S.violation("2:2:end")])
    nover = S.continuity_record("2-3", version=None, violations=[S.violation("2:3:end")])
    S.write_json(proj, "continuity_images", S.continuity([old, nover]))
    assert fs(proj, "continuity_images") == []


def test_finding_ids_unique_for_same_shot_code_phase(proj):
    chk = lambda msg: {"code": "B03", "level": "warning", "shot_key": "scene_01_shot_02", "shot_type": "end", "message": msg}
    S.write_json(proj, "blockout", {
        "blockout_renderer": {"checks": [chk("one"), chk("two"), chk("one")]},
        "blockout_preview": {"checks": [chk("one")]},
    })
    ids = [f.id for f in fs(proj, "blockout")]
    assert len(ids) == 4 and len(set(ids)) == 4
    reports._clear_cache()
    assert [f.id for f in fs(proj, "blockout")] == ids


def test_media_path_outside_project_counts_as_missing(proj, tmp_path_factory):
    outside = tmp_path_factory.mktemp("out") / "a.mp4"
    outside.write_bytes(b"x")
    rec = S.continuity_record("2-2", violations=[S.violation("2:2:end")], media={str(outside): "h"})
    S.write_json(proj, "continuity_images", S.continuity([rec]))
    c = codes(proj, "continuity_images")
    assert "continuity_stale" in c and "continuity_violation" not in c


def test_clean_text_keeps_comparisons():
    assert reports._clean_text("a<5 and b>3") == "a<5 and b>3"
    assert reports._clean_text("x <b>bold</b> y") == "x bold y"


def test_chains_map_cached(proj, monkeypatch):
    S.write_json(proj, "chains", {"chains": [{"chain_id": "c1", "scene_number": 1, "shots": [{"shot_number": 1}]}]})
    real = reports._read_json
    calls = {"n": 0}

    def counted(p):
        calls["n"] += 1
        return real(p)

    monkeypatch.setattr(reports, "_read_json", counted)
    reports._chains_map("demo", S.FILES["chains"])
    reports._chains_map("demo", S.FILES["chains"])
    assert calls["n"] == 1


def test_import_does_not_load_continuity_review():
    import subprocess
    import sys
    code = ("import sys; import custom_tools.storybook.studio.reports; "
            "print('custom_tools.storybook.continuity_review' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]), timeout=60)
    assert out.stdout.strip() == "False", out.stderr


def test_review_version_fallback_is_flagged(proj, monkeypatch):
    monkeypatch.setattr(reports, "_REVIEW_VERSION_CACHE", (8, False))
    S.write_json(proj, "continuity_images", S.continuity([S.continuity_record("2-2", passed=True)]))
    assert entry(proj, "continuity_images").meta["review_version_known"] is False
