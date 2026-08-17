"""
Вкладка «Болванка» (раздел 18.4 ТЗ, docs/tz-blockout-reference-pipeline.md)
============================================================================

Три области: слева — дерево шотов (сцены → цепочки → шоты) с пометкой
состояния и полосой замечаний под ним; справа сверху — сравнение четырёх
изображений выбранного шота; справа снизу — два независимых видеоплеера
(опорный ролик болванки и готовое видео). Кнопки внизу запускают ровно один
шаг пайплайна через уже существующую блокировку `GenerationPanel.is_generating`
(`gui/generation_panel.py::run_blockout_scoped_step()`), правят
`97_shots/shots.json` по общему протоколу раздела 10.2
(`core/file_manager.py::save_json_file()`) или пишут `manifest.json` шота
напрямую (раздел 19.2, «Оставить как есть»).

Модуль намеренно НЕ импортирует `custom_tools.storybook.blockout_renderer`
целиком — он тянет за собой `blockout_blender`, `blockout_common` и
`video_generator_aitunnel_media` (см. комментарий у `compute_spec_hash()`
ниже), а это тяжёлая связность для GUI-кода, которой избегает и
`file_manager.py` (`_shot_merge_key()` там же продублирован по той же
причине). Из этого модуля продублированы только две маленькие чистые
функции — `compute_spec_hash()` и логика `is_manifest_current` в составе
`compute_shot_state()`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from tkinter import ttk, messagebox
import tkinter as tk
from typing import Any, Dict, List, Optional, Sequence, Tuple

from StoryBookManager.core.file_manager import FileManager
from StoryBookManager.core.media_processor import MediaProcessor
from StoryBookManager.gui.video_player import VideoPlayer

logger = logging.getLogger(__name__)

BLOCKOUT_DIR_NAME = "93_blockout"


# =============================================================================
# Чистые функции (тестируются без Tk)
# =============================================================================


def shot_dir_name(scene_number: int, shot_number: int) -> str:
    """Имя директории шота в 93_blockout/ (раздел 8) — согласовано с 97_shots/."""
    return f"scene_{int(scene_number):02d}_shot_{int(shot_number):02d}"


def compute_spec_hash(chain_spec: Dict[str, Any]) -> str:
    """Дубликат `custom_tools/storybook/blockout_renderer.py::compute_spec_hash()`.

    Должен давать байт-в-байт тот же хеш на одном и том же словаре цепочки
    из `scene_spec.json` — иначе сравнение со `spec_hash` в `manifest.json`
    ничего не значит. Импортировать сам `blockout_renderer.py` в GUI нельзя:
    он тянет `blockout_blender`, `blockout_common` и
    `video_generator_aitunnel_media` — тяжёлая связность для интерфейса
    (тот же принцип, что и у `_shot_merge_key()` в `core/file_manager.py`).
    """
    canonical = json.dumps(chain_spec, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def format_timing(duration_s: Any) -> str:
    """MM:SS из duration_s — то же правило, что нормализация раздела 6.2, пункт 4."""
    total = int(round(float(duration_s)))
    return f"{total // 60:02d}:{total % 60:02d}"


def parse_timing_seconds(timing: Any) -> Optional[float]:
    """Разбор `MM:SS` (или голого числа секунд) в секунды; None, если не удалось."""
    if not timing or not isinstance(timing, str):
        return None
    parts = timing.strip().split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 1:
            return float(parts[0])
    except ValueError:
        return None
    return None


def build_manual_duration_update(item: Dict[str, Any], new_duration_s: int) -> Dict[str, Any]:
    """Раздел 18.4, «Вместе с duration_s вкладка переписывает timing».

    Поля, которые ручная правка длительности кладёт в ОДИН элемент шота:
    `duration_s`, `duration_source: "manual"`, `timing` — всегда; и
    `duration_requested_s` — только если элемент его ещё не хранит (не
    перезаписывает существующее значение: «исходный запрос фиксируется один
    раз и навсегда»), тогда оно заполняется разбором СТАРОГО `timing`
    элемента, до его перезаписи. Не мутирует `item` — вызывающий код сам
    накладывает результат на оба элемента пары start/end.
    """
    update: Dict[str, Any] = {
        "duration_s": new_duration_s,
        "duration_source": "manual",
        "timing": format_timing(new_duration_s),
    }
    if "duration_requested_s" not in item:
        old_seconds = parse_timing_seconds(item.get("timing"))
        update["duration_requested_s"] = old_seconds if old_seconds is not None else new_duration_s
    return update


def shot_duration_mismatches_scene(
    shots_duration_s: Optional[Any], scene_spec_duration_s: Optional[Any]
) -> bool:
    """B01 третьей формы (раздел 12): duration_s в shots.json разошёлся с
    тем, под что построен scene_spec.json (ручная правка без пересборки
    сцены). Пока true — «Перерисовать болванку шота» и «Перерисовать
    цепочку» неактивны, доступна только «Пересобрать сцену» (раздел 18.4)."""
    if scene_spec_duration_s is None or shots_duration_s is None:
        return False
    return shots_duration_s != scene_spec_duration_s


def parse_resolution_setting(value: Any) -> Optional[List[int]]:
    """Разбор строки `"WxH"` настройки `blockout_resolution` (см.
    `gui/generation_panel.py`, Combobox `values=("960x540", "1280x720",
    "1920x1080")`) в `[width, height]`. `None`, если разобрать не удалось —
    вызывающий код обязан трактовать `None` как «неизвестно», а не как
    отсутствие несовпадения (раздел 10.2, консервативная сторона ошибки)."""
    if not value or not isinstance(value, str):
        return None
    parts = value.lower().split("x")
    if len(parts) != 2:
        return None
    try:
        return [int(parts[0]), int(parts[1])]
    except ValueError:
        return None


def is_manifest_current(
    manifest: Optional[Dict[str, Any]],
    *, duration_s: Any, fps: Any, spec_hash: Any, resolution: Optional[List[int]] = None,
) -> bool:
    """Правило актуальности раздела 10.2 для интерфейса.

    Сверяет `duration_s`, `fps`, `spec_hash` и — начиная с фикса code-review
    Э10 — `resolution`. `blender_version` из пайплайновой
    `blockout_renderer.is_manifest_current()` по-прежнему не сравнивается: он
    синхронно недоступен GUI без запуска Blender/RPC (см. `_probe_checkpoint`
    и `generation_panel.py`'s `storybook_video_music_readiness()`), и раздел
    10.2 признаёт его необязательным для интерфейсной проверки.

    `resolution` здесь — это ПОЛЬЗОВАТЕЛЬСКАЯ настройка `blockout_resolution`
    (`"WxH"`, разобранная `parse_resolution_setting()`), а НЕ фактическое
    пересчитанное разрешение раздела 16.2 (формула согласования под
    соотношение сторон видеомодели — `_resolve_size_params()`,
    `video_model_caps.json`). Полное согласование 16.2 в GUI не дублируется
    по тому же принципу, что и `compute_spec_hash()` выше (не тянуть
    `blockout_blender`/`video_generator_aitunnel_media`). Из-за этого
    сравнение может дать ложное «устарела» для моделей с не-16:9
    соотношением сторон, где реально отрендеренное разрешение отличается от
    сырой настройки, — это ошибка в БЕЗОПАСНУЮ сторону (лишняя пометка
    «устарела» ведёт максимум к лишнему перерендеру, а не к пропуску
    рендера). Если `resolution` неизвестно (`None`) или отсутствует в
    `manifest`, считается несовпадением — манифест помечается устаревшим, а
    не актуальным: ошибка возможна только в сторону «показать устаревшим то,
    что на самом деле актуально», никогда в обратную.
    """
    if not manifest:
        return False
    manifest_resolution = manifest.get("resolution")
    resolution_ok = (
        resolution is not None
        and manifest_resolution is not None
        and list(manifest_resolution) == list(resolution)
    )
    return (
        manifest.get("duration_s") == duration_s
        and manifest.get("fps") == fps
        and manifest.get("spec_hash") == spec_hash
        and resolution_ok
    )


SHOT_STATE_MISSING = "missing"
SHOT_STATE_STALE = "stale"
SHOT_STATE_JUNCTION_ERROR = "junction_error"
SHOT_STATE_RENDERED = "rendered"

SHOT_STATE_LABELS = {
    SHOT_STATE_MISSING: "отсутствует",
    SHOT_STATE_STALE: "устарела",
    SHOT_STATE_JUNCTION_ERROR: "ошибка стыка",
    SHOT_STATE_RENDERED: "болванка отрендерена",
}


def compute_shot_state(
    manifest: Optional[Dict[str, Any]],
    *,
    shots_duration_s: Optional[Any] = None,
    scene_spec_duration_s: Optional[Any] = None,
    fps: Any = None,
    spec_hash: Optional[str] = None,
    resolution: Optional[List[int]] = None,
) -> str:
    """Раздел 18.4: «"Устарела" имеет ровно два основания» — расхождение
    `manifest.json` с текущими параметрами рендера (актуальность, 10.2), либо
    `duration_s` в `shots.json` не совпадает с длительностью, под которую
    построен `scene_spec.json` (B01 третьей формы, раздел 12)."""
    if manifest is None:
        return SHOT_STATE_MISSING

    if shot_duration_mismatches_scene(shots_duration_s, scene_spec_duration_s):
        return SHOT_STATE_STALE

    if not is_manifest_current(
        manifest, duration_s=scene_spec_duration_s, fps=fps, spec_hash=spec_hash, resolution=resolution
    ):
        return SHOT_STATE_STALE

    for key in ("junction_with_prev", "junction_with_next"):
        junction = manifest.get(key)
        if isinstance(junction, dict) and junction.get("status") == "failed":
            return SHOT_STATE_JUNCTION_ERROR

    return SHOT_STATE_RENDERED


def utc_now_iso() -> str:
    """UTC-метка с суффиксом Z, раздел 10.2: без микросекунд, единый формат
    с `blockout_rendered_at`/`manifest.json`."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso_utc(ts: Any) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_p10_mismatched(manifest: Optional[Dict[str, Any]], image_mtimes: Sequence[float]) -> bool:
    """P10 (раздел 19.2/20.2): изображения кадра старше `blockout_rendered_at`
    (здесь читается как `manifest.json:rendered_at` — то же значение,
    записывается рендерером одновременно, раздел 10.2), и человек с
    расхождением ещё не согласился (`p10_acknowledged` пусто/`null`).
    `image_mtimes` — `os.path.getmtime()` СУЩЕСТВУЮЩИХ файлов `output_path`
    шота; отсутствующие файлы в сравнение не входят."""
    if not manifest:
        return False
    if manifest.get("p10_acknowledged"):
        return False
    rendered_at = parse_iso_utc(manifest.get("rendered_at"))
    if rendered_at is None or not image_mtimes:
        return False
    return any(datetime.fromtimestamp(mt, tz=timezone.utc) < rendered_at for mt in image_mtimes)


def files_to_delete_for_regenerate(
    shots_items: List[Dict[str, Any]], selected_keys: Sequence[Tuple[Any, Any]]
) -> List[str]:
    """Раздел 19.2: файлы на удаление перед «Перегенерировать выбранные
    шоты» — начальный и конечный кадр КАЖДОГО выбранного шота, пути из
    `shots.json`. Связанные шоты (`link_type: full_copy`) добавлять не
    нужно — они подхватят перерисованный кадр копированием сами
    (`artist_batch_edit.py`, раздел 19.2), поэтому здесь нет отдельной
    обработки `link_type`: список — это ровно `output_path` выбранных
    элементов, ничего больше."""
    selected = set(selected_keys)
    paths: List[str] = []
    for item in shots_items:
        if not isinstance(item, dict):
            continue
        key = (item.get("scene_number"), item.get("shot_number"))
        if key not in selected:
            continue
        output_path = item.get("output_path")
        if output_path:
            paths.append(output_path)
    return paths


def write_manifest_atomic(manifest_path: Path, manifest: Dict[str, Any]) -> None:
    """Раздел 19.2, «Оставить как есть»: атомарная запись (временный файл +
    `os.replace`) БЕЗ sidecar-блокировки — кнопка неактивна на всё время
    любого прогона (раздел 18.4), второго писателя у файла вне прогона нет."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = manifest_path.with_name(f"{manifest_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, manifest_path)


def blender_binary_path() -> Optional[str]:
    """Раздел 18.4, «Открыть .blend»: `BLOCKOUT_BLENDER_BIN`, иначе `blender`
    в `PATH`. Возвращает None, если ничего не найдено — кнопка неактивна."""
    configured = os.environ.get("BLOCKOUT_BLENDER_BIN")
    if configured and Path(configured).is_file() and os.access(configured, os.X_OK):
        return configured
    return shutil.which("blender")


# =============================================================================
# BlockoutPanel
# =============================================================================


class BlockoutPanel(ttk.Frame):
    """Вкладка «Болванка» (раздел 18.4). Регистрируется в main_window.py
    последней, использует общую блокировку generation_panel.is_generating —
    своего замка не заводит (раздел 18.4, «Кнопки вкладки обязаны выставлять
    и снимать тот же флаг тем же способом»)."""

    def __init__(self, parent, generation_panel):
        super().__init__(parent)
        self.generation_panel = generation_panel
        self.current_project = None
        self.file_manager: Optional[FileManager] = None
        self.media_processor: Optional[MediaProcessor] = None

        self._has_checkpoint = False
        self._shots_items: List[Dict[str, Any]] = []
        self._chains_data: Dict[str, Any] = {}
        self._scene_spec: Dict[str, Any] = {}
        self._report: Dict[str, Any] = {}
        self._shot_rows: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self._tree_shot_keys: Dict[str, Tuple[int, int]] = {}
        self._tree_chain_ids: Dict[str, str] = {}
        self._comparison_generation = 0

        self.create_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def create_ui(self):
        self.columnconfigure(0, weight=1, minsize=280)
        self.columnconfigure(1, weight=2)
        self.rowconfigure(0, weight=1)

        self._create_tree_area(self)
        self._create_comparison_and_players(self)
        self._create_buttons(self)

    def _create_tree_area(self, parent):
        left = ttk.Frame(parent)
        left.grid(row=0, column=0, sticky="nsew", padx=(5, 2), pady=5)
        left.rowconfigure(0, weight=3)
        left.rowconfigure(2, weight=1)
        left.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(left, columns=("state", "p10"), selectmode="extended")
        self.tree.heading("#0", text="Сцена / цепочка / шот")
        self.tree.heading("state", text="Состояние")
        self.tree.heading("p10", text="")
        self.tree.column("p10", width=24, minwidth=24, stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        tree_scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tree_scroll.set)

        ttk.Label(left, text="Замечания выбранного шота/цепочки:").grid(row=1, column=0, sticky="w", pady=(5, 0))

        self.notes_list = tk.Listbox(left, height=6)
        self.notes_list.grid(row=2, column=0, sticky="nsew")
        notes_scroll = ttk.Scrollbar(left, orient="vertical", command=self.notes_list.yview)
        notes_scroll.grid(row=2, column=1, sticky="ns")
        self.notes_list.configure(yscrollcommand=notes_scroll.set)

        self.duration_label = ttk.Label(left, text="Длительность шота:")
        self.duration_label.grid(row=3, column=0, sticky="w", pady=(5, 0))
        duration_row = ttk.Frame(left)
        duration_row.grid(row=4, column=0, sticky="w")
        self.duration_combo = ttk.Combobox(duration_row, state="disabled", width=8)
        self.duration_combo.pack(side="left")
        self.duration_apply_btn = ttk.Button(
            duration_row, text="Применить", command=self._on_duration_apply, state="disabled"
        )
        self.duration_apply_btn.pack(side="left", padx=(5, 0))

    def _create_comparison_and_players(self, parent):
        right = ttk.Frame(parent)
        right.grid(row=0, column=1, sticky="nsew", padx=(2, 5), pady=5)
        right.rowconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        # Раздел 18.4: "болванка начало │ изображение начала │ изображение конца │ болванка конец"
        compare = ttk.LabelFrame(right, text="Сравнение")
        compare.grid(row=0, column=0, sticky="nsew", pady=(0, 5))
        for i in range(4):
            compare.columnconfigure(i, weight=1)

        self._comparison_canvases: Dict[str, tk.Canvas] = {}
        for i, (key, title) in enumerate((
            ("blockout_start", "болванка начало"),
            ("image_start", "изображение начала"),
            ("image_end", "изображение конца"),
            ("blockout_end", "болванка конец"),
        )):
            cell = ttk.Frame(compare)
            cell.grid(row=0, column=i, sticky="nsew", padx=2, pady=2)
            ttk.Label(cell, text=title).pack()
            canvas = tk.Canvas(cell, width=200, height=150, bg="#2b2b2b", highlightthickness=0)
            canvas.pack()
            self._comparison_canvases[key] = canvas

        players = ttk.LabelFrame(right, text="Видео")
        players.grid(row=1, column=0, sticky="nsew")
        players.columnconfigure(0, weight=1)
        players.columnconfigure(1, weight=1)
        players.rowconfigure(1, weight=1)

        ttk.Label(players, text="Опорный ролик болванки").grid(row=0, column=0)
        ttk.Label(players, text="Готовое видео").grid(row=0, column=1)

        ref_canvas = tk.Canvas(players, bg="#2b2b2b", highlightthickness=0)
        ref_canvas.grid(row=1, column=0, sticky="nsew", padx=2, pady=2)
        ref_controls = ttk.Frame(players)
        ref_controls.grid(row=2, column=0)
        self.ref_player = VideoPlayer(ref_canvas, ref_controls)

        final_canvas = tk.Canvas(players, bg="#2b2b2b", highlightthickness=0)
        final_canvas.grid(row=1, column=1, sticky="nsew", padx=2, pady=2)
        final_controls = ttk.Frame(players)
        final_controls.grid(row=2, column=1)
        self.final_player = VideoPlayer(final_canvas, final_controls)

    def _create_buttons(self, parent):
        row = ttk.Frame(parent)
        row.grid(row=1, column=0, columnspan=2, sticky="ew", padx=5, pady=5)

        self.redraw_shot_btn = ttk.Button(row, text="Перерисовать болванку шота", command=self.redraw_shot)
        self.redraw_shot_btn.pack(side="left", padx=2)

        self.redraw_chain_btn = ttk.Button(row, text="Перерисовать цепочку", command=self.redraw_chain)
        self.redraw_chain_btn.pack(side="left", padx=2)

        self.rebuild_scene_btn = ttk.Button(row, text="Пересобрать сцену", command=self.rebuild_scene)
        self.rebuild_scene_btn.pack(side="left", padx=2)

        self.open_blend_btn = ttk.Button(row, text="Открыть .blend", command=self.open_blend)
        self.open_blend_btn.pack(side="left", padx=2)

        self.build_preview_btn = ttk.Button(row, text="Собрать превью", command=self.build_preview)
        self.build_preview_btn.pack(side="left", padx=2)

        self.regenerate_btn = ttk.Button(
            row, text="Перегенерировать выбранные шоты", command=self.regenerate_selected_shots
        )
        self.regenerate_btn.pack(side="left", padx=2)

        self.leave_as_is_btn = ttk.Button(row, text="Оставить как есть", command=self.leave_as_is)
        self.leave_as_is_btn.pack(side="left", padx=2)

        self.open_preview_btn = ttk.Button(row, text="Открыть превью", command=self.open_preview)
        self.open_preview_btn.pack(side="left", padx=2)

        # Раздел 18.4: «рядом обязательна пометка», что «Перерисовать болванку
        # шота» фактически пересчитывает всю цепочку — постоянная подпись
        # рядом с кнопками, а не диалог на каждый клик.
        redraw_note = ttk.Label(
            parent,
            text=(
                "«Перерисовать болванку шота» и «Перерисовать цепочку» пересчитывают "
                "всю цепочку целиком (раздел 10.2); разница — что сохраняется на диск: "
                "один шот или все шоты цепочки."
            ),
        )
        redraw_note.grid(row=2, column=0, columnspan=2, sticky="w", padx=5)

        self.hint_label = ttk.Label(parent, text="")
        self.hint_label.grid(row=3, column=0, columnspan=2, sticky="w", padx=5)

    # ------------------------------------------------------------------
    # Загрузка проекта и данных
    # ------------------------------------------------------------------

    def load_project(self, project):
        self.current_project = project
        self.file_manager = FileManager(project.project_id)
        self.media_processor = MediaProcessor(project.project_id)
        self._has_checkpoint = False
        self.refresh()

    def refresh(self):
        """Перечитывает данные проекта и перестраивает дерево. Вызывается и
        при первой загрузке проекта, и из main_window.py после завершения
        генерации (раздел 18.4, «before блока перезагрузки редактора»)."""
        if not self.current_project or not self.file_manager:
            return
        self._load_data()
        self._rebuild_tree()
        self._update_button_states()
        self._probe_checkpoint()

    def _load_data(self):
        shots_data = self.file_manager.load_json_file("shots") or {}
        self._shots_items = shots_data.get("items", []) if isinstance(shots_data, dict) else []
        self._chains_data = self.file_manager.load_json_file("chains") or {}
        self._scene_spec = self.file_manager.load_json_file("scene_spec") or {}
        self._report = self._read_report_json()

    def _read_report_json(self) -> Dict[str, Any]:
        path = self.file_manager.project_path / BLOCKOUT_DIR_NAME / "report.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _read_manifest(self, scene_number: int, shot_number: int) -> Optional[Dict[str, Any]]:
        path = self._manifest_path(scene_number, shot_number)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _manifest_path(self, scene_number: int, shot_number: int) -> Path:
        return (
            self.file_manager.project_path
            / BLOCKOUT_DIR_NAME
            / shot_dir_name(scene_number, shot_number)
            / "manifest.json"
        )

    def _chain_lookup(self) -> Dict[Tuple[int, int], Dict[str, Any]]:
        """{(scene_number, shot_number): {"chain_id", "duration_s"}} из
        `chains.json` — раздел 7.2: «по нему вкладка «Болванка» строит
        список цепочек», лёгкий файл, в отличие от `scene_spec.json`."""
        lookup: Dict[Tuple[int, int], Dict[str, Any]] = {}
        for chain in (self._chains_data.get("chains") or []):
            chain_id = chain.get("chain_id")
            scene_number = chain.get("scene_number")
            for shot in (chain.get("shots") or []):
                key = (scene_number, shot.get("shot_number"))
                lookup[key] = {"chain_id": chain_id, "duration_s": shot.get("duration_s")}
        return lookup

    def _scene_spec_chain_by_id(self, chain_id: str) -> Optional[Dict[str, Any]]:
        for chain in (self._scene_spec.get("chains") or []):
            if chain.get("chain_id") == chain_id:
                return chain
        return None

    def _shots_start_item(self, scene_number: int, shot_number: int) -> Optional[Dict[str, Any]]:
        for item in self._shots_items:
            if not isinstance(item, dict):
                continue
            if (
                item.get("scene_number") == scene_number
                and item.get("shot_number") == shot_number
                and item.get("shot_type") == "start"
            ):
                return item
        return None

    def _shots_pair(self, scene_number: int, shot_number: int) -> List[Dict[str, Any]]:
        return [
            item for item in self._shots_items
            if isinstance(item, dict)
            and item.get("scene_number") == scene_number
            and item.get("shot_number") == shot_number
        ]

    def _current_blockout_fps(self) -> Any:
        """«Текущее» fps для проверки актуальности (раздел 10.2) — из
        параметров запуска (бриф проекта), а не из scene_spec.json: если
        человек сменил blockout_fps на панели генерации и не пересобрал
        сцену, scene_spec.json ещё хранит старое значение и не заметил бы
        расхождения сам с собой."""
        brief = getattr(self.current_project, "brief_data", None) or {}
        return brief.get("blockout_fps")

    def _current_blockout_resolution(self) -> Optional[List[int]]:
        """«Текущее» разрешение для проверки актуальности (раздел 10.2), тем
        же путём, что и `_current_blockout_fps()` — из настройки
        `blockout_resolution` брифа проекта (строка `"WxH"`,
        `gui/generation_panel.py`), а не пересчитанное под 16.2 (см.
        docstring `is_manifest_current()`)."""
        brief = getattr(self.current_project, "brief_data", None) or {}
        return parse_resolution_setting(brief.get("blockout_resolution"))

    def _build_shot_rows(self) -> Dict[Tuple[int, int], Dict[str, Any]]:
        chain_lookup = self._chain_lookup()
        fps = self._current_blockout_fps()
        resolution = self._current_blockout_resolution()
        rows: Dict[Tuple[int, int], Dict[str, Any]] = {}
        seen_keys = set()
        for item in self._shots_items:
            if not isinstance(item, dict):
                continue
            key = (item.get("scene_number"), item.get("shot_number"))
            if key in seen_keys:
                continue
            seen_keys.add(key)

            manifest = self._read_manifest(*key)
            chain_info = chain_lookup.get(key)
            scene_spec_duration = chain_info.get("duration_s") if chain_info else None
            start_item = self._shots_start_item(*key)
            shots_duration = start_item.get("duration_s") if start_item else None

            spec_hash = None
            if chain_info:
                chain_dict = self._scene_spec_chain_by_id(chain_info["chain_id"])
                if chain_dict:
                    spec_hash = compute_spec_hash(chain_dict)

            state = compute_shot_state(
                manifest,
                shots_duration_s=shots_duration,
                scene_spec_duration_s=scene_spec_duration,
                fps=fps,
                spec_hash=spec_hash,
                resolution=resolution,
            )
            duration_mismatch = shot_duration_mismatches_scene(shots_duration, scene_spec_duration)

            image_mtimes = []
            for pair_item in self._shots_pair(*key):
                out_path = pair_item.get("output_path")
                if out_path and Path(out_path).is_file():
                    image_mtimes.append(Path(out_path).stat().st_mtime)
            p10 = is_p10_mismatched(manifest, image_mtimes)

            rows[key] = {
                "scene_number": key[0],
                "shot_number": key[1],
                "chain_id": chain_info.get("chain_id") if chain_info else None,
                "state": state,
                "duration_mismatch": duration_mismatch,
                "p10_mismatched": p10,
                "manifest": manifest,
            }
        return rows

    # ------------------------------------------------------------------
    # Дерево
    # ------------------------------------------------------------------

    def _rebuild_tree(self):
        self._shot_rows = self._build_shot_rows()
        self._tree_shot_keys = {}
        self._tree_chain_ids = {}

        for item_id in self.tree.get_children(""):
            self.tree.delete(item_id)

        by_scene: Dict[int, List[Tuple[int, int]]] = {}
        for key in sorted(self._shot_rows.keys()):
            by_scene.setdefault(key[0], []).append(key)

        for scene_number in sorted(by_scene.keys()):
            scene_iid = f"scene_{scene_number}"
            self.tree.insert("", "end", iid=scene_iid, text=f"Сцена {scene_number}", open=True)

            by_chain: Dict[Optional[str], List[Tuple[int, int]]] = {}
            for key in by_scene[scene_number]:
                chain_id = self._shot_rows[key]["chain_id"]
                by_chain.setdefault(chain_id, []).append(key)

            for chain_id, keys in by_chain.items():
                if chain_id:
                    chain_iid = f"chain_{chain_id}"
                    self.tree.insert(scene_iid, "end", iid=chain_iid, text=f"Цепочка {chain_id}", open=True)
                    self._tree_chain_ids[chain_iid] = chain_id
                    parent_iid = chain_iid
                else:
                    parent_iid = scene_iid

                for key in keys:
                    row = self._shot_rows[key]
                    shot_iid = f"shot_{key[0]}_{key[1]}"
                    state_label = SHOT_STATE_LABELS.get(row["state"], row["state"])
                    p10_mark = "⚠" if row["p10_mismatched"] else ""
                    self.tree.insert(
                        parent_iid, "end", iid=shot_iid,
                        text=f"Шот {key[1]}", values=(state_label, p10_mark),
                    )
                    self._tree_shot_keys[shot_iid] = key

    def _on_tree_select(self, _event=None):
        keys = self._get_selected_shot_keys()
        if keys:
            primary = self._get_primary_selected_shot_key()
            self._update_comparison_area(*primary)
            self._update_video_players(*primary)
            self._update_notes_strip(shot_key=primary)
            self._update_duration_picker(primary)
        else:
            chain_id = self._get_selected_chain_id()
            self._update_notes_strip(chain_id=chain_id)
            self._update_duration_picker(None)
        self._update_button_states()

    def _get_selected_shot_keys(self) -> List[Tuple[int, int]]:
        keys = []
        for iid in self.tree.selection():
            key = self._tree_shot_keys.get(iid)
            if key:
                keys.append(key)
        return keys

    def _get_primary_selected_shot_key(self) -> Optional[Tuple[int, int]]:
        selection = self.tree.selection()
        if not selection:
            return None
        return self._tree_shot_keys.get(selection[0])

    def _get_selected_chain_id(self) -> Optional[str]:
        selection = self.tree.selection()
        if not selection:
            return None
        return self._tree_chain_ids.get(selection[0])

    # ------------------------------------------------------------------
    # Полоса замечаний (раздел 18.4, читает 93_blockout/report.json)
    # ------------------------------------------------------------------

    def _report_checks(self) -> List[Dict[str, Any]]:
        checks: List[Dict[str, Any]] = []
        for section in self._report.values():
            if isinstance(section, dict):
                checks.extend(section.get("checks") or [])
        return checks

    def _update_notes_strip(self, shot_key: Optional[Tuple[int, int]] = None, chain_id: Optional[str] = None):
        self.notes_list.delete(0, "end")
        checks = self._report_checks()
        lines: List[str] = []
        if shot_key:
            wanted_shot_key = shot_dir_name(*shot_key)
            for c in checks:
                if c.get("shot_key") == wanted_shot_key or (
                    c.get("scene_number") == shot_key[0] and c.get("shot_number") == shot_key[1]
                ):
                    lines.append(self._format_check_line(c))
            row = self._shot_rows.get(shot_key)
            manifest = row.get("manifest") if row else None
            if manifest:
                for w in (manifest.get("warnings") or []):
                    lines.append(str(w))
        elif chain_id:
            for c in checks:
                if c.get("chain_id") == chain_id:
                    lines.append(self._format_check_line(c))
        for line in lines:
            self.notes_list.insert("end", line)

    @staticmethod
    def _format_check_line(check: Dict[str, Any]) -> str:
        code = check.get("code", "")
        obj = check.get("shot_key") or check.get("chain_id") or ""
        reason = check.get("message", "")
        return f"{code} | {obj} | {reason}"

    # ------------------------------------------------------------------
    # Сравнение изображений
    # ------------------------------------------------------------------

    def _update_comparison_area(self, scene_number: int, shot_number: int):
        self._comparison_generation += 1
        generation = self._comparison_generation

        start_item = self._shots_start_item(scene_number, shot_number)
        end_item = None
        for item in self._shots_pair(scene_number, shot_number):
            if item.get("shot_type") == "end":
                end_item = item
                break

        paths = {
            "blockout_start": start_item.get("blockout_ref_image") if start_item else None,
            "image_start": start_item.get("output_path") if start_item else None,
            "image_end": end_item.get("output_path") if end_item else None,
            "blockout_end": end_item.get("blockout_ref_image") if end_item else None,
        }
        for key, canvas in self._comparison_canvases.items():
            canvas.delete("all")
            path = paths.get(key)
            if path:
                self._load_comparison_image(canvas, path, generation)

    def _load_comparison_image(self, canvas: tk.Canvas, path: str, generation: int):
        if not self.media_processor:
            return

        def _load():
            try:
                if not Path(path).exists():
                    return
                photo = self.media_processor.load_image_for_display(path, max_size=(200, 150))
            except Exception as exc:
                logger.warning(f"Не удалось загрузить изображение сравнения {path}: {exc}")
                return
            if photo:
                self.after(0, lambda: self._on_comparison_image_loaded(canvas, photo, generation))

        threading.Thread(target=_load, daemon=True).start()

    def _on_comparison_image_loaded(self, canvas: tk.Canvas, photo, generation: int):
        if generation != self._comparison_generation:
            return  # выбор сменился, пока грузилась картинка
        canvas._blockout_photo = photo  # хранит ссылку, чтобы PhotoImage не собрал GC
        canvas.delete("all")
        canvas.create_image(100, 75, image=photo, anchor="center")

    # ------------------------------------------------------------------
    # Видеоплееры
    # ------------------------------------------------------------------

    def _update_video_players(self, scene_number: int, shot_number: int):
        start_item = self._shots_start_item(scene_number, shot_number)
        self.ref_player.stop()
        self.final_player.stop()
        if start_item:
            blockout_video = start_item.get("blockout_video")
            if blockout_video:
                self.ref_player.load(blockout_video)
            video_path = start_item.get("video_path")
            if video_path:
                self.final_player.load(video_path)

    # ------------------------------------------------------------------
    # Выбор длительности (раздел 18.4, критерий A24)
    # ------------------------------------------------------------------

    def _allowed_durations(self) -> List[int]:
        """Раздел 6.1: переиспользует уже собранный на панели генерации
        набор допустимых длительностей (`blockout_durations_vars`) вместо
        повторного чтения `97_shots/video_model_caps.json` — сама панель
        читает тот же файл при `load_project()`."""
        vars_ = getattr(self.generation_panel, "blockout_durations_vars", {}) or {}
        return sorted(vars_.keys())

    def _duration_editing_allowed(self) -> bool:
        """Раздел 18.4: доступно только при наличии checkpoint и пока НЕ
        выполняется сам screenplay_shots_generator (справочник ручных
        длительностей он снимает с диска один раз, в начале шага)."""
        if not self._has_checkpoint:
            return False
        last_step = getattr(self.generation_panel, "_last_running_step_id", None)
        return last_step != "screenplay_shots_generator"

    def _update_duration_picker(self, shot_key: Optional[Tuple[int, int]]):
        allowed = self._allowed_durations()
        enabled = bool(shot_key) and bool(allowed) and self._duration_editing_allowed()
        state = "readonly" if enabled else "disabled"
        self.duration_combo.configure(values=allowed, state=state)
        self.duration_apply_btn.configure(state="normal" if enabled else "disabled")
        if shot_key:
            start_item = self._shots_start_item(*shot_key)
            if start_item and start_item.get("duration_s") is not None:
                self.duration_combo.set(start_item.get("duration_s"))

    def _on_duration_apply(self):
        shot_key = self._get_primary_selected_shot_key()
        if not shot_key or not self._duration_editing_allowed():
            return
        try:
            new_duration = int(self.duration_combo.get())
        except (TypeError, ValueError):
            messagebox.showerror("Ошибка", "Выберите длительность из списка")
            return
        self._apply_manual_duration(shot_key, new_duration)

    def _apply_manual_duration(self, shot_key: Tuple[int, int], new_duration_s: int):
        """Раздел 18.4: пишет `duration_s`/`duration_source`/`timing` (и, при
        отсутствии, `duration_requested_s`) во ВСЕ элементы шота — start и,
        если есть, end. Идёт через `FileManager.save_json_file("shots")`,
        то есть по общему протоколу раздела 10.2 (flock sidecar,
        перечитывание, слияние по полям, os.replace, критерий A42)."""
        data = self.file_manager.load_json_file("shots") or {"items": []}
        items = data.get("items", [])
        touched = False
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("scene_number") == shot_key[0] and item.get("shot_number") == shot_key[1]:
                item.update(build_manual_duration_update(item, new_duration_s))
                touched = True
        if not touched:
            return
        data["items"] = items
        if not self.file_manager.save_json_file(data, "shots"):
            messagebox.showerror("Ошибка", "Не удалось сохранить длительность шота")
            return
        self.refresh()

    # ------------------------------------------------------------------
    # Проверка checkpoint (раздел 18.4: без него неактивны все кнопки запуска)
    # ------------------------------------------------------------------

    async def _resolve_has_checkpoint(self, project_id: str) -> bool:
        """core/pipeline_runner.py::_get_latest_project_checkpoint —
        "приватный" метод, но публичной обёртки для одной лишь проверки
        наличия checkpoint у PipelineRunner нет, а rerun_single_step() делает
        внутри себя ровно эту же проверку (раздел 18.4). `pipeline_runner.py`
        вне области этой правки (параллельно правится другим агентом),
        поэтому обращение защищено `getattr`: если метод переименуют или
        уберут, вкладка не падает, а тихо считает checkpoint отсутствующим
        (кнопки запуска останутся неактивны — безопасная деградация, раздел
        18.4)."""
        getter = getattr(self.generation_panel.pipeline_runner, "_get_latest_project_checkpoint", None)
        if getter is None:
            logger.warning(
                f"PipelineRunner не предоставляет _get_latest_project_checkpoint — "
                f"проверка checkpoint для проекта {project_id} пропущена"
            )
            return False
        checkpoint = await getter(project_id)
        return bool(checkpoint and getattr(checkpoint, "context", None))

    def _probe_checkpoint(self):
        project_id = self.current_project.project_id if self.current_project else None
        if not project_id:
            return

        def _worker():
            has_checkpoint = False
            try:
                import asyncio
                loop = asyncio.new_event_loop()
                try:
                    has_checkpoint = loop.run_until_complete(self._resolve_has_checkpoint(project_id))
                finally:
                    loop.close()
            except Exception as exc:
                logger.warning(f"Не удалось проверить checkpoint проекта {project_id}: {exc}")
            self.after(0, lambda: self._on_checkpoint_result(project_id, has_checkpoint))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_checkpoint_result(self, project_id: str, has_checkpoint: bool):
        if not self.current_project or self.current_project.project_id != project_id:
            return  # проект уже сменился, результат устарел
        self._has_checkpoint = has_checkpoint
        self._update_button_states()
        self._update_duration_picker(self._get_primary_selected_shot_key())

    # ------------------------------------------------------------------
    # Состояния кнопок
    # ------------------------------------------------------------------

    def _update_button_states(self):
        is_generating = bool(getattr(self.generation_panel, "is_generating", False))
        has_checkpoint = self._has_checkpoint
        shot_keys = self._get_selected_shot_keys()
        primary = self._get_primary_selected_shot_key()

        launch_ok = has_checkpoint and not is_generating

        primary_row = self._shot_rows.get(primary) if primary else None
        duration_blocked = bool(primary_row and primary_row.get("duration_mismatch"))

        self.redraw_shot_btn.configure(
            state="normal" if (launch_ok and primary and not duration_blocked) else "disabled"
        )
        self.redraw_chain_btn.configure(
            state="normal" if (launch_ok and primary_row and primary_row.get("chain_id") and not duration_blocked)
            else "disabled"
        )
        self.rebuild_scene_btn.configure(state="normal" if launch_ok else "disabled")
        self.build_preview_btn.configure(state="normal" if launch_ok else "disabled")

        blend_path = self._chain_blend_path(primary_row["chain_id"]) if primary_row and primary_row.get("chain_id") else None
        self.open_blend_btn.configure(
            state="normal" if (primary and blend_path and blend_path.is_file() and blender_binary_path()) else "disabled"
        )

        self.regenerate_btn.configure(state="normal" if (launch_ok and shot_keys) else "disabled")

        any_p10 = any(self._shot_rows.get(k, {}).get("p10_mismatched") for k in shot_keys)
        self.leave_as_is_btn.configure(state="normal" if (not is_generating and shot_keys and any_p10) else "disabled")

        preview_path = self._preview_path()
        self.open_preview_btn.configure(state="normal" if preview_path else "disabled")

        if not has_checkpoint:
            self.hint_label.configure(text="Проекту нужен полный прогон — сохранённый checkpoint не найден")
        elif duration_blocked:
            self.hint_label.configure(text="Сначала пересоберите сцену — длительность шота изменена вручную")
        elif is_generating:
            self.hint_label.configure(text="Выполняется прогон — дождитесь завершения")
        else:
            self.hint_label.configure(text="")

    def _chain_blend_path(self, chain_id: str) -> Path:
        return self.file_manager.project_path / BLOCKOUT_DIR_NAME / chain_id / "chain.blend"

    def _preview_path(self) -> Optional[Path]:
        if not self.file_manager:
            return None
        preview_dir = self.file_manager.project_path / BLOCKOUT_DIR_NAME / "preview"
        burnin = preview_dir / "blockout_all_burnin.mp4"
        if burnin.is_file():
            return burnin
        plain = preview_dir / "blockout_all.mp4"
        if plain.is_file():
            return plain
        return None

    # ------------------------------------------------------------------
    # Кнопки
    # ------------------------------------------------------------------

    def redraw_shot(self):
        """Раздел 10.2: считает всю цепочку, но сохраняет файлы только этого
        шота — постоянное пояснение у кнопок ставит _create_buttons()."""
        primary = self._get_primary_selected_shot_key()
        if not primary:
            return
        scope = f"scene_{primary[0]:02d}_shot_{primary[1]:02d}"
        self.generation_panel.run_blockout_scoped_step("blockout_renderer", scope)

    def redraw_chain(self):
        primary_row = self._shot_rows.get(self._get_primary_selected_shot_key())
        chain_id = primary_row.get("chain_id") if primary_row else None
        if not chain_id:
            return
        scope = f"chain_{chain_id}"
        self.generation_panel.run_blockout_scoped_step("blockout_renderer", scope)

    def rebuild_scene(self):
        self.generation_panel.run_blockout_scoped_step("blockout_scene_builder", "all")

    def build_preview(self):
        self.generation_panel.run_blockout_scoped_step("blockout_preview", "all")

    def open_blend(self):
        primary_row = self._shot_rows.get(self._get_primary_selected_shot_key())
        chain_id = primary_row.get("chain_id") if primary_row else None
        if not chain_id:
            return
        blend_path = self._chain_blend_path(chain_id)
        blender_bin = blender_binary_path()
        if not blender_bin or not blend_path.is_file():
            return
        try:
            subprocess.Popen([blender_bin, str(blend_path)])
        except OSError as exc:
            messagebox.showerror("Ошибка", f"Не удалось запустить Blender:\n{exc}")

    def regenerate_selected_shots(self):
        shot_keys = self._get_selected_shot_keys()
        if not shot_keys:
            return
        paths = files_to_delete_for_regenerate(self._shots_items, shot_keys)
        if not paths:
            return
        listing = "\n".join(paths)
        if not messagebox.askyesno(
            "Перегенерировать выбранные шоты",
            f"Будут удалены и заново сгенерированы {len(paths)} файлов изображений:\n\n{listing}\n\nПродолжить?",
        ):
            return
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(f"Не удалось удалить {p}: {exc}")
        self.generation_panel.run_blockout_scoped_step("artist_batch_shots", "all")

    def leave_as_is(self):
        """Раздел 19.2, второй путь: снимает P10 у выбранных рассогласованных
        шотов, записывая `p10_acknowledged` в их `manifest.json` — атомарно,
        без sidecar-блокировки (кнопка неактивна на время любого прогона)."""
        shot_keys = [k for k in self._get_selected_shot_keys() if self._shot_rows.get(k, {}).get("p10_mismatched")]
        if not shot_keys:
            return
        now = utc_now_iso()
        for key in shot_keys:
            manifest = self._shot_rows[key].get("manifest")
            if not manifest:
                continue
            manifest = dict(manifest)
            manifest["p10_acknowledged"] = now
            write_manifest_atomic(self._manifest_path(*key), manifest)
        self.refresh()

    def open_preview(self):
        preview_path = self._preview_path()
        if not preview_path:
            return
        top = tk.Toplevel(self)
        top.title("Превью болванки")
        canvas = tk.Canvas(top, width=640, height=360, bg="#2b2b2b", highlightthickness=0)
        canvas.pack()
        controls = ttk.Frame(top)
        controls.pack()
        player = VideoPlayer(canvas, controls)
        player.load(str(preview_path))
        player.play()

        def _on_close():
            player.stop()
            top.destroy()

        top.protocol("WM_DELETE_WINDOW", _on_close)
