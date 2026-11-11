"""Действия экрана «Проекты» без tkinter: вызовы возможностей studio, задания, подтверждения.

Экран передаёт `ui` с несколькими методами (подтверждение, выбор файла, тосты, перерисовка),
поэтому всю проводку можно проверять на фейковом мосте.
Подсказки идут параметрами for_project_id и id_hint: project_id сервер проверяет как готовый проект.
"""

import logging
import os
import shutil
from typing import Callable, Dict, List, Optional

from StoryBookManager.gui.sections import projects_logic as L
from StoryBookManager.gui.sections.job_runner import JobRunner

logger = logging.getLogger(__name__)

Done = Callable[[bool, str, dict], None]   # (успех, текст ошибки, результат)


class ProjectsController:
    """Данные списка и все действия над проектами.

    ui должен уметь: list_updated(), row_updated(pid), select_project(pid), open_project(pid),
    confirm(spec) -> bool, ask_save_path(initial) -> str, toast_success(text),
    toast_error(text, details), show_blocking(text), project_removed(pid),
    schedule(ms, fn), is_visible() -> bool.
    """

    def __init__(self, bridge, jobs: JobRunner, ui, *, copy_file: Callable[[str, str], object] = shutil.copyfile,
                 run_bg: Optional[Callable[[Callable, Callable], None]] = None):
        self.bridge = bridge
        self.jobs = jobs
        self.ui = ui
        self._copy_file = copy_file
        self._run_bg = run_bg or (lambda func, done: done(func()))
        self.cards: List[dict] = []
        self.total = 0
        self.projects_dir = ""
        self.loading = False
        self.loaded = False
        self.error: Optional[dict] = None
        self.query = ""
        self.sort = L.DEFAULT_SORT
        self.job_text: Dict[str, str] = {}
        self.sizes = L.SizeQueue()
        self._size_timer = False
        self._closed = False

    # --- Список -----------------------------------------------------------------
    def card(self, project_id: Optional[str]) -> Optional[dict]:
        return next((c for c in self.cards if c.get("project_id") == project_id), None)

    def view_rows(self):
        """(состояние списка, строки) с учётом поиска и сортировки."""
        shown = L.sort_cards(L.filter_cards(self.cards, self.query), self.sort, self.sizes.sizes)
        state = L.list_state(loading=self.loading and not self.loaded, error=self.error, total=self.total,
                             shown=len(shown), query=self.query)
        rows = [L.row_for(c, self.sizes.sizes.get(c["project_id"]), self.job_text.get(c["project_id"], ""))
                for c in shown]
        return state, rows

    def refresh(self) -> None:
        self.loading = True
        self.ui.list_updated()
        self.bridge.call("projects_list", {"sort": "modified"}, latest_only_key="projects_list",
                         on_success=self._listed, on_error=self._list_failed)

    def _listed(self, result: dict) -> None:
        self.loading, self.loaded, self.error = False, True, None
        self.cards = list(result.get("projects") or [])
        self.total = int(result.get("total") or 0)
        self.projects_dir = str(result.get("projects_dir") or "")
        self.sizes.reset([c["project_id"] for c in self.cards])
        self.ui.list_updated()
        self._size_step()

    def _list_failed(self, result: dict) -> None:
        self.loading, self.loaded = False, False
        self.error = result.get("error") or {"code": "internal", "message": "Внутренняя ошибка"}
        self.cards, self.total = [], 0
        self.ui.list_updated()

    # --- Размеры по очереди ------------------------------------------------------------
    def _size_step(self) -> None:
        if self._closed or self._size_timer or self.sizes.busy or self.sizes.remaining == 0:
            return
        if not self.ui.is_visible():
            self._size_wait(1000)
            return
        project_id = self.sizes.next_id()
        if project_id is None:
            return
        generation = self.sizes.generation
        self.bridge.call("projects_size", {"project_id": project_id},
                         on_success=lambda r: self._sized(project_id, r.get("size_bytes"), generation),
                         on_error=lambda r: self._sized(project_id, None, generation))

    def _sized(self, project_id: str, size, generation: int) -> None:
        if not self.sizes.done(project_id, size, generation):
            return
        self.ui.row_updated(project_id)
        self._size_wait(L.SIZE_QUEUE_PAUSE_MS)

    def _size_wait(self, ms: int) -> None:
        self._size_timer = True

        def fire():
            self._size_timer = False
            self._size_step()

        self.ui.schedule(ms, fire)

    def close(self) -> None:
        self._closed = True
        self.jobs.close()

    # --- Карточка справа ------------------------------------------------------------------
    def load_details(self, project_id: str, done: Callable[[Optional[dict], Optional[dict]], None]) -> None:
        """Загрузить общие данные постоянного проекта."""
        self.bridge.call("projects_get", {"project_id": project_id}, latest_only_key="projects_details",
                         on_success=lambda r: done(r.get("details") or {}, None),
                         on_error=lambda r: done(None, None))

    # --- Общая обработка заданий ------------------------------------------------------------
    def _progress(self, project_id: Optional[str]):
        def handler(progress: dict) -> None:
            text = L.text_job_progress(str(progress.get("message") or ""), int(progress.get("current") or 0),
                                       int(progress.get("total") or 0))
            if project_id:
                self.job_text[project_id] = text
                self.ui.row_updated(project_id)
        return handler

    def _clear_progress(self, project_id: Optional[str]) -> None:
        if project_id and self.job_text.pop(project_id, None) is not None:
            self.ui.row_updated(project_id)

    def _fail(self, error: dict, *, blocking: bool = False) -> None:
        text = L.error_text(error)
        if blocking or (error or {}).get("code") == "locked":
            self.ui.show_blocking(text)
        else:
            self.ui.toast_error(L.text_job_failed(text), text)

    # --- Создание ----------------------------------------------------------------------------
    def create_from_prompt(self, params: dict, *, progress: Callable[[str], None], done: Done) -> bool:
        """Задание create_from_prompt; окно остаётся открытым до конца (done вызывается один раз)."""
        def on_started(result: dict) -> None:
            self.refresh()   # в списке появится строка «Создаётся…»

        def on_progress(p: dict) -> None:
            progress(L.text_job_progress(str(p.get("message") or L.TXT_CREATING_PROGRESS),
                                         int(p.get("current") or 0), int(p.get("total") or 0)))

        def on_done(result: dict) -> None:
            done(True, "", result)

        def on_error(error: dict) -> None:
            done(False, L.error_text(error), {})

        return self.jobs.start("projects_create_from_prompt", dict(params), on_started=on_started,
                               on_progress=on_progress, on_done=on_done, on_error=on_error)

    def create_by_form(self, values: dict, done: Done) -> None:
        self.bridge.call("projects_create", dict(values),
                         on_success=lambda r: done(True, "", r),
                         on_error=lambda r: done(False, L.error_text(r.get("error")), {}))

    def suggest_id(self, hint: str, done: Callable[[Optional[str]], None]) -> None:
        self.bridge.call("projects_suggest_id", {"id_hint": hint}, latest_only_key="suggest_id",
                         on_success=lambda r: done(r.get("project_id")), on_error=lambda r: done(None))

    # --- Правка ----------------------------------------------------------------------------------
    def rename(self, project_id: str, title: str, done: Done) -> None:
        def ok(result: dict) -> None:
            self.refresh()
            self.ui.toast_success(L.TOAST_RENAMED)
            done(True, "", result)

        self.bridge.call("projects_rename", {"project_id": project_id, "title": title}, on_success=ok,
                         on_error=lambda r: done(False, L.error_text(r.get("error")), {}))

    def duplicate(self, project_id: str, id_hint: str, done: Done) -> bool:
        """done вызывается при старте (успех или ошибка параметров); итог задания - тостом."""
        payload = {"project_id": project_id}
        if id_hint:
            payload["id_hint"] = id_hint
        started = []

        def on_started(result: dict) -> None:
            started.append(True)
            self.job_text[project_id] = "Копирование…"
            self.ui.row_updated(project_id)
            done(True, "", result)

        def on_done(result: dict) -> None:
            self._clear_progress(project_id)
            self.ui.toast_success(L.TOAST_DUPLICATED)
            self.refresh()
            if result.get("new_project_id"):
                self.ui.select_project(result["new_project_id"])

        def on_error(error: dict) -> None:
            if not started:
                done(False, L.error_text(error), {})
                return
            self._clear_progress(project_id)
            self._fail(error)

        return self.jobs.start("projects_duplicate", payload, project_id=project_id, on_started=on_started,
                               on_progress=self._progress(project_id), on_done=on_done, on_error=on_error)

    # --- Удаление --------------------------------------------------------------------------------
    def delete(self, project_id: str) -> None:
        """Размер, затем подтверждение; projects_delete(confirm=True) только после «Удалить»."""
        card = self.card(project_id)
        if card is None:
            return
        gate = L.gate_change(card)
        if not gate.allowed:
            self.ui.toast_info(gate.reason) if hasattr(self.ui, "toast_info") else None
            return
        if self.jobs.is_busy(project_id):
            return

        def ask(size_text: str) -> None:
            if not self.ui.confirm(L.delete_spec(card.get("title") or project_id, size_text)):
                return
            self._delete_confirmed(project_id)

        self.bridge.call("projects_size", {"project_id": project_id},
                         on_success=lambda r: ask(L.format_bytes(r.get("size_bytes"))),
                         on_error=lambda r: ask("размер неизвестен"))

    def _delete_confirmed(self, project_id: str) -> None:
        def on_done(result: dict) -> None:
            self._clear_progress(project_id)
            self.ui.toast_success(L.toast_removed(L.format_bytes(result.get("backup_size_bytes"))))
            self.ui.project_removed(project_id)
            self.refresh()

        def on_error(error: dict) -> None:
            self._clear_progress(project_id)
            self._fail(error)

        started = self.jobs.start("projects_delete", {"project_id": project_id, "confirm": True},
                                  project_id=project_id, on_progress=self._progress(project_id),
                                  on_done=on_done, on_error=on_error)
        if not started:
            self.ui.show_blocking("Для этого проекта уже идёт операция.")

    # --- Экспорт ------------------------------------------------------------------------------------
    def export(self, project_id: str) -> None:
        target = self.ui.ask_save_path(f"{project_id}.zip")
        if not target:
            return

        def on_done(result: dict) -> None:
            self._clear_progress(project_id)
            source = L.zip_target(self.projects_dir, project_id, str(result.get("path") or ""))

            def copy():
                self._copy_file(source, target)
                return target

            def copied(outcome) -> None:
                if isinstance(outcome, Exception):
                    self.ui.toast_error("Не удалось сохранить архив.", str(outcome))
                else:
                    self.ui.toast_success(L.toast_exported(os.path.basename(target)))

            self._run_bg(_capture(copy), copied)

        def on_error(error: dict) -> None:
            self._clear_progress(project_id)
            self._fail(error)

        if not self.jobs.start("projects_export", {"project_id": project_id}, project_id=project_id,
                               on_progress=self._progress(project_id), on_done=on_done, on_error=on_error):
            self.ui.show_blocking("Для этого проекта уже идёт операция.")

    # --- Бэкапы ---------------------------------------------------------------------------------------
    def backups_load(self, for_project_id: str, done: Callable[[Optional[List[dict]], str], None]) -> None:
        payload = {"for_project_id": for_project_id} if for_project_id else {"for_project_id": ""}
        self.bridge.call("projects_backups_list", payload, latest_only_key="backups_list",
                         on_success=lambda r: done(list(r.get("backups") or []), ""),
                         on_error=lambda r: done(None, L.error_text(r.get("error"))))

    def backup_create(self, project_id: str, note: str, done: Callable[[bool, str, bool], None]) -> None:
        payload = {"project_id": project_id}
        if note:
            payload["note"] = note
        started_consistent = []

        def on_started(result: dict) -> None:
            started_consistent.append(result.get("consistent", True))

        def on_done(result: dict) -> None:
            consistent = result.get("consistent", started_consistent[0] if started_consistent else True)
            self.ui.toast_success(L.TOAST_BACKUP_CREATED)
            done(True, "", bool(consistent))

        def on_error(error: dict) -> None:
            done(False, L.error_text(error), True)

        if not self.jobs.start("projects_backup_create", payload, project_id=project_id, on_started=on_started,
                               on_done=on_done, on_error=on_error):
            done(False, "Для этого проекта уже идёт операция.", True)

    def backup_restore(self, backup: dict, done: Callable[[bool, str], None]) -> None:
        """Подтверждение, затем восстановление; locked показывается в окне бэкапов."""
        project_id = str(backup.get("project_id") or "")
        if not self.ui.confirm(L.restore_spec(L.format_datetime(backup.get("created_at")))):
            return

        def on_done(result: dict) -> None:
            self.ui.toast_success(L.TOAST_BACKUP_RESTORED)
            self.refresh()
            self.ui.select_project(str(result.get("project_id") or project_id))
            done(True, "")

        def on_error(error: dict) -> None:
            done(False, L.error_text(error))

        payload = {"project_id": project_id, "backup_id": backup.get("backup_id"), "confirm": True}
        if not self.jobs.start("projects_backup_restore", payload, project_id=project_id,
                               on_done=on_done, on_error=on_error):
            done(False, "Для этого проекта уже идёт операция.")


def _capture(func: Callable):
    """Исключение фоновой работы отдаём как значение: Tk-поток сам решает, что показать."""
    def wrapper():
        try:
            return func()
        except Exception as exc:
            logger.exception("Фоновая работа экрана «Проекты» упала")
            return exc
    return wrapper
