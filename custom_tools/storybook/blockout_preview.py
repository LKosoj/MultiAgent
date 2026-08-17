"""Э6: ``blockout_preview`` -- сплошная склейка отрендеренных болванок в одно
видео для просмотра человеком до трат на видеомодель.

Spec: docs/tz-blockout-reference-pipeline.md, разделы 10.3, 10.3.1, 16.1
(команда-образец, дословно не копируется), 17, 20.3, 21, 22 (в т.ч. A11).

Contract (раздел 10.3): этот шаг НИКОГДА не поднимает исключение наружу --
любая внутренняя ошибка деградирует в ``status: "warning"``, потому что
``storybook_pipeline.yaml`` использует ``on_failure: stop`` и падение здесь
отменило бы дорогой ``video_generator``/``montage_assembler`` без причины.
Допустимые значения ``status``: ``success``, ``partial``, ``warning``,
``skipped`` (нулевой контракт).

Что делает шаг:
1. Читает ``93_blockout/chains.json`` (пишется ``blockout_scene_builder``) и
   строит общий порядок шотов проекта: сцены по возрастанию, внутри сцены --
   шоты по возрастанию (порядок цепочек в файле не используется).
2. Для каждого шота ищет ``93_blockout/scene_NN_shot_MM/blockout_ref.mp4``.
   Уже содержит ровно N_video кадров (раздел 17.2) -- дополнительная
   обрезка не нужна. Отсутствующий сегмент пропускается с предупреждением.
3. Если сегментов не нашлось вовсе -- ``status: "warning"``, ничего не
   создаётся (раздел 17.2).
4. Склеивает найденные сегменты в ``preview/blockout_all.mp4``
   (``-f concat -safe 0 -c copy``). fps/resolution каждого сегмента
   сверяются с первым включённым (данные берутся из уже готового
   ``manifest.json``, без ffprobe) -- несовпадающий сегмент перекодируется.
5. При ``burnin=True`` -- второй проход с ``drawtext`` (сцена/шот/
   длительность/цепочка/таймкод, белым внутри цепочки и жёлтым на первом
   шоте цепочки) в ``preview/blockout_all_burnin.mp4``.
6. Перед сборкой удаляет прежние ``contact_sheet*.png`` и прежний
   ``blockout_all_burnin.mp4`` (в т.ч. когда ``burnin=False`` -- не должно
   оставаться устаревшего файла).
7. Строит ``preview/contact_sheet.png`` (+ ``_02.png`` и т.д. при
   переполнении, 24 шота на страницу) -- пары ``ref_start.png``/
   ``ref_end.png`` с подписями; шот без обоих кадров занимает своё место
   серой ячейкой с подписью «не отрендерен».
8. Дописывает свою секцию в ``93_blockout/report.json`` через общий
   ``merge_write_report`` (импортируется как есть, не переопределяется).

Никаких новых кодов B*/P* этот шаг не владеет -- диагностика идёт как
произвольные текстовые записи ``level: "warning"`` в собственной секции.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

from custom_tools.storybook.blockout_common import n_video
from custom_tools.storybook.blockout_scene_builder import merge_write_report
from custom_tools.storybook.project_paths import safe_storybook_project_dir

logger = logging.getLogger(__name__)

REPORT_SECTION = "blockout_preview"

_FONT_ENV_VAR = "BLOCKOUT_PREVIEW_FONT"
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)


# =============================================================================
# Zero contract / small helpers (раздел 10.3.1, стиль соседних blockout_*)
# =============================================================================


def _zero_contract(project_id: str) -> Dict[str, Any]:
    # раздел 10.2: абсолютный путь через safe_storybook_project_dir() --
    # никогда не хардкодить 'plots/storybooks/...' (неверно при кастомном
    # STORYBOOK_PROJECTS_DIR).
    project_dir = safe_storybook_project_dir(project_id)
    return {
        "status": "skipped",
        "segments_total": 0,
        "segments_included": 0,
        "total_duration_s": 0,
        "blockout_all_path": None,
        "blockout_all_burnin_path": None,
        "contact_sheet_paths": [],
        "artifact_path": str(project_dir / "93_blockout" / "preview" / "blockout_all.mp4"),
    }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(value)


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _resolve_paths(project_id: str) -> Dict[str, Path]:
    project_dir = safe_storybook_project_dir(project_id)
    blockout_dir = project_dir / "93_blockout"
    return {
        "project_dir": project_dir,
        "blockout_dir": blockout_dir,
        "preview_dir": blockout_dir / "preview",
        "chains": blockout_dir / "chains.json",
        "report": blockout_dir / "report.json",
    }


def shot_dir_name(scene_number: int, shot_number: int) -> str:
    return f"scene_{int(scene_number):02d}_shot_{int(shot_number):02d}"


# =============================================================================
# chains.json -> общий порядок шотов (чистая функция, раздел 17.2)
# =============================================================================


def shots_from_chains_payload(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Разворачивает ``chains.json`` в список шотов, упорядоченный по
    (scene_number, shot_number) -- порядок цепочек в файле игнорируется."""
    if not isinstance(payload, dict):
        return []
    shots: List[Dict[str, Any]] = []
    for chain in payload.get("chains") or []:
        if not isinstance(chain, dict):
            continue
        chain_id = chain.get("chain_id")
        scene_number = chain.get("scene_number")
        chain_shots = chain.get("shots") or []
        for idx, shot in enumerate(chain_shots):
            try:
                scene_number_int = int(scene_number)
                shot_number_int = int(shot.get("shot_number"))
                duration_s_int = int(shot.get("duration_s"))
            except (TypeError, ValueError):
                continue
            shots.append(
                {
                    "scene_number": scene_number_int,
                    "shot_number": shot_number_int,
                    "chain_id": chain_id,
                    "duration_s": duration_s_int,
                    "is_first_in_chain": idx == 0,
                }
            )
    shots.sort(key=lambda s: (s["scene_number"], s["shot_number"]))
    return shots


# =============================================================================
# Сверка fps/resolution с первым включённым сегментом (чистая функция)
# =============================================================================


def plan_segment_reencodes(segments: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Сравнивает fps/resolution каждого сегмента с первым сегментом, у
    которого эти данные известны (из manifest.json), и помечает несовпадающие
    как требующие перекодирования к параметрам этого первого сегмента
    (раздел 17.2: ``-c copy`` требует одинаковых параметров потока)."""
    baseline: Optional[Tuple[int, Tuple[int, int]]] = None
    result: List[Dict[str, Any]] = []
    for seg in segments:
        fps = seg.get("fps")
        resolution = seg.get("resolution")
        known = fps is not None and bool(resolution)
        needs_reencode = False
        item = dict(seg)
        if known:
            current = (int(fps), tuple(int(v) for v in resolution))
            if baseline is None:
                baseline = current
            elif current != baseline:
                needs_reencode = True
        item["needs_reencode"] = needs_reencode
        if needs_reencode and baseline is not None:
            item["target_fps"] = baseline[0]
            item["target_resolution"] = list(baseline[1])
        else:
            item["target_fps"] = None
            item["target_resolution"] = None
        result.append(item)
    return result


# =============================================================================
# drawtext burn-in (чистые функции; экранирование ':'/'·' -- проверено
# эмпирически: неэкранированное ':' в text='...' молча обрезает подпись)
# =============================================================================


def escape_drawtext_text(text: str) -> str:
    # backslash must be escaped FIRST -- escaping it after the other
    # characters below would double-escape the backslashes they just added.
    text = text.replace("\\", "\\\\")
    return text.replace(":", "\\:").replace("·", "\\·").replace("'", "\\'")


# =============================================================================
# P2.11: per-shot two-line burn-in (applied to each shot's own clip, not the
# concatenated timeline, so ffmpeg's `n` resets per shot -- see build_shot_
# burnin_filter_chain below and its call site in _run()).
# =============================================================================


def shot_burnin_summary_text(scene_number: int, shot_number: int, duration_s: int) -> str:
    return f"SC {int(scene_number):02d}·SH {int(shot_number):02d}·{int(duration_s)}s"


def build_shot_burnin_line1_filter(
    scene_number: int, shot_number: int, duration_s: int, font_path: str, *, fontsize: int = 14
) -> str:
    text = escape_drawtext_text(shot_burnin_summary_text(scene_number, shot_number, duration_s))
    return (
        f"drawtext=fontfile='{font_path}':text='{text}':fontcolor=white:fontsize={fontsize}"
        f":box=1:boxcolor=black@0.55:boxborderw=4:x=10:y=10"
    )


def build_shot_burnin_line2_filter(font_path: str, fps: int, frame_count: int, *, fontsize: int = 14) -> str:
    # %{expr\:...} is ffmpeg's own per-frame text macro (n = 0-based frame
    # number of THIS ffmpeg run) -- verified empirically that its float
    # output always prints 6 decimals, no matter the expression, so t= uses
    # %{eif\:...\:d} (integer-formatted eval) with a manual trunc/mod split
    # to get exactly one decimal instead. Must stay raw, escape_drawtext_text()
    # would double-escape the intentional single backslash before ':'.
    text = (
        f"t=%{{eif\\:trunc(n/{int(fps)})\\:d}}.%{{eif\\:mod(trunc(n/{int(fps)}*10)\\,10)\\:d}}s"
        f"  frame=%{{eif\\:n+1\\:d}}/{int(frame_count)}"
    )
    return (
        f"drawtext=fontfile='{font_path}':text='{text}':fontcolor=white:fontsize={fontsize}"
        f":box=1:boxcolor=black@0.55:boxborderw=4:x=w-tw-10:y=10"
    )


def build_shot_burnin_filter_chain(
    scene_number: int, shot_number: int, duration_s: int, fps: int, frame_count: int, font_path: str
) -> str:
    line1 = build_shot_burnin_line1_filter(scene_number, shot_number, duration_s, font_path)
    line2 = build_shot_burnin_line2_filter(font_path, fps, frame_count)
    return f"{line1},{line2}"


# =============================================================================
# ffmpeg argv builders (чистые функции)
# =============================================================================


def _escape_concat_path(path: Any) -> str:
    return str(path).replace("'", "'\\''")


def build_concat_list_lines(paths: Sequence[Any]) -> List[str]:
    return [f"file '{_escape_concat_path(p)}'" for p in paths]


def build_concat_ffmpeg_args(list_path: Path, output_path: Path) -> List[str]:
    return ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(output_path)]


def build_reencode_ffmpeg_args(input_path: Path, output_path: Path, fps: int, width: int, height: int) -> List[str]:
    return [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vf", f"scale={int(width)}:{int(height)}",
        "-r", str(int(fps)),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", "16",
        "-preset", "slow",
        "-movflags", "+faststart",
        str(output_path),
    ]


def build_burnin_ffmpeg_args(input_path: Path, output_path: Path, filter_chain: str) -> List[str]:
    # раздел 16.1 / плана: -vf drawtext=... -c:v libx264 -crf 18.
    return ["ffmpeg", "-y", "-i", str(input_path), "-vf", filter_chain, "-c:v", "libx264", "-crf", "18", str(output_path)]


def _find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def _find_font() -> Optional[str]:
    override = os.getenv(_FONT_ENV_VAR)
    if override and Path(override).is_file():
        return override
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def _run_ffmpeg(ffmpeg_bin: str, args: List[str], *, timeout: float) -> Tuple[bool, str]:
    try:
        proc = subprocess.run([ffmpeg_bin] + args[1:], capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if proc.returncode != 0:
        return False, proc.stderr[-2000:]
    return True, ""


# =============================================================================
# contact sheet (PIL) -- сетка пар опорных кадров с подписями (раздел 22, A11)
# =============================================================================

_SHEET_CELL_IMG_W = 320
_SHEET_CELL_IMG_H = 180
_SHEET_CAPTION_H = 24
_SHEET_PAIRS_PER_ROW = 4
_SHEET_ROWS_PER_PAGE = 6
_SHEET_SHOTS_PER_PAGE = _SHEET_PAIRS_PER_ROW * _SHEET_ROWS_PER_PAGE  # 24
_SHEET_CELL_W = _SHEET_CELL_IMG_W * 2
_SHEET_CELL_H = _SHEET_CELL_IMG_H + _SHEET_CAPTION_H
_SHEET_PAGE_W = _SHEET_PAIRS_PER_ROW * _SHEET_CELL_W  # 2560
_SHEET_PAGE_H = _SHEET_ROWS_PER_PAGE * _SHEET_CELL_H  # 1224
_SHEET_BG = (20, 20, 20)
_SHEET_GREY = (90, 90, 90)
_SHEET_CAPTION_COLOR = (230, 230, 230)


def paginate_shots(shots: Sequence[Dict[str, Any]], page_size: int = _SHEET_SHOTS_PER_PAGE) -> List[List[Dict[str, Any]]]:
    if not shots:
        return []
    return [list(shots[i : i + page_size]) for i in range(0, len(shots), page_size)]


def contact_sheet_page_filename(page_index: int) -> str:
    return "contact_sheet.png" if page_index == 0 else f"contact_sheet_{page_index + 1:02d}.png"


def render_contact_sheet_page(
    shots_page: Sequence[Dict[str, Any]],
    shot_dir_resolver: Callable[[Dict[str, Any]], Path],
    font_path: Optional[str],
) -> "Image.Image":
    sheet = Image.new("RGB", (_SHEET_PAGE_W, _SHEET_PAGE_H), _SHEET_BG)
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(font_path, 14) if font_path else ImageFont.load_default()
    for idx, shot in enumerate(shots_page):
        row, col = divmod(idx, _SHEET_PAIRS_PER_ROW)
        x0 = col * _SHEET_CELL_W
        y0 = row * _SHEET_CELL_H
        shot_dir = shot_dir_resolver(shot)
        ref_start = shot_dir / "ref_start.png"
        ref_end = shot_dir / "ref_end.png"
        rendered = ref_start.is_file() and ref_end.is_file()
        if rendered:
            start_img = Image.open(ref_start).convert("RGB").resize((_SHEET_CELL_IMG_W, _SHEET_CELL_IMG_H))
            end_img = Image.open(ref_end).convert("RGB").resize((_SHEET_CELL_IMG_W, _SHEET_CELL_IMG_H))
            sheet.paste(start_img, (x0, y0))
            sheet.paste(end_img, (x0 + _SHEET_CELL_IMG_W, y0))
            caption = f"scene {shot['scene_number']:02d} · shot {shot['shot_number']:02d} · {shot['duration_s']}s"
        else:
            grey_box = Image.new("RGB", (_SHEET_CELL_W, _SHEET_CELL_IMG_H), _SHEET_GREY)
            sheet.paste(grey_box, (x0, y0))
            caption = f"scene {shot['scene_number']:02d} · shot {shot['shot_number']:02d} · не отрендерен"
        draw.text((x0 + 4, y0 + _SHEET_CELL_IMG_H + 4), caption, fill=_SHEET_CAPTION_COLOR, font=font)
    return sheet


# =============================================================================
# report.json section (раздел 20.3) -- reuses the generic read-merge-write
# =============================================================================


def _report_write_summary(report_path: Path, summary: Dict[str, Any]) -> None:
    # раздел 20.3: report.json is accumulative but has a single creator --
    # "создаёт его blockout_scene_builder (первый по порядку)"; every other
    # writer (including blockout_preview) only appends its own section.
    # merge_write_report() itself would happily create the file from
    # scratch, so this step must not call it when the file doesn't exist
    # yet -- e.g. the "Собрать превью" button (раздел 18.4) can run this
    # step standalone, bypassing blockout_scene_builder. Same rule раздел
    # 20.3 gives for artist_batch_shots/video_generator.
    if not report_path.is_file():
        return
    merge_write_report(report_path, REPORT_SECTION, lambda _section: dict(summary))


# =============================================================================
# Поиск сегментов на диске (нечистая функция, чтение файлов проекта)
# =============================================================================


def _gather_segments(blockout_dir: Path, ordered_shots: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    included: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    for shot in ordered_shots:
        shot_dir = blockout_dir / shot_dir_name(shot["scene_number"], shot["shot_number"])
        video_path = shot_dir / "blockout_ref.mp4"
        if not video_path.is_file():
            warnings.append(
                {
                    "code": "note",
                    "level": "warning",
                    "message": f"scene {shot['scene_number']} shot {shot['shot_number']}: blockout_ref.mp4 missing, segment skipped",
                }
            )
            continue
        manifest = _read_json(shot_dir / "manifest.json", default=None)
        fps = manifest.get("fps") if isinstance(manifest, dict) else None
        resolution = manifest.get("resolution") if isinstance(manifest, dict) else None
        if fps is None or not resolution:
            warnings.append(
                {
                    "code": "note",
                    "level": "warning",
                    "message": f"scene {shot['scene_number']} shot {shot['shot_number']}: manifest.json fps/resolution unavailable, compatibility check skipped",
                }
            )
        included.append({**shot, "path": video_path, "fps": fps, "resolution": resolution})
    return included, warnings


# =============================================================================
# Основной проход (нечистая функция; вызывается из-под try/except в
# blockout_preview_tool -- никогда не должна давать исключению уйти дальше)
# =============================================================================


def _run(project_id: str, burnin: bool) -> Dict[str, Any]:
    paths = _resolve_paths(project_id)
    blockout_dir = paths["blockout_dir"]
    preview_dir = paths["preview_dir"]

    chains_payload = _read_json(paths["chains"], default=None)
    ordered_shots = shots_from_chains_payload(chains_payload)
    segments_total = len(ordered_shots)

    included, warnings = _gather_segments(blockout_dir, ordered_shots)

    if not included:
        # раздел 17.2: "если не найден ни один, шаг возвращает status:
        # warning и ничего не создаёт" -- относится к preview/ (blockout_all*,
        # contact_sheet*); report.json дописывается всегда (раздел 20.3).
        warnings.append({"code": "note", "level": "warning", "message": "no blockout_ref.mp4 segments found; nothing built"})
        summary = {
            "status": "warning",
            "segments_total": segments_total,
            "segments_included": 0,
            "total_duration_s": 0,
            "checks": warnings,
        }
        _report_write_summary(paths["report"], summary)
        return {
            "status": "warning",
            "segments_total": segments_total,
            "segments_included": 0,
            "total_duration_s": 0,
            "blockout_all_path": None,
            "blockout_all_burnin_path": None,
            "contact_sheet_paths": [],
            "artifact_path": str(preview_dir / "blockout_all.mp4"),
        }

    preview_dir.mkdir(parents=True, exist_ok=True)
    # раздел 8: перед сборкой удаляются прежние contact_sheet*.png и прежний
    # blockout_all_burnin.mp4 -- в т.ч. когда burnin=False, чтобы не
    # оставался устаревший файл.
    for stale in preview_dir.glob("contact_sheet*.png"):
        stale.unlink(missing_ok=True)
    blockout_all_path = preview_dir / "blockout_all.mp4"
    burnin_path = preview_dir / "blockout_all_burnin.mp4"
    burnin_path.unlink(missing_ok=True)

    total_duration_s = sum(int(seg["duration_s"]) for seg in included)
    ffmpeg_bin = _find_ffmpeg()
    concat_ok = False
    burnin_built = False

    if ffmpeg_bin is None:
        warnings.append({"code": "note", "level": "warning", "message": "ffmpeg not found in PATH; blockout_all.mp4 not built"})
    else:
        with tempfile.TemporaryDirectory(prefix="blockout_preview_") as tmp_dir_name:
            tmp_dir = Path(tmp_dir_name)
            planned = plan_segment_reencodes(included)
            # a segment's needs_reencode stays True even when the reencode
            # ffmpeg call below fails and falls back to the original file --
            # actual_fps tracks the REAL fps of the file that ends up in
            # concat_paths, so burn-in (below) never uses target_fps against
            # a still-original-fps file.
            baseline_fps = next((p["fps"] for p in planned if p.get("fps")), None)
            concat_paths: List[Path] = []
            actual_fps: List[Optional[int]] = []
            for seg in planned:
                if seg["needs_reencode"]:
                    reenc_path = tmp_dir / f"reencoded_scene_{seg['scene_number']:02d}_shot_{seg['shot_number']:02d}.mp4"
                    target_w, target_h = seg["target_resolution"]
                    args = build_reencode_ffmpeg_args(seg["path"], reenc_path, seg["target_fps"], target_w, target_h)
                    ok, err = _run_ffmpeg(ffmpeg_bin, args, timeout=600.0)
                    if ok:
                        concat_paths.append(reenc_path)
                        actual_fps.append(seg["target_fps"])
                        warnings.append(
                            {
                                "code": "note",
                                "level": "warning",
                                "message": f"scene {seg['scene_number']} shot {seg['shot_number']}: re-encoded to match first segment's fps/resolution",
                            }
                        )
                    else:
                        concat_paths.append(seg["path"])
                        actual_fps.append(seg.get("fps") or baseline_fps)
                        warnings.append(
                            {
                                "code": "note",
                                "level": "warning",
                                "message": f"scene {seg['scene_number']} shot {seg['shot_number']}: re-encode failed ({err}); used as-is",
                            }
                        )
                else:
                    concat_paths.append(seg["path"])
                    actual_fps.append(seg.get("fps") or baseline_fps)

            list_path = tmp_dir / "list.txt"
            list_path.write_text("\n".join(build_concat_list_lines(concat_paths)), encoding="utf-8")
            args = build_concat_ffmpeg_args(list_path, blockout_all_path)
            ok, err = _run_ffmpeg(ffmpeg_bin, args, timeout=600.0)
            if ok:
                concat_ok = True
            else:
                warnings.append({"code": "note", "level": "warning", "message": f"ffmpeg concat failed: {err}"})

            if concat_ok and _as_bool(burnin):
                font_path = _find_font()
                if font_path is None:
                    warnings.append({"code": "note", "level": "warning", "message": "burn-in font not found; blockout_all_burnin.mp4 not built"})
                else:
                    # P2.11: burn-in is per-shot (SC/SH/duration + shot-local
                    # t/frame counter via ffmpeg's own `n`), so each concat
                    # input is burned in individually BEFORE concatenation --
                    # doing it on the already-concatenated timeline would make
                    # `n` count frames across the whole video instead of
                    # resetting per shot.
                    burnin_paths: List[Path] = []
                    for plan, src_path, effective_fps in zip(planned, concat_paths, actual_fps):
                        if not effective_fps:
                            warnings.append(
                                {
                                    "code": "note",
                                    "level": "warning",
                                    "message": f"scene {plan['scene_number']} shot {plan['shot_number']}: fps unknown, burn-in text skipped for this segment",
                                }
                            )
                            burnin_paths.append(src_path)
                            continue
                        frame_count = n_video(plan["duration_s"], effective_fps)
                        seg_filter_chain = build_shot_burnin_filter_chain(
                            plan["scene_number"], plan["shot_number"], plan["duration_s"], effective_fps, frame_count, font_path
                        )
                        burnin_seg_path = tmp_dir / f"burnin_scene_{plan['scene_number']:02d}_shot_{plan['shot_number']:02d}.mp4"
                        args = build_burnin_ffmpeg_args(src_path, burnin_seg_path, seg_filter_chain)
                        ok, err = _run_ffmpeg(ffmpeg_bin, args, timeout=300.0)
                        if ok:
                            burnin_paths.append(burnin_seg_path)
                        else:
                            warnings.append(
                                {
                                    "code": "note",
                                    "level": "warning",
                                    "message": f"scene {plan['scene_number']} shot {plan['shot_number']}: burn-in pass failed ({err}); used without burn-in",
                                }
                            )
                            burnin_paths.append(src_path)

                    burnin_list_path = tmp_dir / "burnin_list.txt"
                    burnin_list_path.write_text("\n".join(build_concat_list_lines(burnin_paths)), encoding="utf-8")
                    args = build_concat_ffmpeg_args(burnin_list_path, burnin_path)
                    ok, err = _run_ffmpeg(ffmpeg_bin, args, timeout=600.0)
                    if ok:
                        burnin_built = True
                    else:
                        warnings.append({"code": "note", "level": "warning", "message": f"burn-in concat failed: {err}"})

    contact_sheet_paths: List[Path] = []
    font_path_for_sheet = _find_font()
    for idx, page in enumerate(paginate_shots(ordered_shots)):
        try:
            image = render_contact_sheet_page(
                page, lambda s: blockout_dir / shot_dir_name(s["scene_number"], s["shot_number"]), font_path_for_sheet
            )
            page_path = preview_dir / contact_sheet_page_filename(idx)
            image.save(page_path)
            contact_sheet_paths.append(page_path)
        except Exception as exc:  # noqa: BLE001
            warnings.append({"code": "note", "level": "warning", "message": f"contact sheet page {idx + 1}: failed to build ({exc})"})

    segments_included = len(included)
    if not concat_ok:
        status = "warning"
    elif segments_included < segments_total:
        status = "partial"
    else:
        status = "success"

    summary = {
        "status": status,
        "segments_total": segments_total,
        "segments_included": segments_included,
        "total_duration_s": total_duration_s if concat_ok else 0,
        "burnin_built": burnin_built,
        "contact_sheet_pages": len(contact_sheet_paths),
        "checks": warnings,
    }
    _report_write_summary(paths["report"], summary)

    return {
        "status": status,
        "segments_total": segments_total,
        "segments_included": segments_included,
        "total_duration_s": total_duration_s if concat_ok else 0,
        "blockout_all_path": str(blockout_all_path) if concat_ok else None,
        "blockout_all_burnin_path": str(burnin_path) if burnin_built else None,
        "contact_sheet_paths": [str(p) for p in contact_sheet_paths],
        "artifact_path": str(blockout_all_path),
    }


def _crash_fallback(project_id: str, exc: Exception) -> Dict[str, Any]:
    try:
        paths = _resolve_paths(project_id)
    except Exception:  # noqa: BLE001
        return {
            "status": "warning",
            "segments_total": 0,
            "segments_included": 0,
            "total_duration_s": 0,
            "blockout_all_path": None,
            "blockout_all_burnin_path": None,
            "contact_sheet_paths": [],
            "artifact_path": "",
        }
    try:
        _report_write_summary(
            paths["report"],
            {"status": "warning", "checks": [{"code": "note", "level": "warning", "message": f"blockout_preview crashed: {exc}"}]},
        )
    except Exception:  # noqa: BLE001
        pass
    return {
        "status": "warning",
        "segments_total": 0,
        "segments_included": 0,
        "total_duration_s": 0,
        "blockout_all_path": None,
        "blockout_all_burnin_path": None,
        "contact_sheet_paths": [],
        "artifact_path": str(paths["preview_dir"] / "blockout_all.mp4"),
    }


# =============================================================================
# Публичная точка входа
# =============================================================================


def blockout_preview_tool(session_id: str, project_id: str, burnin: bool = True, enable: bool = True) -> Dict[str, Any]:
    del session_id
    if not _as_bool(enable):
        try:
            return _zero_contract(project_id)
        except Exception:  # noqa: BLE001
            # symmetric with _crash_fallback(): an invalid project_id must
            # degrade to status="skipped" here too, never raise (раздел 10.3).
            return {
                "status": "skipped",
                "segments_total": 0,
                "segments_included": 0,
                "total_duration_s": 0,
                "blockout_all_path": None,
                "blockout_all_burnin_path": None,
                "contact_sheet_paths": [],
                "artifact_path": "",
            }
    try:
        return _run(project_id, burnin)
    except Exception as exc:  # noqa: BLE001
        logger.exception("blockout_preview_tool crashed; degrading to status=warning (раздел 10.3)")
        return _crash_fallback(project_id, exc)
