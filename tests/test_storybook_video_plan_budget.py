"""Бюджет в generate_reference_video: хватает на один из двух референс-роликов."""

import json
from pathlib import Path

import pytest
from PIL import Image

from custom_tools.storybook import video_plan as vp
from custom_tools.storybook import video_generator_aitunnel_tool as generator
from custom_tools.storybook import video_run_config as cfg
from custom_tools.storybook.video_budget import BudgetExceededError
from custom_tools.storybook.video_generator_aitunnel_jobs import _ProviderJobStore

MODEL = "wan"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    monkeypatch.setenv("STORYBOOK_VIDEO_PRICING_PATH", str(tmp_path / "none.yaml"))
    cfg.bind_run_project(None)
    base = tmp_path / "plots/storybooks/test"
    (base / "97_shots").mkdir(parents=True)
    (base / ".studio").mkdir()
    (base / ".studio" / "run_settings.json").write_text(json.dumps({"budget_rub": 15}))
    Image.new("RGB", (32, 24), "red").save(base / "hero.png")
    yield base
    cfg.bind_run_project(None)


def test_budget_for_one_of_two_reference_videos(project, monkeypatch):
    base = project
    store = _ProviderJobStore(str(base / "97_shots/provider_jobs.json"), provider_name="routerai")
    items = [dict(scene_number=1, shot_number=n, duration_s=4, video_prompt=f"Run {n}",
                  reference_image_paths=[str(base / "hero.png")], video_path=str(base / f"{n}.mp4"))
             for n in (1, 2)]
    posts = []

    def post(url, **kwargs):
        posts.append(kwargs["json"])
        return type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: {"id": f"t{len(posts)}"}})()

    monkeypatch.setattr(vp.requests, "post", post)
    monkeypatch.setattr(generator, "_wait_for_video_completion_aitunnel",
                        lambda *a, **k: {"unsigned_urls": ["https://cdn.example/r.mp4"]})
    monkeypatch.setattr(generator, "_download_video_aitunnel", lambda url, path, headers: Path(path).write_bytes(b"v"))
    catalog = {MODEL: {"supports_image_references": True, "supported_durations": [4], "max_image_references": 10}}

    assert vp.generate_reference_video(items[0], "k", MODEL, None, store, catalog)["success"]
    with pytest.raises(BudgetExceededError) as exc:
        vp.generate_reference_video(items[1], "k", MODEL, None, store, catalog)
    assert exc.value.code == "budget_exceeded"
    assert len(posts) == 1
    assert (base / "96_video_contract/budget_report.json").is_file()
