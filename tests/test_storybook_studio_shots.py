"""Ф6: шоты (список, карточка, устаревание, правка, отпечатки)."""

import hashlib
import importlib
import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.studio_run_fakes import make_series_project
from custom_tools.storybook.project_paths import series_scope

from custom_tools.storybook.studio import capabilities as caps

PID = "demo"


def shots():
    return importlib.import_module("custom_tools.storybook.studio.shots")


def fpm():
    return importlib.import_module("custom_tools.storybook.studio._shots_fp")


def art():
    return importlib.import_module("custom_tools.storybook.studio.artifacts")


def call(cap, **payload):
    return caps.invoke(cap, {"project_id": PID, "series_id": "main", **payload})


def item(scene, shot, phase, **extra):
    base = {
        "scene_number": scene, "shot_number": shot, "shot_type": phase, "timing": "00:05",
        "english_prompt": f"prompt {phase} {scene}-{shot}", "negative_prompt": "neg",
        "video_prompt": "move" if phase == "start" else "",
        "output_path": f"plots/storybooks/{PID}/97_shots/scene_{scene:02d}_shot_{shot:02d}/img_final_{phase}_{scene:02d}_{shot:02d}.png",
        "image_path": None, "reference_image_paths": [], "width": 64, "height": 64, "seed": 1,
    }
    base.update(extra)
    return base


def build(root, pairs=((1, 1), (2, 3)), with_files=True, end=True):
    items = []
    for scene, shot in pairs:
        items.append(item(scene, shot, "start"))
        if end:
            items.append(item(scene, shot, "end"))
        d = root / "97_shots" / f"scene_{scene:02d}_shot_{shot:02d}"
        d.mkdir(parents=True, exist_ok=True)
        if with_files:
            (d / f"img_final_start_{scene:02d}_{shot:02d}.png").write_bytes(b"S%d%d" % (scene, shot))
            if end:
                (d / f"img_final_end_{scene:02d}_{shot:02d}.png").write_bytes(b"E%d%d" % (scene, shot))
            (d / f"video_final_{scene:02d}_{shot:02d}.mp4").write_bytes(b"V%d%d" % (scene, shot))
    (root / "97_shots" / "shots.json").write_text(
        json.dumps({"seed": 7, "consistency_rules": ["a"], "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")


def read_items(root):
    return json.loads((root / "97_shots" / "shots.json").read_text(encoding="utf-8"))


def write_items(root, doc):
    (root / "97_shots" / "shots.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    root = make_series_project(tmp_path, PID)
    build(root)
    s = shots()
    s._CACHE.clear()
    fpm()._STORE_CACHE.clear()
    with series_scope(PID, "main"):
        yield root


def by_key(result):
    return {r["shot_key"]: r for r in result["items"]}


def adopt():
    return call("shots_adopt_baseline")


def bump(path, seconds=10):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 10**9))


# --- модель --------------------------------------------------------------------------------


def test_pair_is_one_row_and_keys(project):
    build(project, pairs=((9, 6), (10, 1), (100, 1)))
    res = call("shots_list")
    assert [r["shot_key"] for r in res["items"]] == ["9-6", "10-1", "100-1"]
    row = res["items"][0]
    assert row["parts"]["start"]["exists"] and row["parts"]["end"]["state"] == "ready"
    assert row["duration_s"] == 5 and row["duration_source"] == "parsed" and row["state"] == "ready"
    assert shots().shot_dir_name(2, 3) == "scene_02_shot_03" and shots().shot_dir_name(100, 1) == "scene_100_shot_01"


def test_row_id_is_not_masked_name(project):
    # поле "key" бэкенд маскирует как секрет, поэтому идентификатор называется shot_key
    from backend.fastapi_app.agui.redaction import _redact_payload

    row = call("shots_list")["items"][0]
    assert "key" not in row and _redact_payload(row)["shot_key"] == row["shot_key"]


def test_media_paths_normalized(project):
    s = shots()
    assert s.normalize_media_path(PID, f"plots/storybooks/{PID}/97_shots/scene_02_shot_03/img_final_start_02_03.png",
                                  "2-3", "start") == "97_shots/scene_02_shot_03/img_final_start_02_03.png"
    assert s.normalize_media_path(PID, "/abs/elsewhere/scene_02_shot_03/video_final_02_03.mp4", "2-3", "video") \
        == "97_shots/scene_02_shot_03/video_final_02_03.mp4"
    assert s.normalize_media_path(PID, "../../etc/passwd", "2-3", "end") == "97_shots/scene_02_shot_03/img_final_end_02_03.png"
    assert s.normalize_media_path(PID, "x/scene_02_shot_03/..", "2-3", "end").endswith("img_final_end_02_03.png")


def test_prev_end_flag_tolerant_and_no_end_item(project):
    doc = read_items(project)
    doc["items"][1]["should_use_prev_end_as_reference"] = "true"
    doc["items"] = [i for i in doc["items"] if not (i["scene_number"] == 2 and i["shot_type"] == "end")]
    write_items(project, doc)
    row = by_key(call("shots_list"))["2-3"]
    assert row["parts"]["end"]["state"] == "ready"  # файл конца есть
    (project / "97_shots/scene_02_shot_03/img_final_end_02_03.png").unlink()
    row = by_key(call("shots_list"))["2-3"]
    assert row["parts"]["end"]["state"] == "not_applicable" and row["state"] == "ready"


def test_missing_start_item_and_duplicates(project):
    doc = read_items(project)
    doc["items"] = [i for i in doc["items"] if not (i["scene_number"] == 1 and i["shot_type"] == "start")]
    doc["items"].append(dict(doc["items"][-1], english_prompt="duplicate"))
    write_items(project, doc)
    card = call("shot_get", shot_key="1-1")["shot"]
    assert "start_item_missing" in card["warnings"] and card["editable"]["locked"] and card["editable"]["fields"] == []
    again = call("shot_get", shot_key="2-3")["shot"]
    assert again["start"]["prompt"] == "prompt start 2-3"
    err = call("shot_update", shot_key="1-1", changes={"video_prompt": "x"}, base_digest=card["digest"])
    assert err["error"]["code"] == "invalid_argument"
    doc = read_items(project)
    doc["items"].append(dict(doc["items"][-1], english_prompt="duplicate"))
    write_items(project, doc)
    card = call("shot_get", shot_key="2-3")
    assert "duplicate_item" in card["shot"]["warnings"] and card["shot"]["end"]["prompt"] == "prompt end 2-3"


def test_no_shots_file_and_broken_json(project):
    (project / "97_shots/shots.json").unlink()
    res = call("shots_list")
    assert res["status"] == "success" and res["items"] == [] and res["reason"] == "no_shots_file"
    assert call("shot_get", shot_key="1-1")["error"]["code"] == "not_found"
    (project / "97_shots/shots.json").write_text("{broken")
    assert call("shots_list")["error"]["code"] == "unavailable"
    assert shots().stale_shots_summary(PID) is None


def test_timing_parser_matches_original():
    s = shots()
    cases = {"00:05": 5, "00:00 - 00:05": 5, "5s": 5, "7": 7, "01:02": 62, "00:00:09": 9}
    for text, expected in cases.items():
        assert s.parse_timing_seconds(text) == expected
    assert s.parse_timing_seconds("мусор") is None and s.parse_timing_seconds("") is None
    try:
        from custom_tools.storybook.video_generator_common import parse_duration_seconds_from_timing as orig
    except Exception:
        pytest.skip("video_generator_common не импортируется")
    for text in cases:
        assert orig(text) == s.parse_timing_seconds(text)


def test_duration_prefers_duration_s_over_timing(project):
    doc = read_items(project)
    doc["items"][0].update(duration_s=8, duration_source="manual")
    write_items(project, doc)
    row = by_key(call("shots_list"))["1-1"]
    assert row["duration_s"] == 8 and row["duration_source"] == "manual"


# --- состояния -----------------------------------------------------------------------------


def jobs_file(project, jobs):
    (project / "97_shots/provider_jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


def job(key, status, at, **extra):
    return {"shot_key": key, "status": status, "provider": "p", "model": "m",
            "timestamps": {"updated_at": at}, **extra}


def test_state_table(project):
    build(project, pairs=((1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6)))
    d = project / "97_shots"
    (d / "scene_01_shot_02/img_final_end_01_02.png").unlink()  # partial
    for f in (d / "scene_01_shot_03").iterdir():
        f.unlink()  # missing
    (d / "scene_01_shot_04/video_final_01_04.mp4").unlink()  # без видео и без задач
    jobs_file(project, [job("1-5", "downloaded", "2026-01-01T00:00:00Z"), job("1-5", "failed", "2026-01-02T00:00:00Z"),
                        job("1-6", "submitting", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))])
    rows = by_key(call("shots_list"))
    assert rows["1-1"]["state"] == "ready"
    assert rows["1-2"]["state"] == "partial" and rows["1-2"]["state_title"] == "Не хватает материалов"
    assert rows["1-3"]["state"] == "missing" and rows["1-3"]["state_title"] == "Нет материалов"
    assert rows["1-4"]["state"] == "ready" and rows["1-4"]["parts"]["video"]["state"] == "not_started"
    assert rows["1-5"]["state"] == "error" and rows["1-5"]["job"]["attempts"] == 2
    assert rows["1-6"]["state"] == "generating"


def test_video_missing_when_job_exists_without_file(project):
    (project / "97_shots/scene_01_shot_01/video_final_01_01.mp4").unlink()
    jobs_file(project, [job("1-1", "downloaded", "2026-01-01T00:00:00Z")])
    row = by_key(call("shots_list"))["1-1"]
    assert row["parts"]["video"]["state"] == "missing" and row["state"] == "partial"


def test_video_unit_block(project):
    (project / "97_shots/scene_01_shot_01/video_final_01_01.mp4").unlink()
    (project / "97_shots/video_units").mkdir()
    (project / "97_shots/video_units/1-1_1-2.mp4").write_bytes(b"unit")
    (project / "97_shots/video_plan.json").write_text(json.dumps({"units": [
        {"shot_keys": ["1-1", "1-2"], "video_path": "/abs/path/video_units/1-1_1-2.mp4"}]}))
    video = by_key(call("shots_list"))["1-1"]["parts"]["video"]
    assert video["unit"] == {"keys": ["1-1", "1-2"], "path": "97_shots/video_units/1-1_1-2.mp4"} and video["exists"]


def test_image_generating_hook(project, monkeypatch):
    (project / "97_shots/scene_01_shot_01/img_final_end_01_01.png").unlink()
    mod = importlib.import_module("custom_tools.storybook.studio.shots")
    monkeypatch.setattr(mod, "_optional", lambda m, n: (lambda pid: {"step_id": "artist_batch_shots"})
                        if (m, n) == ("runs", "run_progress") else None)
    row = by_key(call("shots_list"))["1-1"]
    assert row["parts"]["end"]["state"] == "generating" and row["state"] == "generating"


# --- устаревание ---------------------------------------------------------------------------


def test_unknown_without_baseline_then_adopt(project):
    res = call("shots_list")
    assert {r["freshness"] for r in res["items"]} == {"unknown"} and res["facets"]["stale"] == 0
    assert res["baseline"] == {"missing": 2, "outdated": 0}
    out = adopt()
    assert out["adopted"] == 2 and out["parts"] == 6 and out["skipped_stale"] == 0
    assert {r["freshness"] for r in call("shots_list")["items"]} == {"fresh"}
    assert (project / ".studio/shot_inputs.json").is_file()
    assert adopt()["parts"] == 0


def test_prompt_edit_makes_only_start_stale(project):
    adopt()
    doc = read_items(project)
    doc["items"][0]["english_prompt"] = "новый промпт"
    write_items(project, doc)
    card = call("shot_get", shot_key="1-1")["shot"]
    assert card["stale_parts"] == ["start"] and card["stale_reasons"] == {"start": ["prompt_changed"]}
    assert card["cascade"] == {"stale_now": ["start"], "stale_after_regeneration": ["end", "video"]}
    assert card["state"] == "stale" and by_key(call("shots_list"))["2-3"]["state"] == "ready"
    assert call("shots_list", stale_only=True)["total"] == 1
    assert shots().stale_shots_summary(PID) == {"count": 1, "keys": ["1-1"]}
    skipped = adopt()
    assert skipped["skipped_stale"] == 1 and skipped["parts"] == 0


def test_reference_change_and_start_image_cascade(project):
    ref = project / "20_bible/references/locations/x.png"
    ref.parent.mkdir(parents=True)
    ref.write_bytes(b"ref1")
    doc = read_items(project)
    doc["items"][0]["reference_image_paths"] = ["/references/locations/x.png"]
    write_items(project, doc)
    adopt()
    ref.write_bytes(b"ref22")
    card = call("shot_get", shot_key="1-1")["shot"]
    assert card["stale_reasons"]["start"] == ["references_changed"]


def test_start_image_change_stales_end_and_video(project):
    adopt()
    (project / "97_shots/scene_01_shot_01/img_final_start_01_01.png").write_bytes(b"other bytes")
    card = call("shot_get", shot_key="1-1")["shot"]
    assert card["parts"]["start"]["freshness"] == "unknown"  # файл заменён без записи
    assert card["unknown_reasons"]["start"] == ["file_replaced"]
    assert card["stale_reasons"]["end"] == ["start_image_changed"]
    assert "start_image_changed" in card["stale_reasons"]["video"]


def test_project_copy_stays_fresh(project, tmp_path, monkeypatch):
    doc = read_items(project)
    doc["items"][1]["reference_image_paths"] = [f"plots/storybooks/{PID}/97_shots/scene_01_shot_01/img_final_start_01_01.png"]
    write_items(project, doc)
    adopt()
    copy_root = tmp_path / "demo2"
    shutil.copytree(project.parents[1], copy_root)
    for f in copy_root.rglob("*"):
        if f.is_file():
            bump(f, 3)  # mtime всех файлов другой, но одинаково
    res = caps.invoke("shots_list", {"project_id": "demo2", "series_id": "main"})
    assert {r["freshness"] for r in res["items"]} == {"fresh"}


def test_fp_version_change_reads_unknown(project):
    adopt()
    path = project / ".studio/shot_inputs.json"
    data = json.loads(path.read_text())
    data["fp_version"] = 99
    path.write_text(json.dumps(data))
    assert {r["freshness"] for r in call("shots_list")["items"]} == {"unknown"}


def test_video_older_than_image_and_job_stale(project):
    adopt()
    start = project / "97_shots/scene_01_shot_01/img_final_start_01_01.png"
    # запись отпечатка есть, токены картинок те же: одно время файла stale не даёт
    bump(start, 3)
    card = call("shot_get", shot_key="1-1")["shot"]
    assert "source_image_newer" not in card["stale_reasons"].get("video", [])
    jobs_file(project, [job("2-3", "stale", "2026-01-01T00:00:00Z")])
    card = call("shot_get", shot_key="2-3")["shot"]
    assert "provider_job_stale" in card["stale_reasons"]["video"]


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def test_source_image_newer_by_hashes(project):
    start = project / "97_shots/scene_01_shot_01/img_final_start_01_01.png"
    end = project / "97_shots/scene_01_shot_01/img_final_end_01_01.png"
    hashes = {"start_image": _sha(b"S11"), "end_image": _sha(b"E11")}
    jobs_file(project, [job("1-1", "downloaded", "2026-01-01T00:00:00Z", source_image_hashes=hashes)])
    bump(start, 5)  # только время: байты те же
    card = call("shot_get", shot_key="1-1")["shot"]
    assert "source_image_newer" not in card["stale_reasons"].get("video", [])
    start.write_bytes(b"other")  # другие байты
    bump(start, 5)
    card = call("shot_get", shot_key="1-1")["shot"]
    assert "source_image_newer" in card["stale_reasons"]["video"]


def test_source_image_newer_hashes_from_downloaded_job(project):
    start = project / "97_shots/scene_01_shot_01/img_final_start_01_01.png"
    old = {"start_image": _sha(b"S11"), "end_image": _sha(b"E11")}
    new = {"start_image": _sha(b"regen"), "end_image": _sha(b"E11")}
    start.write_bytes(b"regen")  # картинку перегенерировали
    jobs_file(project, [job("1-1", "downloaded", "2026-01-01T00:00:00Z", source_image_hashes=old),
                        job("1-1", "failed", "2026-01-02T00:00:00Z", source_image_hashes=new)])
    card = call("shot_get", shot_key="1-1")["shot"]
    assert "source_image_newer" in card["stale_reasons"]["video"]
    # хеши по ролям: start от end отличать нельзя
    jobs_file(project, [job("1-1", "downloaded", "2026-01-01T00:00:00Z",
                            source_image_hashes={"start_image": _sha(b"E11"), "end_image": _sha(b"regen")})])
    card = call("shot_get", shot_key="1-1")["shot"]
    assert "source_image_newer" in card["stale_reasons"]["video"]


def test_sha256_cache(project):
    mod = shots()
    f = project / "97_shots/scene_01_shot_01/img_final_start_01_01.png"
    first = mod._sha256(f)
    reads = mod._SHA_STATS["reads"]
    assert mod._sha256(f) == first and mod._SHA_STATS["reads"] == reads
    f.write_bytes(b"changed!")
    assert mod._sha256(f) != first and mod._SHA_STATS["reads"] == reads + 1


def test_source_image_newer_mtime_fallback(project):
    bump(project / "97_shots/scene_01_shot_01/img_final_start_01_01.png", 5)
    card = call("shot_get", shot_key="1-1")["shot"]
    assert "source_image_newer" in card["stale_reasons"]["video"]


def test_reconcile_after_run(project):
    adopt()
    old = time.time() - 1000
    for f in (project / "97_shots").rglob("*.*"):
        os.utime(f, (old, old))
    new_start = project / "97_shots/scene_01_shot_01/img_final_start_01_01.png"
    new_start.write_bytes(b"regenerated")
    os.utime(new_start, None)
    before = fpm().load_records(PID)["1-1"]["end"]["at"]
    res = shots().reconcile_after_run(PID, time.time() - 5)
    assert res["updated"] == 1 and res["shots"] == 1
    rec = fpm().load_records(PID)
    assert rec["1-1"]["start"]["source"] == "generated" and rec["1-1"]["end"]["at"] == before
    card = call("shot_get", shot_key="1-1")["shot"]
    assert card["parts"]["start"]["freshness"] == "fresh"
    assert card["stale_reasons"].get("end") == ["start_image_changed"]


def test_baseline_record_roundtrip(project):
    adopt()
    s = shots()
    rec = s.get_baseline_record(PID, "1-1", "start")
    assert rec["source"] == "adopted"
    s.set_baseline_record(PID, "1-1", "start", rec)
    assert s.get_baseline_record(PID, "1-1", "start")["source"] == "restored"
    with pytest.raises(Exception):
        s.set_baseline_record(PID, "1-1", "start", {"fp": "x"})


def test_capture_baseline_before_raw_save(project):
    # сырой редактор Ф10 правит промпт: отпечаток записан до правки, шот сразу «устарел»
    a = art()
    path = project / "97_shots/shots.json"
    digest = a.file_digest(path.read_bytes())
    content = json.loads(path.read_text())
    content["items"][0]["english_prompt"] = "через редактор"
    res = call("artifact_save", key="shots", base_digest=digest, content=content)
    assert res["status"] == "success", res
    assert call("shot_get", shot_key="1-1")["shot"]["stale_parts"] == ["start"]


def test_reads_do_not_write(project):
    before = (project / "97_shots/shots.json").stat().st_mtime_ns
    call("shots_list")
    call("shot_get", shot_key="1-1")
    call("shots_keys")
    assert not (project / ".studio").exists()
    assert (project / "97_shots/shots.json").stat().st_mtime_ns == before


# --- список --------------------------------------------------------------------------------


def test_filters_pagination_sort(project):
    build(project, pairs=((1, 1), (1, 2), (2, 3)))
    doc = read_items(project)
    doc["items"][0]["english_prompt"] = "Зимний ДВОР"
    write_items(project, doc)
    jobs_file(project, [job("1-2", "failed", "2026-01-01T00:00:00Z", cost=9.5, currency="RUB")])
    assert call("shots_list", scene=2)["total"] == 1
    assert call("shots_list", state="error")["total"] == 1
    assert call("shots_list", query="зимний двор")["total"] == 1 and call("shots_list", query="2-3")["total"] == 1
    assert call("shots_list", freshness="unknown")["total"] == 3
    assert call("shots_list", limit=2)["total"] == 3 and len(call("shots_list", limit=2)["items"]) == 2
    assert call("shots_list", offset=50)["items"] == []
    assert call("shots_list", sort="state")["items"][0]["shot_key"] == "1-2"
    assert call("shots_list", sort="cost")["items"][0]["shot_key"] == "1-2"
    facets = call("shots_list", scene=2)["facets"]
    assert facets["state"]["ready"] == 2 and facets["scenes"] == [{"scene": 1, "count": 2}, {"scene": 2, "count": 1}]
    for bad in ({"limit": 0}, {"limit": 201}, {"state": "x"}, {"sort": "x"}, {"review": "x"}, {"offset": -1}):
        assert call("shots_list", **bad)["error"]["code"] == "invalid_argument"
    plain = call("shots_keys")
    assert plain["keys"] == [r["shot_key"] for r in call("shots_list")["items"]] and not plain["truncated"]
    assert call("shots_keys", limit=1)["truncated"] is True
    assert call("shots_keys", state="error")["keys"] == ["1-2"]


def test_review_hook_fallback_and_findings(project, monkeypatch):
    (project / "97_shots/review_state.json").write_text(json.dumps({"shots": {"1-1": {"status": "approved", "note": "ок"}}}))
    rows = by_key(call("shots_list"))
    assert rows["1-1"]["review"] == {"status": "approved", "status_title": "Одобрен"} and rows["2-3"]["review"] is None
    assert call("shots_list", review="approved")["total"] == 1
    (project / "97_shots/review_state.json").write_text(json.dumps({"2-3": "weird"}))
    assert by_key(call("shots_list"))["2-3"]["review"]["status"] == "pending"
    (project / "97_shots/review_state.json").write_text("{не json")
    res = call("shots_list")
    assert res["status"] == "success" and by_key(res)["1-1"]["review"] is None and res["hook_errors"]
    reports = importlib.import_module("custom_tools.storybook.studio.reports")
    monkeypatch.setattr(reports, "shot_counts", lambda pid: (_ for _ in ()).throw(RuntimeError("x")))
    res = call("shots_list")
    assert res["status"] == "success" and res["items"][0]["findings"] is None
    assert any("shot_counts" in e for e in res["hook_errors"])
    monkeypatch.setattr(reports, "shot_counts", lambda pid: {"1-1": {"error": 0, "warning": 2, "info": 0}})
    assert call("shots_list", with_findings=True)["total"] == 1


def test_card_has_job_history_and_edits_hint(project):
    jobs_file(project, [job("1-1", "failed", f"2026-01-0{i}T00:00:00Z", cost=1.0, currency="RUB",
                            error="boom https://x.io/a?token=1 " + "я" * 400, video_url="https://secret", task_id="t")
                        for i in range(1, 8)])
    card = call("shot_get", shot_key="scene_01_shot_01")["shot"]
    assert card["shot_key"] == "1-1" and card["job"]["attempts"] == 7 and len(card["job"]["recent"]) == 5
    text = json.dumps(card["job"], ensure_ascii=False)
    assert "secret" not in text and "task_id" not in text and "https://" not in text
    assert len(card["job"]["recent"][0]["error"]) <= 200
    assert "Раскадровка" in card["editable"]["hint"]
    assert call("shot_get", shot_key="zzz")["error"]["code"] == "invalid_argument"
    assert call("shot_get", shot_key="9-9")["error"]["code"] == "not_found"


def test_performance_counters(project):
    build(project, pairs=[(1, n) for n in range(1, 31)])
    s, f = shots(), fpm()
    s._CACHE.clear()
    before = s.STATS["shots_parse"]
    t = time.time()
    call("shots_list")
    call("shots_list")
    assert s.STATS["shots_parse"] - before == 1 and time.time() - t < 3
    reads = f.STATS["token_reads"]
    call("shots_list")
    assert f.STATS["token_reads"] == reads


# --- shot_update ---------------------------------------------------------------------------


def upd(changes, digest=None, key="1-1"):
    if digest is None:
        digest = call("shot_get", shot_key=key)["shot"]["digest"]
    return call("shot_update", shot_key=key, changes=changes, base_digest=digest)


def test_update_validation(project):
    cases = [{"start_prompt": "  "}, {"start_prompt": 5}, {"zzz": "x"}, {"video_prompt": "a\0b"},
             {"duration_s": True}, {"duration_s": 0}, {"duration_s": 121}, {"duration_s": 5.5},
             {"start_prompt": "я" * 20001}, "not a dict"]
    for changes in cases:
        assert upd(changes)["error"]["code"] == "invalid_argument", changes
    (project / "97_shots/video_model_caps.json").write_text(json.dumps({"supported_durations": [4, 5, 6]}))
    assert upd({"duration_s": 7})["error"]["code"] == "invalid_argument"
    doc = read_items(project)
    doc["items"] = [i for i in doc["items"] if not (i["scene_number"] == 2 and i["shot_type"] == "end")]
    write_items(project, doc)
    assert upd({"end_prompt": "x"}, key="2-3")["error"]["code"] == "invalid_argument"
    assert call("shot_update", shot_key="1-1", changes={}, base_digest="")["error"]["code"] == "invalid_argument"
    assert call("shot_update", shot_key="9-9", changes={"video_prompt": "x"}, base_digest="j1:x")["error"]["code"] == "not_found"


def test_update_success_keeps_other_fields(project):
    adopt()
    before = read_items(project)
    res = upd({"start_prompt": "a\r\nb", "video_prompt": "", "end_negative_prompt": "n2"})
    assert res["status"] == "success" and res["changed"] == ["start_prompt", "video_prompt", "end_negative_prompt"]
    after = read_items(project)
    assert after["seed"] == 7 and after["consistency_rules"] == ["a"]
    s_before, s_after = before["items"][0], after["items"][0]
    assert s_after["english_prompt"] == "a\nb" and s_after["video_prompt"] == ""
    assert {k: v for k, v in s_after.items() if k not in ("english_prompt", "video_prompt")} == \
        {k: v for k, v in s_before.items() if k not in ("english_prompt", "video_prompt")}
    assert after["items"][1]["video_prompt"] == "" and after["items"][1]["negative_prompt"] == "n2"
    assert after["items"][2:] == before["items"][2:]
    assert res["effects"]["stale_now"] == ["start", "end", "video"]
    assert "Картинка не обновится" in (res["effects"]["note"] or "")
    codes = {w["code"] for w in res["warnings"]}
    assert {"video_prompt_empty", "storyboard_rerun_overwrites"} <= codes
    assert res["digest"] == res["shot"]["digest"] != ""


def test_update_video_prompt_goes_to_start_only(project):
    assert upd({"video_prompt": "новое движение"})["status"] == "success"
    doc = read_items(project)
    assert doc["items"][0]["video_prompt"] == "новое движение" and doc["items"][1]["video_prompt"] == ""


def test_update_duration_both_elements(project):
    res = upd({"duration_s": 8})
    assert res["status"] == "success"
    doc = read_items(project)
    for el in doc["items"][:2]:
        assert (el["duration_s"], el["duration_source"], el["timing"], el["duration_requested_s"]) == (8, "manual", "00:08", 5)
    assert upd({"duration_s": 9})["status"] == "success"
    assert read_items(project)["items"][0]["duration_requested_s"] == 5  # один раз
    assert res["warnings"] and any(w["code"] == "duration_unchecked" for w in res["warnings"])


def test_update_marks_stale_immediately_and_logs(project):
    res = upd({"start_prompt": "Новый текст"})
    assert res["shot"]["stale_parts"] == ["start"]
    assert res["effects"]["stale_after_regeneration"] == ["end", "video"]
    log = [json.loads(x) for x in (project / ".studio/shot_edits.jsonl").read_text().splitlines()]
    assert log[0]["shot_key"] == "1-1"
    assert log[0]["fields"]["start_prompt"] == {"old": "prompt start 1-1", "new": "Новый текст"}
    assert res["shot"]["edits"][0]["fields"]["start_prompt"]["new"] == "Новый текст"


def test_update_no_change_does_not_write(project):
    card = call("shot_get", shot_key="1-1")["shot"]
    mtime = (project / "97_shots/shots.json").stat().st_mtime_ns
    res = upd({"start_prompt": "prompt start 1-1"}, card["digest"])
    assert res["changed"] == [] and (project / "97_shots/shots.json").stat().st_mtime_ns == mtime
    assert not (project / ".studio/shot_edits.jsonl").exists()


def test_update_conflict_fast_path(project):
    digest = call("shot_get", shot_key="1-1")["shot"]["digest"]
    doc = read_items(project)
    doc["items"][0]["english_prompt"] = "чужая правка"
    write_items(project, doc)
    res = upd({"start_prompt": "моя"}, digest)
    err = res["error"]
    assert err["code"] == "conflict" and err["details"]["current"]["start_prompt"] == "чужая правка"
    assert err["details"]["current_digest"] != digest
    assert read_items(project)["items"][0]["english_prompt"] == "чужая правка"


def test_update_conflict_race_under_lock(project, monkeypatch):
    s = shots()
    digest = call("shot_get", shot_key="1-1")["shot"]["digest"]
    real = s.capture_baseline_before_write

    def sneaky(pid, keys=None):  # между чтением и записью шот меняет другой писатель
        real(pid, keys)
        doc = read_items(project)
        doc["items"][0]["english_prompt"] = "гонка"
        write_items(project, doc)

    monkeypatch.setattr(s, "capture_baseline_before_write", sneaky)
    res = upd({"start_prompt": "моя"}, digest)
    assert res["error"]["code"] == "conflict"
    assert read_items(project)["items"][0]["english_prompt"] == "гонка"
    assert not list((project / "97_shots").glob("*.tmp"))


def test_update_keeps_foreign_field_changes_and_not_found_on_delete(project, monkeypatch):
    s = shots()
    digest = call("shot_get", shot_key="1-1")["shot"]["digest"]
    real = s.capture_baseline_before_write

    def pipeline_writes(pid, keys=None):
        real(pid, keys)
        doc = read_items(project)
        doc["items"][0]["image_path"] = "set-by-pipeline"
        doc["items"][0]["continuity_score"] = 9
        write_items(project, doc)

    monkeypatch.setattr(s, "capture_baseline_before_write", pipeline_writes)
    assert upd({"start_prompt": "моя"}, digest)["status"] == "success"
    first = read_items(project)["items"][0]
    assert first["english_prompt"] == "моя" and first["image_path"] == "set-by-pipeline" and first["continuity_score"] == 9

    digest = call("shot_get", shot_key="2-3")["shot"]["digest"]

    def deletes(pid, keys=None):
        doc = read_items(project)
        doc["items"] = [i for i in doc["items"] if i["scene_number"] != 2]
        write_items(project, doc)

    monkeypatch.setattr(s, "capture_baseline_before_write", deletes)
    assert upd({"start_prompt": "x"}, digest, key="2-3")["error"]["code"] == "not_found"
    assert all(i["scene_number"] != 2 for i in read_items(project)["items"])  # не воскрешён


def test_update_parallel_different_shots(project):
    d1 = call("shot_get", shot_key="1-1")["shot"]["digest"]
    d2 = call("shot_get", shot_key="2-3")["shot"]["digest"]
    out = {}

    def run(key, digest):
        out[key] = caps.invoke("shot_update", {"project_id": PID, "series_id": "main", "shot_key": key,
                                                "changes": {"start_prompt": "p" + key}, "base_digest": digest})

    threads = [threading.Thread(target=run, args=("1-1", d1)), threading.Thread(target=run, args=("2-3", d2))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert all(r["status"] == "success" for r in out.values())
    items = read_items(project)["items"]
    assert items[0]["english_prompt"] == "p1-1" and items[2]["english_prompt"] == "p2-3"


def test_update_locked_and_run_active(project, monkeypatch):
    a = art()
    monkeypatch.setattr(a, "_run_info", lambda pid: {"active": True, "current_steps": ["screenplay_shots_generator"]})
    assert upd({"start_prompt": "x"})["error"]["code"] == "locked"
    assert call("shot_get", shot_key="1-1")["shot"]["editable"]["locked"] is True
    monkeypatch.setattr(a, "_run_info", lambda pid: {"active": True, "current_steps": ["artist_batch_shots"]})
    res = upd({"start_prompt": "x"})
    assert res["status"] == "success" and any(w["code"] == "run_active" for w in res["warnings"])


def test_set_duration_helper(project):
    s = shots()
    res = s.set_duration(PID, "1-1", 6)
    assert res["changed"] == ["duration_s"] and read_items(project)["items"][0]["duration_s"] == 6
    with pytest.raises(Exception):
        s.set_duration(PID, "1-1", 0)


def test_write_failure_keeps_valid_json(project, monkeypatch):
    import StoryBookManager.core.file_manager as fm

    digest = call("shot_get", shot_key="1-1")["shot"]["digest"]
    real = json.dump

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(fm.json, "dump", boom)
    res = upd({"start_prompt": "x"}, digest)
    monkeypatch.setattr(fm.json, "dump", real)
    assert res["status"] == "error"
    assert read_items(project)["items"][0]["english_prompt"] == "prompt start 1-1"
    assert not list((project / "97_shots").glob("*.tmp"))


def test_journal_rotation(project):
    f = fpm()
    path = project / ".studio/shot_edits.jsonl"
    path.parent.mkdir()
    path.write_text("x" * (f.EDITS_MAX_BYTES + 10))
    f.append_edit(PID, "1-1", {"a": {"old": 1, "new": 2}})
    assert (project / ".studio/shot_edits.jsonl.1").is_file() and path.stat().st_size < 1000


def test_shot_files_and_keys(project):
    s = shots()
    assert s.shot_keys(PID) == ["1-1", "2-3"]
    files = s.shot_files(PID, "2-3")
    assert files["start"].name == "img_final_start_02_03.png" and files["video"].name == "video_final_02_03.mp4"
    assert s.shot_files(PID, "1-1")["start"].is_absolute()


# --- ревью: статус задачи, запись отпечатков, source ------------------------------------------


@pytest.mark.parametrize("status", ["submitting", "running"])
def test_active_job_generating_only_if_alive(project, monkeypatch, status):
    monkeypatch.setattr(art(), "_run_info", lambda pid: {"active": False})
    jobs_file(project, [job("1-1", status, "2026-01-01T00:00:00Z")])
    assert call("shot_get", shot_key="1-1")["shot"]["parts"]["video"]["state"] != "generating"
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    jobs_file(project, [job("1-1", status, fresh)])
    assert call("shot_get", shot_key="1-1")["shot"]["parts"]["video"]["state"] == "generating"
    monkeypatch.setattr(art(), "_run_info", lambda pid: {"active": True})
    jobs_file(project, [job("1-1", status, "2026-01-01T00:00:00Z")])
    assert call("shot_get", shot_key="1-1")["shot"]["parts"]["video"]["state"] == "generating"


def test_only_missing_skips_recorded_parts(project):
    mod = shots()
    ctx = mod._Ctx(PID)
    wanted = [("1-1", "start", "generated"), ("1-1", "end", "generated")]
    fpm().update_records(PID, lambda st: st.setdefault("1-1", {}).__setitem__("start", {"source": "adopted", "at": "x"}))
    assert mod._write_baselines(ctx, wanted, only_missing=True) == 1
    assert fpm().load_records(PID)["1-1"]["start"]["source"] == "adopted"


def test_set_baseline_record_source(project):
    adopt()
    rec = fpm().load_records(PID)["1-1"]["start"]
    shots().set_baseline_record(PID, "1-1", "start", rec)
    assert fpm().load_records(PID)["1-1"]["start"]["source"] == "restored"
    shots().set_baseline_record(PID, "1-1", "start", rec, source="adopted")
    assert fpm().load_records(PID)["1-1"]["start"]["source"] == "adopted"


def test_conflict_in_artifact_save_makes_no_records(project):
    content = {"items": [item(1, 1, "start"), item(1, 1, "end")]}
    res = call("artifact_save", key="shots", base_digest="sha256:wrong", content=content)
    assert res["error"]["code"] == "conflict"
    assert not (project / ".studio/shot_inputs.json").exists()


# --- референсы серии в shared/references ------------------------------------------------

def _move_ref(root, old_rel, name="hero"):
    """Референс переехал в shared/references; старый путь остался алиасом в каталоге."""
    from custom_tools.storybook import reference_catalog as catalog
    old = root.parents[1] / old_rel
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"ref-" + name.encode())
    new = Path(catalog.store_reference(root, old, "identity", name))
    assert not old.exists() and new.is_file()
    return new


def test_moved_reference_view_exists_and_fresh_after_move(project):
    old = project / "20_bible/references/characters/hero.png"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"ref-hero")
    doc = read_items(project)
    doc["items"][0]["reference_image_paths"] = ["/references/characters/hero.png"]
    write_items(project, doc)
    adopt()  # база до переноса
    from custom_tools.storybook import reference_catalog as catalog
    new = Path(catalog.store_reference(project, old, "identity", "hero"))
    shots()._CACHE.clear()
    card = call("shot_get", shot_key="1-1")["shot"]
    assert card["stale_parts"] == [] and card["parts"]["start"]["freshness"] == "fresh"


def test_moved_reference_content_change_still_stales(project):
    old = project.parents[1] / "20_bible/references/characters/hero.png"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"ref-hero")
    doc = read_items(project)
    doc["items"][0]["reference_image_paths"] = ["/references/characters/hero.png"]
    write_items(project, doc)
    from custom_tools.storybook import reference_catalog as catalog
    new = Path(catalog.store_reference(project, old, "identity", "hero"))
    adopt()
    new.write_bytes(b"changed bytes!")
    card = call("shot_get", shot_key="1-1")["shot"]
    assert card["stale_reasons"]["start"] == ["references_changed"]


def test_ref_view_resolves_moved_and_shared_paths(project):
    new = _move_ref(project, "20_bible/references/characters/a.png", "a")
    doc = read_items(project)
    doc["items"][0]["reference_image_paths"] = ["/references/characters/a.png", f"shared/references/characters/{new.name}",
                                                "/references/characters/missing.png"]
    write_items(project, doc)
    s = shots()
    s._CACHE.clear()
    ctx = s._Ctx(PID)
    view = s._ref_view(ctx, {"refs": doc["items"][0]["reference_image_paths"]})
    want = f"shared/references/characters/{new.name}"
    assert [v["path"] for v in view[:2]] == [want, want]
    assert [v["exists"] for v in view] == [True, True, False]
    # ключ отпечатка: у shared-пути sha (не зависит от папки категории), у старого алиаса путь из шота
    entries = ctx._ref_entries({"refs": [want, "/references/characters/a.png"], "bg": None}, None)
    assert entries[0] == ["sha:" + new.stem, entries[0][1]] and entries[0][1] and entries[1][0] == "20_bible/references/characters/a.png"
    assert entries[1][1] == entries[0][1]


def test_unbound_project_root_reference_does_not_exist(project):
    """Файл лежит в корне проекта, но не привязан к серии: генератор на нём упадёт, значит exists=false."""
    from custom_tools.storybook import reference_catalog as catalog
    bound = project.parents[1] / "20_bible/references/characters/b.png"
    bound.parent.mkdir(parents=True)
    bound.write_bytes(b"bound")
    catalog.store_reference(project, bound, "identity", "b")
    other = project.parents[1] / "20_bible/references/characters/other.png"
    other.write_bytes(b"other")
    s = shots()
    s._CACHE.clear()
    ctx = s._Ctx(PID)
    view = s._ref_view(ctx, {"refs": ["/references/characters/other.png", "/references/characters/b.png"]})
    assert [v["exists"] for v in view] == [False, True]


def test_ref_view_resolves_series_scoped_alias(project):
    """Единственный алиас записи лежит под _series/<sid>: шот со ссылкой /references/... всё равно находит файл."""
    new = _move_ref(project, f"20_bible/references/_series/{project.name}/characters/x.png", "x")
    s = shots()
    s._CACHE.clear()
    view = s._ref_view(s._Ctx(PID), {"refs": ["/references/characters/x.png"]})
    assert [v["exists"] for v in view] == [True]
    assert view[0]["path"] == f"shared/references/characters/{new.name}"
