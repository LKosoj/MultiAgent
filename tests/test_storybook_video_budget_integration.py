"""Бюджет в настоящем инструменте routerai: платные отправки без сети."""

import json

import pytest
from PIL import Image

from custom_tools.storybook import video_contract
from custom_tools.storybook import video_generator_aitunnel_tool as generator
from custom_tools.storybook import video_generator_common as common
from custom_tools.storybook import video_run_config as cfg
from custom_tools.storybook.video_generator_routerai_tool import video_generator_routerai_tool

BASE = "https://routerai.ru/api/v1"
MODEL = "google/veo-3.1-fast"


class Response:
    def __init__(self, payload=None, status=200, content=b"video"):
        self.status_code = status
        self.payload = payload
        self.content = content
        self.text = "test response"

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield self.content


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STORYBOOK_PROJECTS_DIR", raising=False)
    monkeypatch.setenv("ROUTERAI_API_KEY", "router-key")
    monkeypatch.setenv("ROUTERAI_VIDEO_MODEL", MODEL)
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    monkeypatch.setenv("STORYBOOK_VIDEO_PRICING_PATH", str(tmp_path / "none.yaml"))
    monkeypatch.delenv("ROUTERAI_API_BASE", raising=False)
    monkeypatch.delenv("ROUTERAI_RESOLUTION", raising=False)
    monkeypatch.setattr(common, "_ROUTERAI_MODELS_CACHE", None)
    monkeypatch.setattr(generator, "update_shots_with_descriptions", lambda *a, **kw: False)
    monkeypatch.setattr(generator.time, "sleep", lambda *a: None)
    monkeypatch.setattr(video_contract, "_active_video_tool_name", lambda: "video_generator_routerai_tool")
    monkeypatch.setattr(video_contract.shutil, "which", lambda name: f"/usr/bin/{name}")
    cfg.bind_run_project(None)
    root = tmp_path / "plots/storybooks/router-project"
    shots = root / "97_shots"
    shots.mkdir(parents=True)
    for name in ("start.png", "end.png"):
        Image.new("RGB", (1280, 720)).save(shots / name)
    item = {
        "scene_number": 1, "shot_number": 1, "shot_type": "start",
        "video_prompt": "The camera pans right", "timing": "00:00 - 00:06",
        "start_image": str(shots / "start.png"), "end_image": str(shots / "end.png"),
        "width": 1280, "height": 720, "video_path": str(shots / "clip.mp4"),
    }
    items = [dict(item, shot_number=n, video_path=str(shots / f"clip{n}.mp4"), video_prompt=f"Camera movement {n}")
             for n in (1, 2, 3)]
    (shots / "shots.json").write_text(json.dumps({"items": items}))
    record = {
        "id": MODEL,
        "architecture": {"output_modalities": ["video"], "input_modalities": ["text", "image"]},
        "supported_durations": [4, 6, 8], "supported_resolutions": ["720p"],
        "supported_aspect_ratios": ["16:9"], "supported_frame_images": ["first_frame", "last_frame"], "seed": True,
    }
    yield root, record
    cfg.bind_run_project(None)


def set_budget(root, value):
    (root / ".studio").mkdir(exist_ok=True)
    (root / ".studio" / "run_settings.json").write_text(json.dumps({"budget_rub": value}))


def install_network(monkeypatch, record, submits):
    def post(url, **kwargs):
        submits.append(kwargs["json"]["prompt"])
        return Response({"id": f"task-{len(submits)}", "status": "pending"}, 202)

    def get(url, **kwargs):
        if url == f"{BASE}/models":
            return Response({"data": [record]})
        if "/content" in url or url.endswith(".mp4"):
            return Response(content=b"mp4")
        return Response({"status": "completed", "unsigned_urls": [f"{BASE}/videos/x/content"], "usage": {"cost": 9.5}})

    monkeypatch.setattr(generator.requests, "post", post)
    monkeypatch.setattr(generator.requests, "get", get)


def run(**kwargs):
    return video_generator_routerai_tool(
        session_id="t", project_id="router-project", enable=True, language="en",
        skip_prompt_enhancement=True, max_concurrency=1, **kwargs)


def test_budget_for_one_of_three_stops_the_rest(project, monkeypatch):
    root, record = project
    set_budget(root, 15)
    submits = []
    install_network(monkeypatch, record, submits)
    result = run()
    assert len(submits) == 1
    assert result["status"] == "error"
    assert result["stats"]["budget_stopped"] is True
    codes = [r.get("error_code") for r in result["results"]]
    assert codes.count("budget_exceeded") == 2
    jobs = json.loads((root / "97_shots/provider_jobs.json").read_text())["jobs"]
    assert any(j.get("reserved_cost_rub") == 10.0 for j in jobs)
    assert (root / "96_video_contract/budget_report.json").is_file()
    assert len([p for p in (root / "97_shots").glob("clip*.mp4")]) == 1


def test_resume_with_bigger_budget_pays_only_for_remaining(project, monkeypatch):
    root, record = project
    set_budget(root, 15)
    submits = []
    install_network(monkeypatch, record, submits)
    run()
    assert len(submits) == 1
    set_budget(root, 1000)
    result = run()
    assert len(submits) == 3
    assert result["status"] == "success"


def test_zero_budget_behaves_as_before(project, monkeypatch):
    root, record = project
    submits = []
    install_network(monkeypatch, record, submits)
    result = run()
    assert len(submits) == 3 and result["status"] == "success"
    assert not (root / "96_video_contract/budget_report.json").exists()
    assert result["stats"].get("budget_stopped") in (None, False)


def test_unknown_price_with_budget_stops_before_any_post(project, monkeypatch):
    root, record = project
    monkeypatch.delenv("ROUTERAI_VIDEO_COST_RUB")
    set_budget(root, 100)
    submits = []
    install_network(monkeypatch, record, submits)
    result = run()
    assert submits == []
    assert {r.get("error_code") for r in result["results"]} == {"budget_price_unknown"}


def test_direct_call_binds_project_so_hash_uses_project_model(project, monkeypatch):
    root, record = project
    cfg.bind_run_project(None)
    (root / ".studio").mkdir(exist_ok=True)
    (root / ".studio" / "run_settings.json").write_text(json.dumps({"video": {"provider": "routerai", "resolution": "720p"}}))
    install_network(monkeypatch, record, [])
    run()
    assert cfg.bound_project() == "router-project"


def test_call_without_project_resets_stale_binding(project, monkeypatch):
    root, record = project
    cfg.bind_run_project("router-project")
    install_network(monkeypatch, record, [])
    video_generator_routerai_tool(session_id="t", project_id=None, enable=True, items=[], language="en",
                                  skip_prompt_enhancement=True)
    assert cfg.bound_project() is None
