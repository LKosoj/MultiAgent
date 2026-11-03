"""Общие помощники тестов болванки (Ф11): проект-образец и подделки соседей. Без Blender и сети."""

import importlib
import json
import os
import shutil
from pathlib import Path

from custom_tools.storybook.studio import _blockout_state as S

PID = "demo"
PKG = "custom_tools.storybook.studio"
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")

# сцена 1: шоты 1 и 2 в одной цепочке; сцена 2: шот 1 в своей
LAYOUT = {"sc01_ch01": (1, [1, 2]), "sc02_ch01": (2, [1])}


def mod(name):
    return importlib.import_module(f"{PKG}.{name}")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def shot_item(scene, shot, phase, duration=5, **extra):
    base = {
        "scene_number": scene, "shot_number": shot, "shot_type": phase, "timing": "00:05",
        "english_prompt": f"prompt {phase} {scene}-{shot}", "negative_prompt": "neg",
        "video_prompt": "move" if phase == "start" else "",
        "output_path": f"plots/storybooks/{PID}/97_shots/scene_{scene:02d}_shot_{shot:02d}/"
                       f"img_final_{phase}_{scene:02d}_{shot:02d}.png",
        "image_path": None, "reference_image_paths": [], "width": 64, "height": 64, "seed": 1,
    }
    if phase == "start":
        base["duration_s"] = duration
    base.update(extra)
    return base


def chain_spec(chain_id):
    return {"chain_id": chain_id, "camera": {"lens_mm": 35}, "objects": [{"instance_id": "hero"}]}


def manifest_for(chain_id, **over):
    data = {"rendered_at": "2026-11-01T10:00:00Z", "fps": 24, "resolution": [1280, 720], "duration_s": 5,
            "spec_hash": S.compute_spec_hash(chain_spec(chain_id)), "blender_version": "4.5",
            "warnings": [], "junction_with_prev": None, "junction_with_next": None, "p10_acknowledged": None}
    data.update(over)
    return data


def build_project(root: Path, *, manifests=True, brief=None, built=True, shots=True) -> Path:
    """Проект-образец. Все манифесты свежие, картинок кадров нет (P10 выключен)."""
    root.mkdir(parents=True, exist_ok=True)
    items = []
    if shots:
        for chain_id, (scene, numbers) in LAYOUT.items():
            for n in numbers:
                items += [shot_item(scene, n, "start"), shot_item(scene, n, "end")]
    write_json(root / "97_shots" / "shots.json", {"seed": 7, "items": items})
    write_json(root / "00_brief.json", brief if brief is not None else {"blockout_fps": 24, "blockout_resolution": "1280x720"})
    if built:
        chains = [{"chain_id": cid, "scene_number": scene,
                   "shots": [{"shot_number": n, "duration_s": 5, "t_start": 5.0 * i} for i, n in enumerate(nums)],
                   "total_duration_s": 5 * len(nums)} for cid, (scene, nums) in LAYOUT.items()]
        write_json(root / "93_blockout" / "chains.json", {"chains": chains})
        write_json(root / "93_blockout" / "scene_spec.json",
                   {"project_id": PID, "fps": 24, "resolution": [1280, 720], "chains": [chain_spec(c) for c in LAYOUT]})
        write_json(root / "93_blockout" / "report.json", {})
        if manifests:
            for cid, (scene, nums) in LAYOUT.items():
                for n in nums:
                    write_json(root / "93_blockout" / S.shot_dir_name(scene, n) / "manifest.json", manifest_for(cid))
    return root


def add_final_images(root: Path, scene: int, shot: int, mtime: float) -> list[Path]:
    folder = root / "97_shots" / S.shot_dir_name(scene, shot)
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for part in ("start", "end"):
        path = folder / f"img_final_{part}_{scene:02d}_{shot:02d}.png"
        path.write_bytes(PNG)
        os.utime(path, (mtime, mtime))
        paths.append(path)
    return paths


def snapshot_files(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class FakeRuns:
    """Подмена runs.run_plan_get / run_start / active_run_info: запоминает вызовы."""

    def __init__(self, monkeypatch, *, codes=(), active=False, steps=None, estimate=None, digest="d1"):
        self.runs = mod("runs")
        self.plan_calls, self.start_calls = [], []
        self.codes, self.digest, self.estimate = list(codes), digest, estimate
        self.info = {"active": active, "current_steps": steps}
        monkeypatch.setattr(self.runs, "run_plan_get", self._plan)
        monkeypatch.setattr(self.runs, "run_start", self._start)
        monkeypatch.setattr(self.runs, "active_run_info", lambda project_id: dict(self.info))

    def _plan(self, project_id, mode, **kw):
        self.plan_calls.append({"mode": mode, **kw})
        problems = [{"level": "error", "code": c, "message": c, "step_id": None, "artifact": None} for c in self.codes]
        return {"status": "success", "plan": {
            "mode": mode, "problems": problems, "can_start": not self.codes, "plan_digest": self.digest,
            "estimate": self.estimate, "steps_to_run": [kw.get("step_id")]}}

    def _start(self, project_id, mode, **kw):
        self.start_calls.append({"mode": mode, **kw})
        return {"status": "success", "run_id": "r1", "workflow_id": "w1", "plan": {"plan_digest": self.digest}}

    @property
    def probe_calls(self):
        return [c for c in self.plan_calls if c["mode"] == "single_step" and c.get("inputs") is None]


def fake_tools(monkeypatch, *, blender=True, ffmpeg=True):
    contract = importlib.import_module("custom_tools.storybook.video_contract")
    monkeypatch.setattr(contract, "_probe_blockout_blender_readiness",
                        lambda: {"available": blender, "version": "4.5" if blender else None,
                                 "message": "ok" if blender else "blender binary not found"})
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: ("/usr/bin/ffmpeg" if ffmpeg else None)
                        if name == "ffmpeg" else real(name, *a, **k))


def hide_review(monkeypatch):
    """Прячет review (Ф7): так проверяется поведение, когда хука перегенерации нет."""
    import sys
    monkeypatch.setitem(sys.modules, f"{PKG}.review", None)
    mod("blockout").reset_caches()
