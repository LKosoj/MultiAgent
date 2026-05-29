import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import logging

# Ensure repo root is importable BEFORE importing project packages.
def _ensure_repo_root_on_syspath() -> Path:
    """
    Make the repository root importable regardless of the current working directory.
    This is intentionally executed at import time (top of module) so early imports succeed.
    """
    here = Path(__file__).resolve()
    for p in [here.parent, *here.parents]:
        # Heuristic: repo root contains StoryBookManager/ and agent_command.py
        if (p / "StoryBookManager").is_dir() and (p / "agent_command.py").exists():
            repo_root = p
            if str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
            # Some project modules import "config.*" expecting StoryBookManager/ to be on sys.path.
            sbm_root = repo_root / "StoryBookManager"
            # IMPORTANT: keep repo_root ahead of sbm_root to avoid shadowing top-level modules like utils.py
            if sbm_root.is_dir() and str(sbm_root) not in sys.path:
                insert_at = 1 if len(sys.path) > 0 and sys.path[0] == str(repo_root) else 0
                sys.path.insert(insert_at, str(sbm_root))
            try:
                os.chdir(repo_root)
            except Exception:
                pass
            return repo_root
    # fallback: just keep current dir
    return Path.cwd()


_REPO_ROOT = _ensure_repo_root_on_syspath()

from StoryBookManager.utils.logging_config import setup_logging  # noqa: E402

setup_logging()

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class ShotKey:
    scene_number: int
    shot_number: int
    shot_type: str  # "start" | "end"


def _safe_str(x: Any) -> str:
    if x is None:
        return ""
    return str(x)


def _extract_transition_camera_change(video_prompt: str) -> str:
    # Раньше эта функция помечала отчёт как "camera_move:" / "camera_static_or_unspecified:"
    # на основе хардкод-словаря (zoom/dolly/push/pull/track) — но он пропускал crane, pan,
    # tilt, arc, reveal и пр., и нарушал правило refactoring.md о closed-world эвристиках.
    # Для текстового отчёта классификация не нужна: достаточно показать сам video_prompt.
    vp = (video_prompt or "").strip()
    if not vp:
        return ""
    return vp


def _index_storyboard(screenplay: Dict[str, Any]) -> Dict[Tuple[int, int], Dict[str, Any]]:
    lookup: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for sc in (screenplay.get("screenplay") or []):
        try:
            sn = int(sc.get("scene_number", 0))
        except Exception:
            continue
        for sh in (sc.get("storyboard") or []):
            try:
                shn = int(sh.get("shot_number", 0))
            except Exception:
                continue
            lookup[(sn, shn)] = {
                "camera_plan": _safe_str(sh.get("camera_plan", "")),
                "timing": _safe_str(sh.get("timing", "")),
                "description": _safe_str(sh.get("description", "")),
                "scene_action": _safe_str(sc.get("action", "")),
                "location_time": _safe_str(sc.get("location_time", "")),
            }
    return lookup


def _index_items(items: Iterable[Dict[str, Any]]) -> Dict[ShotKey, Dict[str, Any]]:
    out: Dict[ShotKey, Dict[str, Any]] = {}
    for it in items:
        try:
            key = ShotKey(
                scene_number=int(it.get("scene_number", 0)),
                shot_number=int(it.get("shot_number", 0)),
                shot_type=_safe_str(it.get("shot_type", "")).strip().lower(),
            )
        except Exception:
            continue
        out[key] = it
    return out


def _format_block(title: str, text: str) -> str:
    t = (text or "").strip()
    return f"{title}:\n{t}\n"


def _find_repo_root(start: Path) -> Path:
    """
    Tries to locate the repository root so the script can be executed
    both from project root and from its own directory.
    """
    start = start.resolve()
    for p in [start] + list(start.parents):
        try:
            if (p / "custom_tools").is_dir() and (p / "plots").is_dir() and (p / "requirements.txt").exists():
                return p
        except Exception:
            continue
    # Fallback: current working directory
    return Path.cwd().resolve()


def _ensure_repo_root_context() -> Path:
    """
    Ensures:
    - repo root is on sys.path (so `import custom_tools...` works)
    - cwd is repo root (so relative paths like plots/... work)
    """
    repo_root = _find_repo_root(Path(__file__).resolve())
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    try:
        os.chdir(repo_root_str)
    except Exception:
        pass
    return repo_root


def build_shots_text_report(
    *,
    shots_data: Dict[str, Any],
    screenplay_data: Dict[str, Any],
    limit_scenes: Optional[List[int]] = None,
    limit_shots: Optional[List[int]] = None,
) -> str:
    items = list(shots_data.get("items") or [])
    storyboard = _index_storyboard(screenplay_data or {})
    idx = _index_items(items)

    # group per (scene, shot)
    pairs: List[Tuple[int, int]] = sorted(
        {(k.scene_number, k.shot_number) for k in idx.keys()},
        key=lambda x: (x[0], x[1]),
    )
    if limit_scenes is not None:
        allow = {int(x) for x in limit_scenes}
        pairs = [p for p in pairs if p[0] in allow]
    if limit_shots is not None:
        allow = {int(x) for x in limit_shots}
        pairs = [p for p in pairs if p[1] in allow]

    out_lines: List[str] = []
    out_lines.append("SHOTS PROMPTS REPORT\n")

    for sn, shn in pairs:
        meta = storyboard.get((sn, shn), {})
        cam_plan = _safe_str(meta.get("camera_plan", ""))
        timing = _safe_str(meta.get("timing", ""))
        desc = _safe_str(meta.get("description", ""))
        scene_action = _safe_str(meta.get("scene_action", ""))
        location_time = _safe_str(meta.get("location_time", ""))

        start = idx.get(ShotKey(sn, shn, "start"))
        end = idx.get(ShotKey(sn, shn, "end"))

        out_lines.append("=" * 120)
        out_lines.append(f"SCENE {sn}  SHOT {shn}   storyboard.camera_plan={cam_plan}   timing={timing}")
        if location_time:
            out_lines.append(f"scene.location_time: {location_time}")
        if scene_action:
            out_lines.append(_format_block("scene.action", scene_action).rstrip())
        out_lines.append(_format_block("storyboard.description", desc).rstrip())

        start_vp = _safe_str((start or {}).get("video_prompt", ""))
        camera_change = _extract_transition_camera_change(start_vp)
        out_lines.append(_format_block("camera_change_from_start_video_prompt", camera_change).rstrip())

        if start:
            out_lines.append("-" * 120)
            out_lines.append("START")
            out_lines.append(_format_block("english_prompt", _safe_str(start.get("english_prompt", ""))).rstrip())
            out_lines.append(_format_block("video_prompt", start_vp).rstrip())
        else:
            out_lines.append("START: <missing>\n")

        if end:
            out_lines.append("-" * 120)
            out_lines.append("END")
            out_lines.append(_format_block("english_prompt", _safe_str(end.get("english_prompt", ""))).rstrip())
            out_lines.append(_format_block("video_prompt", _safe_str(end.get("video_prompt", ""))).rstrip())
        else:
            out_lines.append("END: <missing>\n")

    out_lines.append("\n")
    return "\n".join(out_lines) + "\n"


def generate_before_after_qa_reports(
    *,
    project_id: str,
    shots_json_path: str,
    screenplay_json_path: str,
    out_before_path: str,
    out_after_path: str,
    # Generation (full flow)
    gen_session_id: str = "shots_prompt_qa_reporter_gen",
    gen_generate_end_shots: bool = True,
    gen_language: str = "en",
    gen_scene_numbers: Optional[List[int]] = None,
    gen_max_scenes: Optional[int] = None,
    gen_force: bool = True,
    qa_model: str = "ultimate",
    qa_temperature: float = 0.1,
    qa_max_scenes: Optional[int] = None,
    qa_scene_numbers: Optional[List[int]] = None,
    qa_force: bool = True,
    qa_global_max_repairs: int = 0,
    limit_scenes: Optional[List[int]] = None,
    limit_shots: Optional[List[int]] = None,
) -> None:
    # Make this callable from both repo root and this file's directory.
    _ensure_repo_root_context()

    # Delayed imports: allow running as a script from subdir (repo root will be on sys.path).
    from custom_tools.storybook.screenplay_shots_generator import screenplay_shots_generator_tool
    from custom_tools.storybook.shots_prompt_qa import shots_prompt_qa_tool

    # 1) GENERATE shots.json (or overwrite) for requested scope
    # NOTE: generator writes shots.json to the standard project path; we also keep the returned in-memory data.
    shots_data = screenplay_shots_generator_tool(
        session_id=gen_session_id,
        project_id=project_id,
        generate_end_shots=gen_generate_end_shots,
        enable=True,
        language=gen_language,
        scene_numbers=gen_scene_numbers,
        max_scenes=gen_max_scenes,
        force=gen_force,
    )
    screenplay_data = json.loads(Path(screenplay_json_path).read_text(encoding="utf-8"))

    before_txt = build_shots_text_report(
        shots_data=shots_data,
        screenplay_data=screenplay_data,
        limit_scenes=limit_scenes,
        limit_shots=limit_shots,
    )
    Path(out_before_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_before_path).write_text(before_txt, encoding="utf-8")

    # 2) QA (dry-run in-memory) and save AFTER report
    qa_out = shots_prompt_qa_tool(
        session_id="shots_prompt_qa_reporter",
        project_id=project_id,
        shots_data=json.loads(json.dumps(shots_data, ensure_ascii=False)),
        enable=True,
        model=qa_model,
        temperature=qa_temperature,
        max_scenes=qa_max_scenes,
        scene_numbers=qa_scene_numbers,
        force=qa_force,
        global_max_repairs=qa_global_max_repairs,
        dry_run=True,
    )
    after_txt = build_shots_text_report(
        shots_data=qa_out,
        screenplay_data=screenplay_data,
        limit_scenes=limit_scenes,
        limit_shots=limit_shots,
    )
    Path(out_after_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_after_path).write_text(after_txt, encoding="utf-8")


def _parse_int_list_csv(value: str) -> List[int]:
    if not value:
        return []
    out: List[int] = []
    for part in re.split(r"[,\s]+", value.strip()):
        if not part:
            continue
        out.append(int(part))
    return out


def main() -> int:
    """
    Script entrypoint: runs FULL flow:
      1) generate shots.json (scenes 1,2 by default for our project)
      2) write BEFORE report
      3) run QA (dry-run)
      4) write AFTER report
    """
    import argparse

    _ensure_repo_root_context()

    parser = argparse.ArgumentParser(description="Generate shots.json + write before/after QA prompt reports.")
    parser.add_argument("--project-id", default="dolboyazher11")
    parser.add_argument("--scenes", default="1", help="Comma-separated scene numbers to generate+QA (e.g. '1,2' or '2,3').")
    parser.add_argument("--out-dir", default="", help="Optional output dir for reports (default: project 97_shots folder).")
    parser.add_argument("--qa-model", default="ultimate")
    parser.add_argument("--qa-temperature", type=float, default=0.1)
    parser.add_argument("--qa-global-max-repairs", type=int, default=0)
    args = parser.parse_args()

    project_id = str(args.project_id)
    scenes = _parse_int_list_csv(str(args.scenes))
    if not scenes:
        scenes = [1]

    base = Path("plots/storybooks") / project_id
    shots_json_path = str(base / "97_shots" / "shots.json")
    screenplay_json_path = str(base / "91_screenplay" / "screenplay.json")

    out_dir = Path(str(args.out_dir)) if str(args.out_dir).strip() else (base / "97_shots")
    out_before_path = str(out_dir / "prompts_before_qa.txt")
    out_after_path = str(out_dir / "prompts_after_qa.txt")

    qa_global_max_repairs = max(int(args.qa_global_max_repairs), 100)

    generate_before_after_qa_reports(
        project_id=project_id,
        shots_json_path=shots_json_path,
        screenplay_json_path=screenplay_json_path,
        out_before_path=out_before_path,
        out_after_path=out_after_path,
        gen_session_id="shots_prompt_qa_reporter_gen",
        gen_scene_numbers=scenes,
        gen_force=True,
        qa_model=str(args.qa_model),
        qa_temperature=float(args.qa_temperature),
        qa_scene_numbers=scenes,
        qa_force=True,
        qa_global_max_repairs=qa_global_max_repairs,
        # report scope should match the same scenes by default
        limit_scenes=scenes,
    )

    print(f"Wrote BEFORE report: {out_before_path}")
    print(f"Wrote AFTER  report: {out_after_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

