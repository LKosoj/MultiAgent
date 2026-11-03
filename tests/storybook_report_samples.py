"""Образцы отчётов пайплайна для тестов reports (без префикса test_, pytest не собирает).

Формы взяты из реальных файлов plots/storybooks/platonov_20261024 и
/test/projects/project-1; для blockout, провальных final_review/render_report
реальных образцов нет — они построены по коду писателей
(blockout_renderer.py, montage_assembler.py).
"""

from __future__ import annotations

import json
from pathlib import Path

FILES = {
    "preflight": "96_video_contract/provider_menu_summary.json",
    "delivery_promise": "96_video_contract/delivery_promise.json",
    "decision_log": "96_video_contract/decision_log.json",
    "continuity_images": "97_shots/continuity_images_review.json",
    "continuity_videos": "97_shots/continuity_videos_review.json",
    "shots_prompt_qa": "97_shots/shots_prompt_qa_report.json",
    "blockout": "93_blockout/report.json",
    "chains": "93_blockout/chains.json",
    "render_report": "99_final/render_report.json",
    "final_review": "99_final/final_review.json",
    "provider_jobs": "97_shots/provider_jobs.json",
    "quality_review": "quality_review.json",
    "video_plan": "97_shots/video_plan.json",
}


def write_json(project: Path, name: str, data) -> Path:
    path = project / FILES.get(name, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def preflight(**over) -> dict:
    """Копия структуры /test/projects/project-1 (все возможности есть, шотов ещё нет)."""
    data = {
        "status": "success", "provider": "aitunnel", "generation_enabled": True,
        "items_total": 2, "expected_video_count": 2, "shots_exists": True, "shots_error": None,
        "capabilities": {"image": True, "video": True, "audio": True, "render": True},
        "capability_details": {"providers_considered": [
            {"name": "aitunnel", "configured": True, "required_env": ["AITUNNEL_API_KEY", "AITUNNEL_VIDEO_MODEL"]},
            {"name": "veo", "configured": False, "required_env": ["GEMINI_API_KEY"]},
        ]},
        "blockout": {"enabled": False, "use_reference": False,
                     "reference_forecast": {"eligible": 0, "not_eligible": 0}},
    }
    data.update(over)
    return data


def delivery_promise(**over) -> dict:
    data = {"status": "success", "will_generate_video": True, "expected_video_count": 3,
            "blocking_reasons": [], "provider": "aitunnel"}
    data.update(over)
    return data


def decision_log(**over) -> dict:
    data = {"status": "success", "approved": True, "final_video_status": "ready", "decisions": [
        {"name": "video_provider", "selected": "aitunnel", "reason": "configured", "confidence": 0.9},
    ]}
    data.update(over)
    return data


def continuity(records: list[dict]) -> dict:
    """Файл ревью: словарь tasks, записи добавляются в порядке списка."""
    return {"tasks": {f"hash{i}": rec for i, rec in enumerate(records)}}


def continuity_record(rid="2-2|2-3", passed=False, version=8, violations=(), uncertain=(),
                      dismissed=(), media=None) -> dict:
    return {"id": rid, "passed": passed, "review_version": version, "state_hash": "s",
            "media_hashes": media or {},
            "verdicts": [{"passed": passed, "violations": list(violations),
                          "uncertain": list(uncertain), "dismissed": list(dismissed)}],
            "session_id": "s"}


def violation(frame_key="2:2:end", observed="Герой без шапки", required="Герой в шапке", entity="hero") -> dict:
    return {"frame_key": frame_key, "entity_id": entity, "observed": observed, "required": required}


def provider_job(shot="2-3", status="downloaded", updated="2026-10-27T10:00:00Z", error=None,
                 cost=None, currency=None, **over) -> dict:
    job = {"shot_key": shot, "status": status, "error": error, "cost": cost, "currency": currency,
           "provider": "routerai", "model": "m", "output_path": f"video_units/{shot}_{shot}.mp4",
           "timestamps": {"created_at": "2026-10-27T09:00:00Z", "updated_at": updated}}
    job.update(over)
    return job


def provider_jobs(jobs: list[dict]) -> dict:
    return {"version": 2, "jobs": jobs}


def qa_report(**over) -> dict:
    data = {"scenes_processed": 3, "repairs_suggested": 4, "repairs_applied": 3, "repairs_rejected": 1,
            "rejected": [], "items_total": 10, "missing_shots": [], "rendered_image_findings": [],
            "global_rejected": []}
    data.update(over)
    return data


def blockout_report(checks: list, section="blockout_renderer", status="ok") -> dict:
    return {section: {"status": status, "checks": checks}}


def render_report(**over) -> dict:
    data = {"render_result": {"returncode": 0, "cached": False}, "errors": [], "warnings": [],
            "ffmpeg_available": True, "ffprobe_available": True, "clips_actual_vs_planned": []}
    data.update(over)
    return data


def final_review(**over) -> dict:
    data = {"passed": True, "checks": {"container": {"passed": True}}, "errors": [], "warnings": [],
            "clip_count": 5, "expected_duration_seconds": 40.0}
    data.update(over)
    return data


def quality_review(checks: list) -> dict:
    return {"checks": checks, "generation_method": "agent"}


def video_plan(**over) -> dict:
    data = {"units": [{"shot_keys": ["2-2", "2-3"], "duration_s": 6}],
            "decisions": [{"shot_keys": ["1-1"], "method": "references"},
                          {"shot_keys": ["1-2"], "method": "keyframes"}]}
    data.update(over)
    return data
