"""R3-A: правка картинки ИИ (image_edit_preview / image_edit_start) без сети и платных вызовов."""

import importlib
import io
import json
import os
import sys
import time
import types

import pytest
from PIL import Image

from custom_tools.storybook.studio import capabilities as caps
from tests.test_storybook_studio_shots import PID, build
from tests.studio_run_fakes import make_series_project
from custom_tools.storybook.project_paths import series_scope

IE = "custom_tools.storybook.studio.image_edit"
PROMPT = "сделай небо розовым"


def rg():
    return importlib.import_module("custom_tools.storybook.studio.regeneration")


def png(color=(200, 10, 10)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 6), color).save(buf, format="PNG")
    return buf.getvalue()


def call(cap, **payload):
    return caps.invoke(cap, {"project_id": PID, "series_id": "main", **payload})


def bad(cap, **payload):
    r = call(cap, **payload)
    assert r["status"] == "error", r
    return r["error"]


def reason(err):
    return (err.get("details") or {}).get("reason")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    root = make_series_project(tmp_path, PID)
    build(root)
    (root / "50_images" / "page_001").mkdir(parents=True)
    (root / "50_images" / "page_001" / "a.png").write_bytes(png())
    (root / "50_images" / "page_001" / "b.png").write_bytes(png((1, 2, 3)))
    (root / "50_images" / "page_002").mkdir()
    (root / "50_images" / "page_002" / "c.jpg").write_bytes(png((9, 9, 9)))
    (root / "50_images" / "page_002" / "notes.txt").write_text("x")
    (root / "20_bible").mkdir()
    (root / "20_bible" / "ref.png").write_bytes(png())
    importlib.import_module("custom_tools.storybook.studio.shots")._CACHE.clear()
    importlib.import_module("custom_tools.storybook.studio._shots_fp")._STORE_CACHE.clear()
    with series_scope(PID, "main"):
        yield root


@pytest.fixture
def price(monkeypatch):
    """Цена картинки: по умолчанию неизвестна (как в боевом конфиге)."""
    box = {"value": None}
    cost = importlib.import_module("custom_tools.storybook.studio.cost_estimate")
    monkeypatch.setattr(cost, "_flat_price", lambda section, field: box["value"])
    return box


@pytest.fixture
def tool(monkeypatch):
    """Подмена инструмента правки и перевода. calls хранит аргументы, mode управляет исходом."""
    state = {"calls": [], "mode": "ok", "color": (0, 200, 0)}
    # настоящие utils и image_tools при импорте ходят в сеть (память агентов): подставляем лёгкие двойники
    fake_utils = types.ModuleType("utils")
    image_tools = types.ModuleType("custom_tools.image_tools")
    monkeypatch.setitem(sys.modules, "utils", fake_utils)
    monkeypatch.setitem(sys.modules, "custom_tools.image_tools", image_tools)

    def fake(prompt, image_paths, session_id, output_path=None, **kw):
        state["calls"].append({"prompt": prompt, "images": list(image_paths), "output": output_path})
        mode = state["mode"]
        if mode == "raise":
            raise RuntimeError("secret https://api.example/key=SECRET")
        if mode == "error_text":
            return "Ошибка: https://api.example/key=SECRET"
        if mode == "nofile":
            return output_path
        if mode == "broken":
            with open(output_path, "wb") as fh:
                fh.write(b"not an image")
            return output_path
        with open(output_path, "wb") as fh:
            fh.write(png(state["color"]))
        return output_path

    image_tools.edit_image_vse_tool = fake
    fake_utils._translate_texts_batch = lambda texts, lang: [f"EN:{t}" for t in texts]
    return state


def shot_path(root, part="start", scene=1, shot=1):
    return root / "97_shots" / f"scene_{scene:02d}_shot_{shot:02d}" / f"img_final_{part}_{scene:02d}_{shot:02d}.png"


def preview(**kw):
    r = call("image_edit_preview", **kw)
    assert r["status"] == "success", r
    return r


def start(**kw):
    kw.setdefault("prompt", PROMPT)
    return call("image_edit_start", **kw)


def go_shot(price_box=None, **kw):
    """Предпросмотр -> старт с подтверждением цены -> ждать задание."""
    target = {"shot_key": kw.pop("shot_key", "1-1"), "part": kw.pop("part", "start")}
    pv = preview(**target, **{k: v for k, v in kw.items() if k == "reference_paths"})
    args = {**target, "expected_plan_digest": pv["plan_digest"], **kw}
    if pv["estimate"]["unknown_cost"]:
        args.setdefault("confirm_unknown_cost", True)
    else:
        args.setdefault("confirm_cost_rub", pv["estimate"]["cost_rub"])
    return start(**args)


def wait(job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        st = caps.invoke("projects_job_status", {"job_id": job_id})
        if st["state"] != "running":
            return st
        time.sleep(0.02)
    raise AssertionError("задание не завершилось")


def lock_free(project):
    handle = rg().acquire_lock(PID)
    rg().release_lock(handle)


# --- реестр и аргументы -------------------------------------------------------------------


def test_registered():
    reg = caps.registry()
    assert reg["image_edit_preview"].access == "read" and reg["image_edit_preview"].category == "media"
    assert reg["image_edit_start"].access == "run" and reg["image_edit_start"].category == "media"
    for gone in ("labels_get", "run_settings_validate", "cost_estimate", "reports_summary", "reports_findings"):
        assert gone not in reg


def test_in_video_contract_actions():
    from custom_tools.storybook import video_contract
    actions = video_contract.storybook_workflow_actions()
    text = json.dumps(actions, ensure_ascii=False)
    assert "image_edit_preview" in text and "image_edit_start" in text


def test_invoke_rejects_unknown_and_missing(project):
    assert bad("image_edit_preview", bogus=1)["code"] == "invalid_argument"
    assert bad("image_edit_start", prompt=PROMPT)["code"] == "invalid_argument"  # нет expected_plan_digest


@pytest.mark.parametrize("kw", [
    {},
    {"shot_key": "1-1"},
    {"part": "start"},
    {"shot_key": "1-1", "part": "start", "path": "50_images/page_001/a.png"},
    {"shot_key": "1-1", "part": "video"},
    {"shot_key": "oops", "part": "start"},
    {"path": "50_images/page_002/notes.txt"},
    {"path": "97_shots/scene_01_shot_01/img_final_start_01_01.png"},
    {"path": "20_bible/ref.png"},
])
def test_preview_invalid_target(project, kw):
    assert bad("image_edit_preview", **kw)["code"] == "invalid_argument"


def test_preview_not_found(project):
    assert bad("image_edit_preview", shot_key="9-9", part="start")["code"] == "not_found"
    assert bad("image_edit_preview", path="50_images/page_001/none.png")["code"] == "not_found"
    shot_path(project, "end").unlink()
    assert bad("image_edit_preview", shot_key="1-1", part="end")["code"] == "not_found"


def test_preview_path_escape_and_symlink(project, tmp_path):
    assert bad("image_edit_preview", path="50_images/../97_shots/shots.json")["code"] == "forbidden_path"
    outside = tmp_path / "outside.png"
    outside.write_bytes(png())
    os.symlink(outside, project / "50_images" / "page_001" / "link.png")
    assert bad("image_edit_preview", path="50_images/page_001/link.png")["code"] == "forbidden_path"


def test_preview_gif_rejected(project):
    (project / "50_images" / "page_001" / "g.gif").write_bytes(b"GIF89a")
    assert bad("image_edit_preview", path="50_images/page_001/g.gif")["code"] == "invalid_argument"


def test_references_checks(project):
    base = {"path": "50_images/page_001/a.png"}
    assert bad("image_edit_preview", **base, reference_paths=["50_images/page_001/b.png"] * 4)["code"] == "invalid_argument"
    assert bad("image_edit_preview", **base, reference_paths=["../x.png"])["code"] == "forbidden_path"
    assert bad("image_edit_preview", **base, reference_paths=["50_images/page_002/notes.txt"])["code"] == "invalid_argument"
    assert bad("image_edit_preview", **base, reference_paths="b.png")["code"] == "invalid_argument"
    assert bad("image_edit_preview", **base, reference_paths=["50_images/page_001/none.png"])["code"] == "not_found"
    (project / ".studio").mkdir(exist_ok=True)
    (project / ".studio" / "x.png").write_bytes(png())
    assert bad("image_edit_preview", **base, reference_paths=[".studio/x.png"])["code"] == "invalid_argument"


def test_references_dedupe_and_target_dropped(project):
    r = preview(path="50_images/page_001/a.png",
                reference_paths=["50_images/page_001/a.png", "50_images/page_001/b.png", "50_images/page_001/b.png"])
    assert [x["path"] for x in r["references"]] == ["50_images/page_001/b.png"]


# --- предпросмотр: состав ответа и цена -----------------------------------------------------


def test_preview_shape_shot(project, price):
    r = preview(shot_key="1-1", part="start")
    assert r["target"]["kind"] == "shot" and r["target"]["shot_key"] == "1-1" and r["target"]["part"] == "start"
    assert r["target"]["path"].endswith("img_final_start_01_01.png") and r["target"]["size"] > 0
    assert r["limits"] == {"prompt_min": 5, "prompt_max": 2000, "references_max": 3,
                           "extensions": [".png", ".jpg", ".jpeg", ".webp"]}
    assert r["estimate"] == {"cost_rub": 0.0, "unknown_cost": True, "price_per_image_rub": None}
    assert r["can_start"] is True and len(r["plan_digest"]) == 64


def test_preview_shape_illustration(project, price):
    r = preview(path="50_images/page_002/c.jpg")
    assert r["target"]["kind"] == "illustration" and r["target"]["shot_key"] == "" and r["target"]["part"] == ""


def test_preview_known_price(project, price):
    price["value"] = 5.0
    r = preview(shot_key="1-1", part="end")
    assert r["estimate"] == {"cost_rub": 5.0, "unknown_cost": False, "price_per_image_rub": 5.0}


def test_digest_changes(project, price):
    path = "50_images/page_001/a.png"
    d1 = preview(path=path)["plan_digest"]
    assert preview(path=path)["plan_digest"] == d1
    assert preview(path=path, reference_paths=["50_images/page_001/b.png"])["plan_digest"] != d1
    price["value"] = 7.0
    d2 = preview(path=path)["plan_digest"]
    assert d2 != d1
    (project / path).write_bytes(png((5, 5, 5)) + b"x")
    assert preview(path=path)["plan_digest"] != d2


def test_preview_project_busy_and_pending(project, price):
    handle = rg().acquire_lock(PID)
    try:
        r = preview(shot_key="1-1", part="start")
        assert r["can_start"] is False and [p["code"] for p in r["problems"] if p["level"] == "error"] == ["project_busy"]
    finally:
        rg().release_lock(handle)
    assert preview(shot_key="1-1", part="start")["can_start"] is True


def test_preview_live_journal(project, price, monkeypatch):
    monkeypatch.setattr(rg(), "has_live_journal", lambda pid: True)
    r = preview(shot_key="1-1", part="start")
    assert "regeneration_pending" in [p["code"] for p in r["problems"]] and r["can_start"] is False


def test_preview_paid_job_warning(project, price, monkeypatch):
    monkeypatch.setattr(rg(), "pending_paid_jobs", lambda root, keys: [{"shot_key": "1-1", "status": "queued"}])
    start_ = preview(shot_key="1-1", part="start")
    assert ("warning", "paid_job_pending") in [(p["level"], p["code"]) for p in start_["problems"]]
    assert start_["can_start"] is True
    end = preview(shot_key="1-1", part="end")
    assert "paid_job_pending" not in [p["code"] for p in end["problems"]]


# --- старт: проверки ------------------------------------------------------------------------


def test_start_prompt_checks(project, price, tool):
    pv = preview(shot_key="1-1", part="start")
    base = {"shot_key": "1-1", "part": "start", "expected_plan_digest": pv["plan_digest"],
            "confirm_unknown_cost": True}
    for text in ("", "    ", "abcd", "  abcd  ", 5, None, "x" * 2001):
        assert bad("image_edit_start", prompt=text, **base)["code"] == "invalid_argument", text
    assert tool["calls"] == []


def test_start_plan_changed(project, price, tool):
    err = bad("image_edit_start", prompt=PROMPT, shot_key="1-1", part="start", expected_plan_digest="0" * 64,
              confirm_unknown_cost=True)
    assert err["code"] == "conflict" and reason(err) == "plan_changed"
    assert tool["calls"] == []


def test_start_cost_confirmation(project, price, tool):
    price["value"] = 5.0
    pv = preview(shot_key="1-1", part="start")
    base = {"shot_key": "1-1", "part": "start", "expected_plan_digest": pv["plan_digest"]}
    err = bad("image_edit_start", prompt=PROMPT, **base)
    assert reason(err) == "cost_confirmation_required" and err["details"]["cost_rub"] == 5.0
    err = bad("image_edit_start", prompt=PROMPT, confirm_cost_rub=4.0, **base)
    assert reason(err) == "cost_changed"
    err = bad("image_edit_start", prompt=PROMPT, confirm_cost_rub="много", **base)
    assert reason(err) == "cost_changed"
    assert tool["calls"] == []
    ok = start(confirm_cost_rub=4.6, **base)  # допуск 0,5 рубля
    assert ok["status"] == "success"
    wait(ok["job_id"])


def test_start_unknown_cost_confirmation(project, price, tool):
    pv = preview(shot_key="1-1", part="start")
    base = {"shot_key": "1-1", "part": "start", "expected_plan_digest": pv["plan_digest"]}
    err = bad("image_edit_start", prompt=PROMPT, **base)
    assert reason(err) == "cost_confirmation_required" and err["details"]["unknown_cost"] is True
    assert bad("image_edit_start", prompt=PROMPT, confirm_unknown_cost=False, **base)["code"] == "conflict"
    assert tool["calls"] == []


def test_start_free_price_needs_no_confirmation(project, price, tool):
    price["value"] = 0.0
    pv = preview(shot_key="1-1", part="start")
    r = start(shot_key="1-1", part="start", expected_plan_digest=pv["plan_digest"])
    assert r["status"] == "success"
    assert wait(r["job_id"])["state"] == "done"


# --- успех ----------------------------------------------------------------------------------


def test_success_shot(project, price, tool):
    from custom_tools.storybook.studio import shot_versions as sv
    original = shot_path(project).read_bytes()
    assert call("shots_adopt_baseline")["status"] == "success"
    assert call("review_set_status", keys=["1-1"], status="approved")["status"] == "success"
    before = call("shot_get", shot_key="1-1")["shot"]
    assert before["parts"]["start"]["freshness"] == "fresh"
    refs = ["50_images/page_001/a.png", "50_images/page_001/b.png"]
    r = go_shot(reference_paths=refs, surface="tk")
    job = wait(r["job_id"])
    assert job["state"] == "done", job
    res = job["result"]
    assert res["shot_key"] == "1-1" and res["backup"] is None and res["version_id"]
    assert res["path"].endswith("img_final_start_01_01.png") and res["size"] == shot_path(project).stat().st_size
    # файл заменён, прежний лежит в версии
    new = shot_path(project).read_bytes()
    assert new != original and Image.open(io.BytesIO(new)).size == (8, 6)
    versions = call("versions_list", key="1-1")
    titles = json.dumps(versions, ensure_ascii=False)
    assert "Перед правкой картинки ИИ" in titles
    assert sv.REASON_TITLES["image_edit"] == "Перед правкой картинки ИИ"
    vdirs = list((project / "97_shots" / "_versions").rglob("img_final_start_01_01.png"))
    assert len(vdirs) == 1 and vdirs[0].read_bytes() == original
    # инструмент получил перевод и порядок файлов: цель, потом образцы; пишет не в цель
    c = tool["calls"][0]
    assert c["prompt"] == "EN:" + PROMPT
    assert c["images"][0] == str(shot_path(project).resolve()) or c["images"][0] == str(shot_path(project))
    assert [os.path.basename(p) for p in c["images"][1:]] == ["a.png", "b.png"]
    assert c["output"] != c["images"][0] and ".studio/tmp/" in c["output"]
    assert not list((project / ".studio" / "tmp").glob("*"))
    # ревью сброшено, картинка не «устарела» сама по себе
    assert call("review_get", key="1-1")["shots"]["1-1"]["status"] == "pending"
    after = call("shot_get", shot_key="1-1")["shot"]
    assert after["parts"]["start"]["freshness"] == "fresh"
    lock_free(project)


def test_success_shot_marks_video_per_usual_rules(project, price, tool):
    """Фиксируем текущее поведение: видео шота после правки стартовой картинки."""
    call("shots_adopt_baseline")
    assert call("shot_get", shot_key="1-1")["shot"]["parts"]["video"]["freshness"] == "fresh"
    wait(go_shot()["job_id"])
    # правка стартовой картинки сама видео устаревшим не делает: отпечаток видео от картинки не зависит
    assert call("shot_get", shot_key="1-1")["shot"]["parts"]["video"]["freshness"] == "fresh"


def test_success_end_part_untouched_start(project, price, tool):
    start_before = shot_path(project).read_bytes()
    job = wait(go_shot(part="end")["job_id"])
    assert job["state"] == "done"
    assert shot_path(project).read_bytes() == start_before
    assert shot_path(project, "end").read_bytes() != b"E11"


def test_success_illustration(project, price, tool):
    path = "50_images/page_001/a.png"
    old = (project / path).read_bytes()
    before = call("media_inventory", types=["illustration"], probe_limit=0)
    old_ts = {i["path"]: i["modified_ts"] for i in before["items"]}[path]
    pv = preview(path=path)
    r = start(path=path, expected_plan_digest=pv["plan_digest"], confirm_unknown_cost=True)
    job = wait(r["job_id"])
    assert job["state"] == "done", job
    res = job["result"]
    assert res["shot_key"] is None and res["version_id"] is None
    assert res["backup"].startswith(".studio/image_edit_backups/") and res["backup"].endswith("_a.png")
    assert (project / res["backup"]).read_bytes() == old
    assert (project / path).read_bytes() != old
    inv = call("media_inventory", types=["illustration"], probe_limit=0)
    names = [i["path"] for i in inv["items"]]
    assert sorted(names) == ["50_images/page_001/a.png", "50_images/page_001/b.png", "50_images/page_002/c.jpg"]
    new_ts = {i["path"]: i["modified_ts"] for i in inv["items"]}[path]
    assert new_ts > old_ts
    all_inv = call("media_inventory", limit=2000, probe_limit=0)
    assert not [i for i in all_inv["items"] if ".studio" in i["path"]]
    lock_free(project)


def test_success_jpg_keeps_extension(project, price, tool):
    path = "50_images/page_002/c.jpg"
    pv = preview(path=path)
    job = wait(start(path=path, expected_plan_digest=pv["plan_digest"], confirm_unknown_cost=True)["job_id"])
    assert job["state"] == "done" and (project / path).is_file()
    assert tool["calls"][0]["output"].endswith(".jpg")


# --- сбои инструмента -----------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["raise", "error_text", "nofile", "broken"])
def test_tool_failure_keeps_target(project, price, tool, mode):
    tool["mode"] = mode
    original = shot_path(project).read_bytes()
    job = wait(go_shot()["job_id"])
    assert job["state"] == "error" and job["error"]["code"] == "unavailable"
    assert job["error"]["message"] == "Не удалось исправить картинку. Прежняя осталась на месте"
    assert "SECRET" not in json.dumps(job, ensure_ascii=False) and "example" not in json.dumps(job)
    assert shot_path(project).read_bytes() == original
    # копия сделана до вызова инструмента и осталась
    assert list((project / "97_shots" / "_versions").rglob("img_final_start_01_01.png"))
    assert not list((project / ".studio" / "tmp").glob("*"))
    lock_free(project)


def test_tool_failure_illustration_keeps_backup(project, price, tool):
    tool["mode"] = "raise"
    path = "50_images/page_001/b.png"
    old = (project / path).read_bytes()
    pv = preview(path=path)
    job = wait(start(path=path, expected_plan_digest=pv["plan_digest"], confirm_unknown_cost=True)["job_id"])
    assert job["state"] == "error"
    assert (project / path).read_bytes() == old
    assert len(list((project / ".studio" / "image_edit_backups").glob("*_b.png"))) == 1
    lock_free(project)


def test_translation_failure_not_fatal(project, price, tool, monkeypatch):
    import utils  # двойник из фикстуры tool

    def boom(texts, lang):
        raise RuntimeError("no net")

    utils._translate_texts_batch = boom
    job = wait(go_shot()["job_id"])
    assert job["state"] == "done" and tool["calls"][0]["prompt"] == PROMPT


def test_review_not_reset_on_failure(project, price, tool):
    call("review_set_status", keys=["1-1"], status="approved")
    tool["mode"] = "raise"
    wait(go_shot()["job_id"])
    assert call("review_get", key="1-1")["shots"]["1-1"]["status"] == "approved"


# --- замок и параллельность -----------------------------------------------------------------


def test_locked_project_start(project, price, tool):
    pv = preview(shot_key="1-1", part="start")
    handle = rg().acquire_lock(PID)
    try:
        err = bad("image_edit_start", prompt=PROMPT, shot_key="1-1", part="start",
                  expected_plan_digest=pv["plan_digest"], confirm_unknown_cost=True)
        assert err["code"] == "locked"
    finally:
        rg().release_lock(handle)
    assert tool["calls"] == []
    jobs_dir = project.parents[2] / ".studio_jobs"
    assert not jobs_dir.exists() or not list(jobs_dir.glob("*.json"))


def test_second_start_while_running(project, price, tool, monkeypatch):
    import threading
    gate = threading.Event()
    image_tools = sys.modules["custom_tools.image_tools"]
    real = image_tools.edit_image_vse_tool

    def slow(prompt, image_paths, session_id, output_path=None, **kw):
        gate.wait(5)
        return real(prompt, image_paths, session_id, output_path)

    monkeypatch.setattr(image_tools, "edit_image_vse_tool", slow)
    pv = preview(path="50_images/page_001/a.png")
    first = start(path="50_images/page_001/a.png", expected_plan_digest=pv["plan_digest"], confirm_unknown_cost=True)
    assert first["status"] == "success"
    try:
        pv2 = call("image_edit_preview", path="50_images/page_001/b.png")
        assert [p["code"] for p in pv2["problems"] if p["level"] == "error"] == ["edit_running"]
        err = bad("image_edit_start", prompt=PROMPT, path="50_images/page_001/b.png",
                  expected_plan_digest=pv2["plan_digest"], confirm_unknown_cost=True)
        assert err["code"] == "conflict" and reason(err) == "edit_running"
        # прогон и перегенерация тоже получают locked, пока замок у правки
        assert bad("versions_restore", key="1-1", version_id="v1")["code"] in ("locked", "not_found")
    finally:
        gate.set()
    assert wait(first["job_id"])["state"] == "done"
    lock_free(project)
    assert preview(path="50_images/page_001/b.png")["can_start"] is True


def test_live_journal_blocks_start(project, price, tool, monkeypatch):
    pv = preview(shot_key="1-1", part="start")
    monkeypatch.setattr(rg(), "has_live_journal", lambda pid: True)
    err = bad("image_edit_start", prompt=PROMPT, shot_key="1-1", part="start",
              expected_plan_digest=pv["plan_digest"], confirm_unknown_cost=True)
    assert err["code"] == "conflict"
    # digest не зависит от журнала, поэтому ответ именно про журнал
    assert reason(err) == "regeneration_pending"


def test_file_changed_after_preview(project, price, tool):
    pv = preview(shot_key="1-1", part="start")
    shot_path(project).write_bytes(b"changed!!")
    err = bad("image_edit_start", prompt=PROMPT, shot_key="1-1", part="start",
              expected_plan_digest=pv["plan_digest"], confirm_unknown_cost=True)
    assert reason(err) == "plan_changed"
    lock_free(project)


def test_target_vanishes_before_work(project, price, tool, monkeypatch):
    """Файл пропал между стартом и работой: ошибка not_found, замок отпущен."""
    jobs = importlib.import_module("custom_tools.storybook.studio.jobs")
    real = jobs.start_job

    def sabotage(root, kind, pid, func):
        shot_path(project).unlink()
        return real(root, kind, pid, func)

    monkeypatch.setattr(jobs, "start_job", sabotage)
    job = wait(go_shot()["job_id"])
    assert job["state"] == "error" and job["error"]["code"] == "not_found"
    lock_free(project)


def test_start_job_failure_releases_lock(project, price, tool, monkeypatch):
    jobs = importlib.import_module("custom_tools.storybook.studio.jobs")

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(jobs, "start_job", boom)
    pv = preview(shot_key="1-1", part="start")
    r = start(shot_key="1-1", part="start", expected_plan_digest=pv["plan_digest"], confirm_unknown_cost=True)
    assert r["status"] == "error" and r["error"]["code"] == "internal"
    lock_free(project)


def test_atomic_replace_never_exposes_partial(project, price, tool):
    """Пока инструмент пишет, цель остаётся прежней; на место попадает только целый файл."""
    seen = {}
    image_tools = sys.modules["custom_tools.image_tools"]
    real = image_tools.edit_image_vse_tool

    def spy(prompt, image_paths, session_id, output_path=None, **kw):
        seen["target_during"] = shot_path(project).read_bytes()
        return real(prompt, image_paths, session_id, output_path)

    image_tools.edit_image_vse_tool = spy
    try:
        original = shot_path(project).read_bytes()
        wait(go_shot()["job_id"])
    finally:
        image_tools.edit_image_vse_tool = real
    assert seen["target_during"] == original
    assert shot_path(project).read_bytes() != original


def test_restore_returns_previous_picture(project, price, tool):
    original = shot_path(project).read_bytes()
    res = wait(go_shot()["job_id"])["result"]
    r = call("versions_restore", key="1-1", version_id=res["version_id"], parts=["start"])
    assert r["status"] == "success", r
    assert shot_path(project).read_bytes() == original


# --- ревью R3-A: замок, NaN, дубль версии, гонки, предупреждения ---------------------------


def test_project_dir_error_in_work_releases_lock(project, price, tool, monkeypatch):
    """project_dir падает уже в фоновой работе: замок всё равно отпускается."""
    mod = importlib.import_module(IE)
    jobs = importlib.import_module("custom_tools.storybook.studio.jobs")
    real_start, real_dir = jobs.start_job, mod.project_dir

    def boom(pid, **kw):
        from custom_tools.storybook.studio.errors import StudioError
        raise StudioError("not_found", "проект пропал")

    def arm(root, kind, pid, func):
        monkeypatch.setattr(mod, "project_dir", boom)
        return real_start(root, kind, pid, func)

    released = []
    real_release = rg().release_lock
    monkeypatch.setattr(rg(), "release_lock", lambda h: (released.append(h), real_release(h)))
    monkeypatch.setattr(jobs, "start_job", arm)
    job = wait(go_shot()["job_id"])
    monkeypatch.setattr(mod, "project_dir", real_dir)
    assert job["state"] == "error"
    assert len(released) == 1  # без явного release замок ушёл бы только вместе со сборкой мусора
    lock_free(project)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_start_rejects_non_finite_confirm(project, price, tool, bad_value):
    price["value"] = 5.0
    pv = preview(shot_key="1-1", part="start")
    err = bad("image_edit_start", prompt=PROMPT, shot_key="1-1", part="start",
              expected_plan_digest=pv["plan_digest"], confirm_cost_rub=bad_value)
    assert err["code"] == "invalid_argument"
    assert tool["calls"] == []
    lock_free(project)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf")])
def test_regenerate_start_rejects_non_finite_confirm(project, bad_value):
    err = bad("regenerate_start", what="images", launcher="backend", expected_plan_digest="x",
              shot_keys=["1-1"], confirm_cost_rub=bad_value)
    assert err["code"] == "invalid_argument"


def test_unknown_cost_message_is_own_text(project, price, tool):
    pv = preview(shot_key="1-1", part="start")
    err = bad("image_edit_start", prompt=PROMPT, shot_key="1-1", part="start",
              expected_plan_digest=pv["plan_digest"])
    assert err["message"] == "Подтвердите правку без оценки цены"
    assert "Стоимость изменилась" not in err["message"]


def test_duplicate_snapshot_returns_existing_version(project, price, tool):
    tool["mode"] = "raise"
    wait(go_shot()["job_id"])  # копия прежней картинки осталась, файл не менялся
    first = [v for v in importlib.import_module("custom_tools.storybook.studio.shot_versions")
             .iter_versions(PID, "1-1") if "start" in v["meta"]["parts"]]
    assert len(first) == 1
    tool["mode"] = "ok"
    res = wait(go_shot()["job_id"])["result"]  # та же картинка: новая копия не нужна
    assert res["version_id"] == first[0]["id"]
    assert call("versions_restore", key="1-1", version_id=res["version_id"], parts=["start"])["status"] == "success"


def _link_shots(project):
    doc = json.loads((project / "97_shots" / "shots.json").read_text(encoding="utf-8"))
    prev_end = next(i for i in doc["items"] if i["scene_number"] == 1 and i["shot_type"] == "end")["output_path"]
    for i in doc["items"]:
        if i["scene_number"] == 2 and i["shot_type"] == "start":
            i["copy_from_previous_end"] = True
            i["source_end_path"] = prev_end
    (project / "97_shots" / "shots.json").write_text(json.dumps(doc), encoding="utf-8")
    importlib.import_module("custom_tools.storybook.studio.shots")._CACHE.clear()


def test_linked_part_warnings(project, price):
    _link_shots(project)
    end = preview(shot_key="1-1", part="end")
    linked = [p for p in end["problems"] if p["code"] == "linked_part"]
    assert linked and linked[0]["level"] == "warning" and "2-3" in linked[0]["message"]
    assert end["can_start"] is True
    copy = preview(shot_key="2-3", part="start")
    assert "linked_part" in [p["code"] for p in copy["problems"]]
    assert "linked_part" not in [p["code"] for p in preview(shot_key="1-1", part="start")["problems"]]


def test_studio_path_rejected(project):
    (project / ".studio" / "image_edit_backups").mkdir(parents=True)
    (project / ".studio" / "image_edit_backups" / "x.png").write_bytes(png())
    assert bad("image_edit_preview", path=".studio/image_edit_backups/x.png")["code"] == "invalid_argument"


def test_symlink_inside_project_outside_illustrations(project):
    os.symlink(project / "20_bible" / "ref.png", project / "50_images" / "page_001" / "inner.png")
    assert bad("image_edit_preview", path="50_images/page_001/inner.png")["code"] == "invalid_argument"


def test_two_simultaneous_starts(project, price, tool):
    import threading
    gate = threading.Event()
    image_tools = sys.modules["custom_tools.image_tools"]
    real = image_tools.edit_image_vse_tool

    def slow(prompt, image_paths, session_id, output_path=None, **kw):
        gate.wait(5)
        return real(prompt, image_paths, session_id, output_path)

    image_tools.edit_image_vse_tool = slow
    path = "50_images/page_001/a.png"
    pv = preview(path=path)
    args = dict(path=path, expected_plan_digest=pv["plan_digest"], confirm_unknown_cost=True)
    barrier = threading.Barrier(2)
    results = []

    def runner():
        barrier.wait()
        results.append(start(**args))

    threads = [threading.Thread(target=runner) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    gate.set()
    ok = [r for r in results if r["status"] == "success"]
    failed = [r for r in results if r["status"] == "error"]
    assert len(ok) == 1 and len(failed) == 1, results
    err = failed[0]["error"]
    assert err["code"] == "locked" or (err["code"] == "conflict" and reason(err) in ("edit_running", "plan_changed"))
    assert wait(ok[0]["job_id"])["state"] == "done"
    assert len(tool["calls"]) == 1
    lock_free(project)


def test_regenerate_start_rejects_bool_confirm(project):
    err = bad("regenerate_start", what="images", launcher="backend", expected_plan_digest="x",
              shot_keys=["1-1"], confirm_cost_rub=True)
    assert err["code"] == "invalid_argument"
