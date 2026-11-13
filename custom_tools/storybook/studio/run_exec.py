"""Исполнитель прогона по плану: замок, control.json, журналы, хуки движка, пауза и отмена.

Общий для Tk и React. Хуки ставятся на экземпляр движка один раз и ищут сессию по
`workflow_id`, поэтому параллельные прогоны разных проектов не мешают друг другу.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import importlib
import json
import logging
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from custom_tools.storybook import project_paths

from . import run_control, run_logs, run_plan
from .errors import StudioError
from .step_runs import StepRunLog, compute_step_input_hash

logger = logging.getLogger(__name__)

_PACKAGE = "custom_tools.storybook.studio"
POLL_S = 1.0
HEARTBEAT_S = 10.0
PROTECT_STEPS = frozenset({"artist_batch_shots", "video_generator"})
_ENGINE: dict[str, Any] = {}
_ENGINE_LOCK = threading.Lock()
_SESSIONS: dict[str, "RunSession"] = {}
_SESSIONS_LOCK = threading.Lock()


def _studio(name: str):
    return importlib.import_module(f"{_PACKAGE}.{name}")


def get_engine():
    """Кэшируемая фабрика движка; в тестах подменяется `set_engine`."""
    with _ENGINE_LOCK:
        if "engine" not in _ENGINE:
            from workflow.enhanced_engine import EnhancedWorkflowEngine
            _ENGINE["engine"] = EnhancedWorkflowEngine()
        return _ENGINE["engine"]


def set_engine(engine) -> None:
    with _ENGINE_LOCK:
        if engine is None:
            _ENGINE.pop("engine", None)
        else:
            _ENGINE["engine"] = engine


def _status_str(result: Any) -> str:
    status = getattr(result, "status", None)
    return str(getattr(status, "value", None) or status or "").lower()


def build_response(result: Any, payload: dict) -> dict:
    """Тот же контракт, что `PipelineRunner._build_runner_response`."""
    status = _status_str(result) or None
    if status == "cancelled":
        return {**payload, "status": "cancelled", "result": result}
    if status in (None, "completed"):
        return {**payload, "status": "success", "result": result}
    message = getattr(result, "error", None) or f"Workflow завершился со статусом '{status}'"
    return {"status": "error", "message": message, "result": result}


class RunSession:
    """Состояние одного прогона: управляющие файлы, журнал шагов, прогресс."""

    def __init__(self, engine, plan: dict, root: Path, *, run_id: str, surface: str, launcher: str,
                 progress_callback: Optional[Callable] = None, poll_s: float = POLL_S,
                 heartbeat_s: float = HEARTBEAT_S) -> None:
        self.engine = engine
        self.plan = plan
        self.root = Path(root)
        self.run_id = run_id
        self.surface = surface
        self.launcher = launcher
        self.workflow_id = plan["workflow_id"]
        self.progress_callback = progress_callback
        self.poll_s = poll_s
        self.heartbeat_s = heartbeat_s
        self.total = len(plan["steps_to_run"])
        self.running: list[str] = []
        self.finished: set[str] = set()
        self._failed_notified: set[str] = set()
        self.started_ts = time.time()
        self.cancel_sent = False
        self.paused_saved = False
        self.log: Optional[run_logs.RunLog] = None
        self.step_log: Optional[StepRunLog] = None
        self._artifacts: dict[str, list[str]] = {}
        self._stale_prepared: dict[str, list[str]] = {}
        self._stale_completed: set[str] = set()
        self._stale_archived_paths: set[str] = set()
        self._stale_inplace: dict[str, dict[str, tuple[int, int]]] = {}
        self._stale_inputs_ok: dict[str, bool] = {}
        self._series_revision = None

    # ---- начало и конец
    def begin(self) -> None:
        run_control.control_create(self.root, self.run_id, self.surface, self.workflow_id)
        self.step_log = StepRunLog(self.root, self.run_id, self.workflow_id)
        self.log = run_logs.RunLog(self.root, self.run_id)
        self._artifacts = run_plan.step_output_artifacts()
        from .series import _read
        series = _read(self.root / "series.json")
        self._series_revision = {key: copy.deepcopy(series.get(key)) for key in ("source_material", "participant_ids", "settings", "snapshot", "input_revision", "sound_revision")}
        self._archive_overwritten()
        now = time.time()
        run_control.write_active(self.root, {
            "version": 1, "run_id": self.run_id, "workflow_id": self.workflow_id, "mode": self.plan["mode"],
            "surface": self.surface, "launcher": self.launcher, "pid": os.getpid(), "host": socket.gethostname(),
            "started_at": run_control.iso(now), "started_ts": now, "heartbeat_ts": now,
            "heartbeat_at": run_control.iso(now), "state": "running", "step_id": None, "running": [],
            "step_index": 0, "step_total": self.total, "percent": 0.0, "message": "Прогон начат",
            "steps": list(self.plan["steps_to_run"]), "outcome": None, "error": None,
            "regen_id": self.plan.get("_regen_id"),
        })
        self.event(f"Прогон {self.run_id} ({self.plan['mode']}) начат, шагов: {self.total}")

    def _archive_overwritten(self) -> None:
        """Упавший прогон без итога, который затирает наш begin, попадает в историю как прерванный."""
        with contextlib.suppress(Exception):
            prev = run_control.read_active(self.root)
            if not prev.get("run_id") or prev["run_id"] == self.run_id or prev.get("outcome") \
                    or prev.get("state") == "queued":
                return
            if any(r.get("run_id") == prev["run_id"] for r in run_control.read_history(self.root)):
                return
            run_control.append_history(self.root, {
                "run_id": prev["run_id"], "workflow_id": prev.get("workflow_id"), "mode": prev.get("mode"),
                "surface": prev.get("surface"), "started_at": prev.get("started_at"),
                "finished_at": run_control.iso(), "outcome": "interrupted"})

    def event(self, text: str, level: str = "INFO") -> None:
        if self.log is not None:
            self.log.event(text, level)

    def _touch(self, **fields: Any) -> None:
        now = time.time()
        run_control.update_active(self.root, self.run_id, heartbeat_ts=now,
                                  heartbeat_at=run_control.iso(now), **fields)

    def _progress_fields(self, message: str) -> dict:
        done = len(self.finished)
        return {"running": list(self.running), "step_id": self.running[0] if self.running else None,
                "step_index": min(done + (1 if self.running else 0), self.total), "step_total": self.total,
                "percent": round(done / self.total * 100, 1) if self.total else 100.0, "message": message}

    # ---- шаги
    def step_started(self, step, context=None) -> None:
        if step.id in self.running:  # ретрай: шаг уже отмечен
            return
        self.running.append(step.id)
        variables = dict(getattr(context, "variables", None) or {})
        try:
            digest = compute_step_input_hash(step, variables, self.root, self._artifacts)
        except Exception:  # noqa: BLE001
            digest = None
        if self.step_log:
            self.step_log.mark_started(step.id, digest, time.time())
        self.event(f"Шаг «{step.id}» выполняется")
        self._touch(**self._progress_fields(f"Шаг '{step.id}' выполняется"))
        self._notify(message=f"Шаг '{step.id}' выполняется", step_id=step.id, step_status="running")

    def _prepare_stale_step(self, step_id: str) -> None:
        markers = self.plan.get("stale_steps", {}).get(step_id, [])
        if not markers:
            return
        from . import artifacts
        from .series import _read
        from .project_store import write_json_atomic
        series_path = self.root / "series.json"
        series = _read(series_path)
        confirmations = series.setdefault("stale_confirmations", {})
        try:
            self._stale_inputs_ok[step_id] = self._stale_inputs_ready(step_id, markers, series)
        except Exception as exc:
            logger.warning("Не удалось проверить актуальность входов шага %s: %s", step_id, exc)
            self._stale_inputs_ok[step_id] = False
        outputs, pipeline_steps = run_plan._active_outputs(self.root, self.plan.get("inputs"))
        for marker in markers:
            revision = {key: value for key, value in (self._series_revision or {}).items()
                        if key != "sound_revision" or marker.startswith("99_final") or marker.startswith("98_audio/previews")}
            entry = confirmations.get(marker)
            if not isinstance(entry, dict) or entry.get("revision") != revision:
                entry = {"revision": revision, "steps": [], "requirements": []}
                confirmations[marker] = entry
            entry["steps"] = [step for step in entry.get("steps", [])
                              if step != step_id and step_id not in run_plan.step_ancestors(step, pipeline_steps)]
            entry["requirements"] = list(dict.fromkeys(entry.get("requirements", []) + self.plan.get("stale_requirements", {}).get(marker, [])))
            expected = entry.setdefault("expected", {}).setdefault(step_id, [])
            for rel in outputs.get(step_id, []):
                if (self.root / rel).is_file() and run_plan._overlaps(marker, rel) and rel not in expected:
                    expected.append(rel)
        write_json_atomic(series_path, series)
        prepared = []
        inplace = {}
        if step_id == "artist_batch" and "50_images" in markers:
            import json
            from .image_edit import _backup_illustration
            images = (self.root / "50_images").resolve()
            try:
                items = json.loads((self.root / "50_items/items.json").read_text(encoding="utf-8"))
                targets = [(Path(item["output_path"]) if Path(item["output_path"]).is_absolute()
                            else self.root / item["output_path"]).resolve()
                           for item in items.get("items", []) if item.get("output_path")]
            except (OSError, ValueError, TypeError, KeyError):
                targets = [path.resolve() for path in images.rglob("*") if path.is_file()]
            for path in dict.fromkeys(targets):
                if not path.is_relative_to(images) or not path.is_file():
                    continue
                _backup_illustration(self.plan["project_id"], path)
                rel = path.relative_to(self.root).as_posix()
                entry = confirmations["50_images"]
                expected = entry.setdefault("expected", {}).setdefault(step_id, [])
                if rel not in expected:
                    expected.append(rel)
                write_json_atomic(series_path, series)
                path.unlink()
                self._stale_archived_paths.add(rel)
                prepared.append(rel)
        if step_id == "storybook_sfx_generator":
            rel = "98_audio/sfx_manifest.json"
            path = self.root / rel
            if path.is_file() and any(rel == marker or rel.startswith(marker + "/") for marker in markers):
                artifacts._snapshot(self.plan["project_id"], "sfx_manifest", path.read_bytes(), "external")
                stat = path.stat()
                inplace[rel] = (stat.st_mtime_ns, stat.st_size)
                prepared.append(rel)
        if step_id == "bible_builder":
            for key in ("characters", "locations"):
                spec = artifacts.ARTIFACTS[key]
                rel = spec.path
                path = self.root / rel
                if (any(rel == marker or rel.startswith(marker + "/") for marker in markers)
                        and path.is_file() and rel not in self._stale_archived_paths):
                    artifacts._snapshot(self.plan["project_id"], key, path.read_bytes(), "external")
                    path.unlink()
                    self._stale_archived_paths.add(rel)
                    prepared.append(rel)
            if prepared:
                (self.root / "20_bible/.shared_generation_complete").unlink(missing_ok=True)
                from .series import materialize_snapshot
                materialize_snapshot(self.plan["project_id"], self.plan["series_id"])
        for spec in artifacts.ARTIFACTS.values():
            if step_id not in spec.writers or spec.key in ("brief", "characters", "locations"):
                continue
            rel = spec.path
            if not any(rel == marker or rel.startswith(marker + "/") for marker in markers):
                continue
            path = self.root / rel
            if step_id == "style_keeper" and (self._series_revision or {}).get("snapshot", {}).get("style"):
                if path.is_file():
                    prepared.append(rel)
                continue
            if rel in self._stale_archived_paths:
                if path.is_file():
                    stat = path.stat()
                    inplace[rel] = (stat.st_mtime_ns, stat.st_size)
                prepared.append(rel)
            elif path.is_file():
                artifacts._snapshot(self.plan["project_id"], spec.key, path.read_bytes(), "external")
                # Эти шаги меняют существующий вход, а не создают его с нуля.
                if step_id not in ("shots_prompt_qa", "story_editor", "video_generator", "artist_batch_shots",
                                   "storybook_sfx_generator", "storybook_music_generator", "storybook_audio_subtitle"):
                    path.unlink()
                else:
                    stat = path.stat()
                    inplace[rel] = (stat.st_mtime_ns, stat.st_size)
                self._stale_archived_paths.add(rel)
                prepared.append(rel)
        # Для остальных JSON-выходов использовать ту же историю артефактов.
        active_outputs, _ = run_plan._active_outputs(self.root, self.plan.get("inputs"))
        registered = {spec.path for spec in artifacts.ARTIFACTS.values()}
        for rel in active_outputs.get(step_id, []):
            path = self.root / rel
            if (rel in registered or rel == "98_audio/speech.json" or rel.startswith("99_final/")
                    or not rel.endswith(".json") or not path.is_file()
                    or not any(rel == marker or rel.startswith(marker + "/") for marker in markers)):
                continue
            if rel not in self._stale_archived_paths:
                artifacts._snapshot(self.plan["project_id"], rel.replace("/", "_"), path.read_bytes(), "external")
                self._stale_archived_paths.add(rel)
            stat = path.stat()
            inplace[rel] = (stat.st_mtime_ns, stat.st_size)
            prepared.append(rel)
        if step_id in ("montage_assembler", "storybook_photo_montage"):
            from . import shot_versions
            targets = [rel for rel in self._artifacts.get(step_id, [])
                       if rel.startswith("99_final/") and (self.root / rel).is_file()
                       and rel not in self._stale_archived_paths]
            op = shot_versions.plan_area_op(self.plan["project_id"], "99_final", targets,
                                            reason="film", run_id=self.run_id, dedupe=False)
            if op:
                shot_versions.apply_op(self.plan["project_id"], op)
                for rel in targets:
                    (self.root / rel).unlink()
                    self._stale_archived_paths.add(rel)
                    prepared.append(rel)
        for marker in markers:
            entry = confirmations.get(marker, {})
            for rel in entry.get("expected", {}).get(step_id, []):
                if rel not in prepared and not (self.root / rel).exists():
                    prepared.append(rel)
        self._stale_prepared[step_id] = prepared
        self._stale_inplace[step_id] = inplace

    def _stale_inputs_ready(self, step_id: str, own_markers: list[str], series: dict) -> bool:
        outputs, steps = run_plan._active_outputs(self.root, self.plan.get("inputs"))
        ancestors = run_plan.step_ancestors(step_id, steps)
        info = run_plan.stale_results_for_plan(self.plan["project_id"], sorted(ancestors), inputs=self.plan.get("inputs"))
        own_outputs = outputs.get(step_id) or run_plan.step_output_artifacts().get(step_id, [])
        confirmations = series.get("stale_confirmations", {})
        for marker in info["affected_results"]:
            need = [step for step in info["stale_requirements"].get(marker, []) if step in ancestors]
            upstream_paths = [rel for ancestor in need for rel in outputs.get(ancestor, [])
                              if run_plan._overlaps(marker, rel) and (self.root / rel).exists()]
            # Несозданный чужой выход и общий файл редактора не делают
            # собственную отметку зависимостью самой от себя.
            if marker in own_markers and (not need or (upstream_paths and all(rel in own_outputs for rel in upstream_paths))):
                continue
            revision = {key: value for key, value in (self._series_revision or {}).items()
                        if key != "sound_revision" or marker.startswith("99_final") or marker.startswith("98_audio/previews")}
            entry = confirmations.get(marker, {})
            if (not need or entry.get("revision") != revision
                    or not all(step in entry.get("steps", []) for step in need)):
                return False
        return True

    def _complete_stale_step(self, step_id: str, status: str) -> None:
        prepared = self._stale_prepared.get(step_id, [])
        if step_id == "storybook_speech" and status == "completed":
            import json
            try:
                speech = json.loads((self.root / "98_audio/speech.json").read_text(encoding="utf-8"))
                if speech.get("status") in ("success", "silent"):
                    prepared = ["98_audio/speech.json"]
            except (OSError, ValueError):
                pass
        if status != "completed" or not prepared or not all((self.root / rel).is_file() for rel in prepared):
            return
        for rel, previous in self._stale_inplace.get(step_id, {}).items():
            stat = (self.root / rel).stat()
            if (stat.st_mtime_ns, stat.st_size) == previous:
                return
        self._stale_completed.add(step_id)
        from .series import _read
        from .project_store import write_json_atomic
        path = self.root / "series.json"
        series = _read(path)
        if any(series.get(key) != value for key, value in (self._series_revision or {}).items()
               if key != "sound_revision" or step_id == "montage_assembler"):
            if step_id == "storybook_speech":
                speech_path = self.root / "98_audio/speech.json"
                try:
                    speech = json.loads(speech_path.read_text(encoding="utf-8"))
                    speech["status"] = "stale"
                    write_json_atomic(speech_path, speech)
                except (OSError, ValueError):
                    pass
            return
        if not self._stale_inputs_ok.get(step_id, False):
            return
        confirmations = series.setdefault("stale_confirmations", {})
        for marker in self.plan.get("stale_steps", {}).get(step_id, []):
            revision = {key: value for key, value in (self._series_revision or {}).items()
                        if key != "sound_revision" or marker.startswith("99_final") or marker.startswith("98_audio/previews")}
            entry = confirmations.get(marker)
            if not isinstance(entry, dict) or entry.get("revision") != revision:
                entry = {"revision": revision, "steps": []}
                confirmations[marker] = entry
            if step_id not in entry["steps"]:
                entry["steps"].append(step_id)
        write_json_atomic(path, series)

    def _finalize_stale(self) -> None:
        from .series import _read
        from .project_store import write_json_atomic
        path = self.root / "series.json"
        series = _read(path)
        confirmations = series.get("stale_confirmations", {})
        requirements = self.plan.get("stale_requirements", {})
        stale = list(series.get("stale_results", []))
        for marker in list(stale):
            if marker.startswith("99_final"):
                continue
            revision = {key: value for key, value in (self._series_revision or {}).items()
                        if key != "sound_revision" or marker.startswith("98_audio/previews")}
            entry = confirmations.get(marker, {})
            if (all(series.get(key) == value for key, value in revision.items())
                    and entry.get("revision") == revision and requirements.get(marker)
                    and all(step in entry.get("steps", []) for step in requirements[marker])):
                stale.remove(marker)
                confirmations.pop(marker, None)
        series["stale_results"] = stale
        write_json_atomic(path, series)
        for marker in list(stale):
            if not marker.startswith("99_final"):
                continue
            entry = confirmations.get(marker, {})
            if (all(series.get(key) == value for key, value in (self._series_revision or {}).items())
                    and entry.get("revision") == (self._series_revision or {}) and requirements.get(marker)
                    and all(step in entry.get("steps", []) for step in requirements[marker])):
                if run_plan.stale_upstream_for_step(self.plan["project_id"], "montage_assembler", inputs=self.plan.get("inputs")):
                    confirmations.pop(marker, None)
                else:
                    stale.remove(marker)
                    confirmations.pop(marker, None)
        series["stale_results"] = stale
        write_json_atomic(path, series)

    def step_finished(self, step, result) -> None:
        status = _status_str(result) or "completed"
        self._complete_stale_step(step.id, status)
        if step.id in self.running:
            self.running.remove(step.id)
        self.finished.add(step.id)
        if status == "failed":
            self._failed_notified.add(step.id)
        if self.step_log:
            self.step_log.mark_finished(step.id, status, time.time(),
                                        duration_s=getattr(result, "duration_seconds", None),
                                        error=getattr(result, "error", None) if status == "failed" else None)
        self.event(f"Шаг «{step.id}» завершён ({status})")
        fields = self._progress_fields(f"Шаг '{step.id}' завершён ({status})")
        self._touch(**fields)
        self._notify(message=f"Шаг '{step.id}' завершён ({status})", progress=fields["percent"],
                     step_id=step.id, step_status=status,
                     step_duration=getattr(result, "duration_seconds", None),
                     step_error=getattr(result, "error", None),
                     step_error_class=(getattr(result, "error_class", "") or None),
                     step_traceback=(getattr(result, "metadata", None) or {}).get("traceback"))

    def _notify(self, **kwargs: Any) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.error("progress_callback: %s", exc)

    # ---- пауза и отмена
    def control(self) -> dict:
        return run_control.read_control(self.root)

    def cancelled(self) -> bool:
        return bool(self.control().get("cancel_requested")) or self.cancel_sent

    async def gate(self) -> None:
        """Старт шага ждёт, пока снята пауза; отмена пропускает ожидание."""
        while True:
            data = self.control()
            if data.get("cancel_requested") or not data.get("pause_requested"):
                return
            await asyncio.sleep(self.poll_s)

    async def _save_status(self, status_name: str, meta_key: str) -> None:
        try:
            from workflow.models import WorkflowContext, WorkflowStatus
            store = self.engine.state_manager.store
            ckpt = await store.get_latest_checkpoint(self.workflow_id)
            context = ckpt.context if ckpt and ckpt.context else WorkflowContext(
                workflow_id=self.workflow_id, session_id=self.plan["project_id"])
            await self.engine.state_manager.save_checkpoint(
                workflow_id=self.workflow_id, status=getattr(WorkflowStatus, status_name), context=context,
                step_results=ckpt.step_results if ckpt else {}, current_step=ckpt.current_step if ckpt else None,
                metadata={meta_key: "studio"})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Чекпоинт %s не сохранён: %s", status_name, exc)

    async def tick(self, last_beat: float) -> float:
        """Один проход монитора; возвращает время последнего heartbeat."""
        data = self.control()
        if data.get("cancel_requested") and not self.cancel_sent:
            self.cancel_sent = True
            run_control.control_patch(self.root, self.run_id, ack={"cancel": True})
            self._touch(state="stopping", message="Остановка прогона")
            self.event("Запрошена остановка прогона", "WARNING")
            try:
                await self.engine.cancel_workflow(self.workflow_id, reason="Остановлено пользователем (studio)")
            except Exception as exc:  # noqa: BLE001
                logger.error("cancel_workflow: %s", exc)
        elif data.get("pause_requested") and not self.cancel_sent:
            if not self.running:
                if not data.get("paused"):
                    run_control.control_patch(self.root, self.run_id, ack={"pause": True}, paused=True)
                    self._touch(state="paused", message="Прогон на паузе")
                    self.event("Прогон на паузе")
                    await self._save_status("PAUSED", "paused_by")
                    self.paused_saved = True
            elif not data.get("ack", {}).get("pause"):
                run_control.control_patch(self.root, self.run_id, ack={"pause": True})
                self._touch(state="pause_requested", message="Пауза сработает после текущего шага")
        elif (data.get("paused") or self.paused_saved) and not data.get("pause_requested"):
            run_control.control_patch(self.root, self.run_id, paused=False, ack={"pause": False})
            self._touch(state="running", message="Прогон продолжен")
            self.event("Прогон продолжен")
            await self._save_status("RUNNING", "resumed_by")
            self.paused_saved = False
        now = time.monotonic()
        if now - last_beat >= self.heartbeat_s:
            self._touch()
            last_beat = now
        return last_beat

    async def monitor(self) -> None:
        last_beat = time.monotonic()
        while True:
            try:
                last_beat = await self.tick(last_beat)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Монитор прогона: %s", exc)
            await asyncio.sleep(self.poll_s)

    def end(self, result: Any = None, error: BaseException | None = None) -> None:
        """Финальная сверка, исход в active_run.json, сброс control.json."""
        step_results = getattr(result, "step_results", None)
        if self.step_log and step_results:
            with contextlib.suppress(Exception):
                self.step_log.reconcile(step_results)
        if isinstance(step_results, dict):
            for sid, sr in step_results.items():
                if _status_str(sr) == "failed" and sid not in self._failed_notified:
                    self._notify(message=f"Шаг '{sid}' завершён с ошибкой", step_id=sid, step_status="failed",
                                 step_duration=getattr(sr, "duration_seconds", None),
                                 step_error=getattr(sr, "error", None),
                                 step_error_class=(getattr(sr, "error_class", "") or None),
                                 step_traceback=(getattr(sr, "metadata", None) or {}).get("traceback"))
        status = _status_str(result)
        if error is not None:
            state, outcome, message = "failed", "failed", str(error)
        elif status == "cancelled" or self.cancel_sent:
            state, outcome, message = "cancelled", "cancelled", "Прогон остановлен"
        elif status == "completed" or not status:
            state, outcome, message = "completed", "completed", "Прогон завершён"
        else:
            state, outcome = "failed", "failed"
            message = str(getattr(result, "error", None) or f"Прогон завершился со статусом {status}")
        if state == "completed":
            self._finalize_stale()
        fields = self._progress_fields(message)
        fields.update(running=[], step_id=None)
        if state == "completed":
            fields["percent"] = 100.0
        run_control.update_active(self.root, self.run_id, state=state, outcome=outcome,
                                  error=message if state == "failed" else None,
                                  finished_at=run_control.iso(), **fields)
        self.event(message, "ERROR" if state == "failed" else "INFO")
        with contextlib.suppress(Exception):
            run_control.append_history(self.root, {
                "run_id": self.run_id, "workflow_id": self.workflow_id, "mode": self.plan["mode"],
                "surface": self.surface, "started_at": run_control.iso(self.started_ts),
                "finished_at": run_control.iso(), "outcome": outcome})
        run_control.control_clear(self.root, self.run_id)
        if self.log is not None:
            self.log.close()


class _FailedStep:
    """Итог шага, упавшего исключением, в виде, который понимает RunSession."""

    status = "failed"
    duration_seconds = None
    error_class = ""
    metadata: dict = {}

    def __init__(self, error: str) -> None:
        self.error = error


def _safe(func, *args, **kwargs) -> None:
    """Запись состояния студии не должна валить шаг."""
    try:
        func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Запись состояния прогона: %s", exc)


async def _safe_async(func, *args, **kwargs) -> None:
    try:
        await func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Запись состояния прогона: %s", exc)


# ------------------------------------------------------------------ хуки движка

def _install_hooks(engine) -> None:
    """Один раз на экземпляр: обёртки ищут сессию по workflow_id и без неё ничего не меняют."""
    if getattr(engine, "_studio_hooked", False):
        return
    start_name = "_execute_enhanced_step" if hasattr(engine, "_execute_enhanced_step") else "_execute_workflow_step"
    original_start = getattr(engine, start_name)
    original_done = engine._on_step_completed

    async def hooked_start(*args, **kwargs):
        step = args[0] if args else kwargs.get("step")
        context = args[1] if len(args) > 1 else kwargs.get("context")
        session = _SESSIONS.get(getattr(context, "workflow_id", None))
        if session is None:
            return await original_start(*args, **kwargs)
        await _safe_async(session.gate)
        if session.cancelled():
            await _safe_async(session.tick, time.time())  # отмену обработать сразу: монитор мог не успеть
            from workflow.models import StepResult, StepStatus
            return StepResult(step_id=step.id, status=getattr(StepStatus, "SKIPPED", StepStatus.PENDING),
                              error="Прогон остановлен")
        session._prepare_stale_step(step.id)
        _safe(session.step_started, step, context)
        try:
            result = await original_start(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            _safe(session.step_finished, step, _FailedStep(str(exc)))
            raise
        if _status_str(result) == "failed":  # движок зовёт хук завершения только для успешных шагов
            _safe(session.step_finished, step, result)
        return result

    async def hooked_done(workflow_id, step, step_result, context, step_results, *args, **kwargs):
        await original_done(workflow_id, step, step_result, context, step_results, *args, **kwargs)
        session = _SESSIONS.get(workflow_id)
        if session is not None:
            _safe(session.step_finished, step, step_result)

    setattr(engine, start_name, hooked_start)
    engine._on_step_completed = hooked_done
    engine._studio_hooked = True


# ------------------------------------------------------------------ подготовка и исполнение

def _hook(module: str, name: str):
    try:
        mod = _studio(module)
    except ImportError:
        return None
    return getattr(mod, name, None)


SINGLE_WHAT = {"storybook_music_generator": "music", "montage_assembler": "film"}


def _raise_hook_error(res: dict, fallback: str) -> None:
    """Ответ хука Ф7 со status=error (плоский или вложенный) становится понятной ошибкой прогона."""
    err = res.get("error") if isinstance(res.get("error"), dict) else {}
    code = str(res.get("code") or err.get("code") or "internal")
    message = str(res.get("message") or err.get("message") or fallback)
    raise StudioError(code, message)


def _prepare(plan: dict, project_id: str, surface: Optional[str] = None) -> dict:
    """Хуки Ф7 (модуль review, ищем в момент вызова). Возвращает {step_outputs, regen_id}.

    `abandon_pending_jobs` и `image_parts` приходят из `plan["scope"]` (их задаёт `run_start`), поверхность запуска
    передаётся отдельно; всё это уходит в `review.prepare_regeneration` / `protect_before_run`."""
    steps = list(plan["steps_to_run"])
    if plan["mode"] == "regenerate_shots":
        prepare = _hook("review", "prepare_regeneration")
        if prepare is None:
            raise StudioError("unavailable", "Перегенерация шотов пока недоступна")
        kinds = plan["scope"]["kinds"]
        what = "both" if len(kinds) > 1 else (kinds[0] if kinds else "images")
        extra = {"abandon_pending_jobs": bool(plan["scope"].get("abandon_pending_jobs"))}
        if plan["scope"].get("image_parts"):
            extra["image_parts"] = tuple(plan["scope"]["image_parts"])
        res = prepare(project_id, plan["scope"]["shot_keys"], what, steps_to_run=steps, surface=surface, **extra)
        if not isinstance(res, dict) or res.get("status") == "error":
            _raise_hook_error(res if isinstance(res, dict) else {}, "Подготовка перегенерации не удалась")
        for step_id, params in (res.get("step_params") or {}).items():
            plan.setdefault("step_params", {}).setdefault(step_id, {}).update(params or {})
        return {"step_outputs": dict(res.get("step_outputs") or {}), "regen_id": res.get("regen_id")}
    if plan["mode"] == "single_step" and len(steps) == 1 and steps[0] in SINGLE_WHAT:
        # снимок music/film делает Ф7 в regenerate_start, журнал она не оставляет: regen_id не нужен
        if steps[0] == "storybook_music_generator" and plan["scope"].get("force_regenerate"):
            plan.setdefault("step_params", {}).setdefault(steps[0], {})["force_regenerate"] = True
        return {}
    protect = _hook("review", "protect_before_run")
    if protect is not None and PROTECT_STEPS & set(steps):
        res = _optional_call(protect, project_id, steps, surface=surface)
        if res is not None and res.get("status") == "error":
            _raise_hook_error(res, "Копия устаревших частей шотов не сделана")
        if res is not None and res.get("regen_id"):
            return {"regen_id": res["regen_id"]}
    return {}


def _optional_call(func, *args, **kwargs) -> Optional[dict]:
    """Необязательный хук: исключение пишем предупреждением, прогон не валим."""
    try:
        res = func(*args, **kwargs)
    except StudioError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Хук %s: %s", getattr(func, "__name__", func), exc)
        return None
    return res if isinstance(res, dict) else None


def _build_context(engine, plan: dict, prepared: dict):
    from workflow.models import WorkflowContext
    project_id = plan["project_id"]
    variables = copy.deepcopy(plan["inputs"])
    outputs: dict = {}
    metadata: dict = {}
    source = plan.get("source_workflow_id")
    ckpt = run_plan.load_checkpoint(engine, source) if source else None
    base = getattr(ckpt, "context", None) if ckpt else None
    if base is not None:
        outputs = copy.deepcopy(getattr(base, "step_outputs", None) or {})
        if plan["mode"] in ("single_step", "from_step", "regenerate_shots") or plan.get("reuse_workflow_id"):
            metadata = copy.deepcopy(getattr(base, "metadata", None) or {})
        if plan.get("reuse_workflow_id"):
            variables = {**copy.deepcopy(getattr(base, "variables", None) or {}), **variables}
    outputs.update(copy.deepcopy(prepared.get("step_outputs") or {}))
    selected = set(plan["steps_to_run"])
    if (plan["mode"] in ("single_step", "from_step")
            and "character_reference_states" in selected and "shots_prompt_qa" not in selected):
        shots_path = project_paths.safe_storybook_project_dir(project_id) / "97_shots/shots.json"
        if shots_path.is_file():
            outputs["shots_prompt_qa"] = json.loads(shots_path.read_text(encoding="utf-8"))
    if source:
        metadata["source_workflow_id"] = source
    if plan["mode"] == "single_step":
        metadata["single_step_rerun"] = plan.get("start_step")
    variables["project_id"] = project_id
    variables["series_id"] = plan["series_id"]
    return WorkflowContext(workflow_id=plan["workflow_id"], session_id=project_id, variables=variables,
                           step_outputs=outputs, metadata=metadata)


@contextlib.contextmanager
def _env(values: dict):
    previous = {k: os.environ.get(k) for k in values}
    os.environ.update({k: str(v) for k, v in values.items()})
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


async def _execute_plan_raw(engine, plan: dict, *, run_id: Optional[str] = None, surface: str = "react",
                           launcher: str = "backend", progress_callback: Optional[Callable] = None,
                           lock_held: bool = False, poll_s: Optional[float] = None,
                           heartbeat_s: Optional[float] = None):
    """Выполнить план и вернуть WorkflowResult. Без замка и при пустом плане - StudioError."""
    project_id = plan["project_id"]
    root = project_paths.safe_storybook_project_dir(project_id, must_exist=True)
    run_id = run_id or f"run-{uuid.uuid4().hex}"
    if not plan.get("steps_to_run"):
        raise StudioError("invalid_argument", "В плане нет шагов для запуска")
    handle = None
    if not lock_held:
        handle = run_control.acquire_lock_with_retries(project_id)
        if handle is None:
            raise run_control.locked_error(root)
    session = RunSession(engine, plan, root, run_id=run_id, surface=surface, launcher=launcher,
                         progress_callback=progress_callback,
                         poll_s=POLL_S if poll_s is None else poll_s,
                         heartbeat_s=HEARTBEAT_S if heartbeat_s is None else heartbeat_s)
    started_at = time.time()
    prepared: dict = {}
    result = None
    error: BaseException | None = None
    monitor_task = None
    try:
        from .series import materialize_snapshot
        materialize_snapshot(project_id, plan["series_id"])
        session.begin()
        prepared = _prepare(plan, project_id, surface)
        if prepared.get("regen_id"):
            run_control.update_active(root, run_id, regen_id=prepared["regen_id"])
        definition = run_plan.build_definition(plan)
        context = _build_context(engine, plan, prepared)
        _install_hooks(engine)
        with _SESSIONS_LOCK:
            _SESSIONS[plan["workflow_id"]] = session
        monitor_task = asyncio.ensure_future(session.monitor())
        with _env(plan.get("env") or {}):
            result = await engine.execute_workflow(definition, context)
        return result
    except BaseException as exc:  # noqa: BLE001 - отчёт и очистка ниже, ошибка уходит вызывающему
        error = exc
        raise
    finally:
        if monitor_task is not None:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await monitor_task
        with _SESSIONS_LOCK:
            _SESSIONS.pop(plan["workflow_id"], None)
        outcome = ("failed" if error is not None else
                   "cancelled" if (session.cancel_sent or _status_str(result) == "cancelled") else "completed")
        try:
            _finish(plan, project_id, prepared, started_at, outcome)
        finally:
            try:
                session.end(result, error)
            finally:  # замок и обработчик журнала снимаются всегда
                run_plan.clear_runs_cache()
                if session.log is not None:
                    session.log.close()
                if handle is not None:
                    run_control.release_lock(handle)


async def execute_plan_raw(engine, plan: dict, **kwargs):
    with project_paths.series_scope(plan["project_id"], plan["series_id"]):
        return await _execute_plan_raw(engine, plan, **kwargs)


def _finish(plan: dict, project_id: str, prepared: dict, since_ts: float, outcome: str) -> None:
    """После прогона под замком: отпечатки шотов и закрытие перегенерации (Ф6/Ф7); каждое в своём try."""
    try:
        reconcile = _hook("shots", "reconcile_after_run")
        if reconcile is not None:
            reconcile(project_id, since_ts, shot_keys=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Сверка отпечатков шотов (reconcile): %s", exc)
    try:
        finish = _hook("review", "finish_regeneration")
        if finish is not None and prepared.get("regen_id"):
            finish(project_id, prepared["regen_id"], since_ts=since_ts, outcome=outcome)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Закрытие перегенерации: %s", exc)


async def execute_plan(engine, plan: dict, *, run_id: Optional[str] = None, surface: str = "react",
                       launcher: str = "backend", progress_callback: Optional[Callable] = None,
                       lock_held: bool = False, **kwargs: Any) -> dict:
    """То же, что `execute_plan_raw`, но ответ в формате `PipelineRunner._build_runner_response`."""
    try:
        result = await execute_plan_raw(engine, plan, run_id=run_id, surface=surface, launcher=launcher,
                                        progress_callback=progress_callback, lock_held=lock_held, **kwargs)
    except StudioError as exc:
        return {"status": "error", "code": exc.code, "message": exc.message}
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка выполнения плана: %s", exc)
        return {"status": "error", "message": str(exc)}
    return build_response(result, {"project_id": plan["project_id"], "mode": plan["mode"],
                                   "workflow_id": plan["workflow_id"], "start_step": plan.get("start_step"),
                                   "skipped_steps": len(plan.get("skip_steps") or [])})


def execute_plan_sync(engine, plan: dict, **kwargs: Any) -> dict:
    """Для потоков без цикла событий (Tk-лаунчер)."""
    return asyncio.run(execute_plan(engine, plan, **kwargs))
