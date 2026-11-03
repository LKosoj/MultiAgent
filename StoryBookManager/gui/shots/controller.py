"""Машина состояний экрана «Кадры» (без tkinter).

Контроллер ничего не рисует и не знает про виджеты. Зависимости передаются снаружи:
  bridge        - StudioBridge: call(capability_id, payload, on_success=, on_error=, latest_only_key=);
  view          - объект с методами refresh(what), read_form(), apply_form(), is_visible(),
                  ask_unsaved(what), ask_conflict(body), confirm_restore(number), open_regen(keys);
  notify        - тосты: success, info, warning, error;
  schedule      - schedule(ms, fn): вызвать fn в главном потоке через ms миллисекунд;
  get_project_id - id открытого проекта или None.
Все ответы возвращаются в главном потоке (это делает мост), устаревшие отбрасываются по поколению.
"""

import logging
from typing import Callable, Dict, List, Optional, Tuple

from StoryBookManager.gui.shots import model as M
from StoryBookManager.gui.shots import texts as T

logger = logging.getLogger(__name__)

SURFACE = "tk"
SELECT_DELAY_MS = 150
PROBE_MS = 5000
NEAR_END = 20
MAX_KEYS = 5000


class ShotsController:
    def __init__(self, bridge, view, notify, schedule: Callable[[int, Callable], object],
                 get_project_id: Callable[[], Optional[str]],
                 get_run_state: Optional[Callable[[], Optional[str]]] = None,
                 on_started: Optional[Callable[[], None]] = None):
        self.bridge = bridge
        self.view = view
        self.notify = notify
        self.schedule = schedule
        self.get_project_id = get_project_id
        self.get_run_state = get_run_state
        self.on_started = on_started
        self.closed = False

        # список
        self.project_id: Optional[str] = None
        self.filters = M.ListFilters()
        self.mode = "list"
        self.items: List[dict] = []
        self.total = 0
        self.facets: Optional[dict] = None
        self.baseline = {"missing": 0, "outdated": 0}
        self.state_titles: Dict[str, str] = {}
        self.review_titles: Dict[str, str] = {}
        self.revision: Optional[str] = None
        self.reason: Optional[str] = None
        self.load_state = "idle"      # idle, loading, ready, empty, empty_filter, error
        self.load_error = ""
        self.loaded_for: Optional[str] = None
        self.marked: set = set()
        self.run_state: Optional[str] = None

        # карточка
        self.selected: Optional[str] = None
        self.card: Optional[dict] = None
        self.entry: Optional[dict] = None
        self.card_state = "none"      # none, loading, ready, error
        self.card_error = ""
        self.draft: Optional[dict] = None
        self.draft_note: Optional[str] = None
        self.reports: Optional[List[dict]] = None
        self.versions: Optional[dict] = None

        self._list_gen = 0
        self._more_gen = 0
        self._more_busy = False
        self._reloading = False
        self._sel_gen = 0
        self._regen_gen = 0
        self._pending_select: Optional[str] = None
        self._saving = False
        self._probe_scheduled = False

    # ------------------------------------------------------------------ вспомогательное

    @property
    def visible_items(self) -> List[dict]:
        return M.apply_predicate(self.items, self.filters.predicate)

    @property
    def order(self) -> List[str]:
        return [r["shot_key"] for r in self.visible_items]

    def row_of(self, key: Optional[str]) -> Optional[dict]:
        for row in self.items:
            if row.get("shot_key") == key:
                return row
        return None

    @property
    def run_busy(self) -> bool:
        return M.is_run_busy(self.run_state)

    @property
    def found_count(self) -> int:
        return len(self.visible_items) if self.filters.predicate else self.total

    def regen_block(self) -> str:
        return M.regen_block_reason(self.run_state, self.row_of(self.selected) if self.selected else None)

    def refresh(self, what: str) -> None:
        if not self.closed:
            self.view.refresh(what)

    def _call(self, capability: str, payload: dict, ok: Callable, fail: Callable, latest: Optional[str] = None) -> None:
        payload = {"project_id": self.project_id, **payload}
        project = self.project_id

        def guard_ok(result):
            if not self.closed and project == self.project_id:
                ok(result)

        def guard_fail(result):
            if not self.closed and project == self.project_id:
                fail(result)

        self.bridge.call(capability, payload, on_success=guard_ok, on_error=guard_fail, latest_only_key=latest)

    # ------------------------------------------------------------------ проект и показ

    def set_project(self, project_id: Optional[str]) -> None:
        """Смена проекта: всё забываем, данные подгрузятся при показе раздела."""
        self.project_id = project_id
        self._list_gen += 1
        self._more_gen += 1
        self._more_busy = False
        self._reloading = False
        self._sel_gen += 1
        self._regen_gen += 1
        self.filters = M.ListFilters()
        self.items, self.total, self.facets = [], 0, None
        self.baseline = {"missing": 0, "outdated": 0}
        self.revision = self.reason = None
        self.marked = set()
        self.selected = None
        self._clear_card()
        self.load_state = "idle"
        self.loaded_for = None
        self._pending_select = None
        for what in ("filters", "list", "card", "bulk", "banner"):
            self.refresh(what)

    def _clear_card(self) -> None:
        self.card = self.entry = None
        self.card_state = "none"
        self.card_error = ""
        self.draft = self.draft_note = None
        self.reports = self.versions = None

    def on_show(self) -> None:
        project = self.get_project_id()
        if project != self.project_id:
            self.set_project(project)
        if self.project_id is None:
            return
        if self.loaded_for != self.project_id:
            self.reload()
        else:
            self.reload(quiet=True)
        self._ensure_probe()

    def on_refresh(self) -> None:
        """F5: перечитать список и карточку, если в ней нет несохранённых правок."""
        if self.project_id is None:
            return
        self.reload(quiet=True)
        if self.selected and not self.has_unsaved():
            self.select(self.selected, immediate=True, force=True)

    def apply_intent(self, filter_name: Optional[str] = None, shot_key: Optional[str] = None) -> None:
        """Переход из «Обзора»: фильтр и/или шот. Без фильтра и шота ничего не меняется."""
        if filter_name is None and shot_key is None:
            return
        self.filters = M.filters_from_intent(filter_name)
        self._pending_select = shot_key
        self.refresh("filters")
        if self.project_id is not None and self.view.is_visible():
            self.reload()

    def shutdown(self) -> None:
        self.closed = True
        self._list_gen += 1
        self._sel_gen += 1

    # ------------------------------------------------------------------ загрузка списка

    def reload(self, quiet: bool = False) -> None:
        """Перечитать список. quiet: не показывать «Загрузка», сохранить выбор и число страниц."""
        if self.project_id is None:
            return
        self._list_gen += 1
        self._more_gen += 1
        self._more_busy = False
        self._reloading = True
        gen = self._list_gen
        if not quiet or self.load_state in ("idle", "error"):
            self.load_state = "loading"
            self.refresh("list")
            quiet = False
        target = max(M.PAGE_SIZE, len(self.items)) if quiet else M.PAGE_SIZE
        self._load_page(gen, 0, [], target, quiet)

    def _load_page(self, gen: int, offset: int, acc: List[dict], target: int, quiet: bool) -> None:
        payload = {"limit": M.PAGE_SIZE, "offset": offset, **self.filters.to_params()}

        def ok(result):
            if gen != self._list_gen:
                return
            rows = list(result.get("items") or [])
            total = int(result.get("total") or 0)
            acc.extend(rows)
            self.total = total
            self.facets = result.get("facets")
            self.baseline = result.get("baseline") or {"missing": 0, "outdated": 0}
            self.state_titles = result.get("state_titles") or self.state_titles
            self.review_titles = result.get("review_titles") or self.review_titles
            self.revision = result.get("revision")
            self.reason = result.get("reason")
            need_all = self.filters.predicate is not None or self._pending_select_missing(acc)
            goal = total if need_all else min(target, total)
            if rows and len(acc) < goal:
                self.items = list(acc) if not quiet else self.items
                if not quiet:
                    self.refresh("list")
                self._load_page(gen, len(acc), acc, target, quiet)
                return
            self.items = list(acc)
            self._finish_load()

        def fail(result):
            if gen != self._list_gen:
                return
            self._reloading = False
            self.load_state = "error"
            self.load_error = M.result_error_text(result)
            self.refresh("list")

        self._call("shots_list", payload, ok, fail, latest="shots_list")

    def _pending_select_missing(self, acc: List[dict]) -> bool:
        return self._pending_select is not None and not any(r.get("shot_key") == self._pending_select for r in acc)

    def _finish_load(self) -> None:
        self._reloading = False
        self.loaded_for = self.project_id
        visible = self.visible_items
        if not self.items and self.total == 0 and not self.filters.is_active():
            self.load_state = "empty"
        elif not visible:
            self.load_state = "empty_filter" if self.filters.is_active() else "empty"
        else:
            self.load_state = "ready"
        keys = [r["shot_key"] for r in visible]
        for what in ("filters", "list", "bulk", "banner", "busy"):
            self.refresh(what)
        target = self._pending_select
        self._pending_select = None
        if target is not None and target in keys:
            self.select(target, immediate=True)
            self.view_scroll_to(target)
        elif self.selected is None and keys:
            self.select(keys[0], immediate=True, ask=False)
        elif self.selected is not None and self.selected not in keys and self.load_state == "ready":
            self.select(keys[0], immediate=True, ask=False)
        elif not keys and self.selected is not None and not self.has_unsaved():
            self.selected = None
            self._clear_card()
            self.refresh("card")

    def view_scroll_to(self, key: str) -> None:
        scroll = getattr(self.view, "scroll_to_key", None)
        if scroll is not None:
            scroll(key)

    def maybe_load_more(self, last_visible_index: int) -> None:
        """Список докатился до конца загруженного: подгрузить следующую страницу."""
        loaded = len(self.items)
        if self.load_state != "ready" or self._reloading or not M.has_more_pages(loaded, self.total):
            return
        if last_visible_index < len(self.visible_items) - NEAR_END:
            return
        if self._more_busy:
            return
        self._more_busy = True
        gen = self._more_gen      # свой счётчик: тихая перезагрузка (_list_gen) не сбивается
        acc = list(self.items)

        def ok(result):
            self._more_busy = False
            if gen != self._more_gen or self._reloading:
                return
            rows = list(result.get("items") or [])
            self.total = int(result.get("total") or self.total)
            self.items = M.merge_pages(acc, rows, loaded, self.total)
            self.refresh("list")

        def fail(result):
            self._more_busy = False
            if gen == self._more_gen:
                self.notify.error(M.result_error_text(result))

        self._call("shots_list", {"limit": M.PAGE_SIZE, "offset": loaded, **self.filters.to_params()}, ok, fail,
                   latest="shots_more")

    def set_filters(self, filters: "M.ListFilters") -> None:
        if filters == self.filters:
            return
        self.filters = filters
        self.reload()

    def reset_filters(self) -> None:
        self.set_filters(M.ListFilters())

    def set_mode(self, mode: str) -> None:
        if mode in ("list", "grid") and mode != self.mode:
            self.mode = mode
            self.refresh("list")

    # ------------------------------------------------------------------ опрос во время прогона

    def on_status(self, brief: Optional[dict]) -> None:
        from StoryBookManager.gui.shell.status_model import run_state

        state = run_state(brief)
        if state == self.run_state:
            return
        was_busy = self.run_busy
        self.run_state = state
        self.refresh("busy")
        if self.run_busy:
            self._ensure_probe()
        elif was_busy and self.project_id is not None and self.view.is_visible():
            self.reload(quiet=True)  # прогон закончился: подхватить новые файлы

    def _ensure_probe(self) -> None:
        if self._probe_scheduled or not self.run_busy or self.project_id is None:
            return
        self._probe_scheduled = True
        self.schedule(PROBE_MS, self._probe)

    def _probe(self) -> None:
        self._probe_scheduled = False
        if self.closed or not self.run_busy or self.project_id is None or not self.view.is_visible():
            return
        gen = self._list_gen

        def ok(result):
            if gen == self._list_gen and result.get("revision") != self.revision and self.load_state in ("ready", "empty"):
                self.reload(quiet=True)

        self._call("shots_list", {"limit": 1}, ok, lambda r: None, latest="shots_probe")
        self._ensure_probe()

    # ------------------------------------------------------------------ выбор и карточка

    def select(self, key: str, immediate: bool = False, force: bool = False, ask: bool = True) -> None:
        """ask=False: фоновая смена выбора; при несохранённых правках выбор не трогаем, диалог не открываем."""
        if key == self.selected and not force:
            return
        if self.has_unsaved() and key != self.selected:
            if not ask:
                return
            choice = self.view.ask_unsaved(self.unsaved_what())
            if choice == "save":
                self.save(then=lambda: self.select(key, immediate, force, ask))
                return
            if choice != "discard":
                return
        self.selected = key
        self._sel_gen += 1
        gen = self._sel_gen
        self._clear_card()
        self.card_state = "loading"
        self.refresh("selection")
        self.refresh("card")
        self.refresh("busy")
        if immediate:
            self._load_card(key, gen)
        else:
            self.schedule(SELECT_DELAY_MS, lambda: self._load_card(key, gen) if gen == self._sel_gen else None)

    def move(self, delta: int) -> None:
        key = M.neighbor_key(self.order, self.selected, delta)
        if key is not None:
            self.select(key, immediate=True)
            self.view_scroll_to(key)

    def _load_card(self, key: str, gen: int, draft: Optional[Tuple[dict, str]] = None) -> None:
        got: Dict[str, dict] = {}
        failed: List[str] = []

        def finish():
            if gen != self._sel_gen:
                return
            if failed:
                self.card_state = "error"
                self.card_error = failed[0]
                self.refresh("card")
                return
            self.card = got["shot"].get("shot")
            self.entry = (got["review"].get("shots") or {}).get(key) or {}
            self.card_state = "ready"
            if draft is not None:
                self.draft = M.reapply_draft(self.card, draft[0])
                self.draft_note = draft[1]
            self.refresh("card")
            self.draft = self.draft_note = None
            self.refresh("busy")
            self._load_secondary(key, gen)

        def collect(name):
            def ok(result):
                if gen != self._sel_gen:
                    return
                got[name] = result
                if len(got) == 2 and not failed:
                    finish()

            def fail(result):
                if gen != self._sel_gen:
                    return
                if not failed:
                    failed.append(M.result_error_text(result))
                    finish()

            return ok, fail

        ok, fail = collect("shot")
        self._call("shot_get", {"shot_key": key}, ok, fail)
        ok, fail = collect("review")
        self._call("review_get", {"key": key}, ok, fail)

    def reload_card(self, draft: Optional[Tuple[dict, str]] = None) -> None:
        if self.selected is None:
            return
        self._sel_gen += 1
        gen = self._sel_gen
        key = self.selected
        self._clear_card()
        self.card_state = "loading"
        self.refresh("card")
        self._load_card(key, gen, draft)

    def _load_secondary(self, key: str, gen: int) -> None:
        def reports_ok(result):
            if gen == self._sel_gen:
                self.reports = list(result.get("groups") or [])
                self.refresh("secondary")

        def versions_ok(result):
            if gen != self._sel_gen:
                return
            self.versions = result
            if result.get("recovery"):
                self.notify.info(T.SHOT_TEXTS["recovery_toast"])
            self.refresh("secondary")

        def reports_fail(result):
            if gen == self._sel_gen:
                self.reports = []
                self.refresh("secondary")

        def versions_fail(result):
            if gen == self._sel_gen:
                self.versions = {"versions": [], "current": None, "selected": None}
                self.refresh("secondary")

        self._call("reports_for_shot", {"shot_key": key}, reports_ok, reports_fail)
        self._call("versions_list", {"key": key}, versions_ok, versions_fail)

    def reload_versions(self) -> None:
        if self.selected is not None:
            self._load_secondary(self.selected, self._sel_gen)

    # ------------------------------------------------------------------ правки и сохранение

    def pending_edit(self) -> Tuple[dict, bool]:
        """(изменённые поля, изменена ли заметка)."""
        if self.card_state != "ready" or self.card is None:
            return {}, False
        form, note = self.view.read_form()
        changes = M.dirty_changes(self.card, form)
        original_note = (self.entry or {}).get("note") or ""
        return changes, note != original_note

    def has_unsaved(self) -> bool:
        changes, note_changed = self.pending_edit()
        return bool(changes) or note_changed

    def unsaved_what(self) -> str:
        """Название шота для вопроса о несохранённых правках."""
        return f"Шот {self.selected}"

    def discard_edit(self) -> None:
        """«Отменить правку»: поля возвращаются к сохранённым значениям."""
        if self.card_state == "ready":
            self.refresh("card_reset")

    def save(self, then: Optional[Callable[[], None]] = None, on_fail: Optional[Callable[[], None]] = None) -> None:
        """Ctrl+S: сначала правка шота, потом заметка; если первый вызов упал, второй не выполняется."""
        def done_ok():
            if then is not None:
                then()

        def done_fail():
            if on_fail is not None:
                on_fail()

        if self.card_state != "ready" or self._saving:
            done_fail()
            return
        form_error = getattr(self.view, "form_error", lambda: "")()
        if form_error:
            self.notify.error(form_error)
            done_fail()
            return
        changes, note_changed = self.pending_edit()
        if not changes and not note_changed:
            done_ok()
            return
        _, note = self.view.read_form()
        key = self.selected
        gen = self._sel_gen
        self._saving = True

        def finish_ok():
            self._saving = False
            if gen != self._sel_gen:
                done_ok()
                return
            self.notify.success(T.SHOT_TEXTS["saved_toast"])
            self.refresh("card")
            self.reload(quiet=True)
            done_ok()

        def conflict(body: str):
            self._saving = False
            choice = self.view.ask_conflict(body)
            if choice == "refresh":
                self.reload_card()
            elif choice == "keep":
                self.reload_card(draft=(changes, note))
            done_fail()

        def save_note():
            def ok(result):
                if gen != self._sel_gen:
                    self._saving = False
                    done_ok()
                    return
                self.entry = result.get("entry") or self.entry
                finish_ok()

            def fail(result):
                code, reason, message, _ = M.error_parts(result)
                if gen != self._sel_gen:
                    self._saving = False
                    done_fail()
                    return
                if code == "conflict":
                    conflict(T.SHOT_TEXTS["review_conflict"])
                    return
                self._saving = False
                self.notify.error(T.error_text(code, reason, message))
                done_fail()

            self._call("review_set_note", {"key": key, "note": note, "expected_digest": (self.entry or {}).get("entry_digest") or "",
                                           "surface": SURFACE}, ok, fail)

        def save_shot():
            def ok(result):
                if gen != self._sel_gen:
                    self._saving = False
                    done_ok()
                    return
                self.card = result.get("shot") or self.card
                if note_changed:
                    save_note()
                else:
                    finish_ok()

            def fail(result):
                code, reason, message, _ = M.error_parts(result)
                if gen != self._sel_gen:
                    self._saving = False
                    done_fail()
                    return
                if code == "conflict":
                    conflict(T.SHOT_TEXTS["conflict_body"])
                    return
                self._saving = False
                self.notify.error(T.error_text(code, reason, message))
                done_fail()

            self._call("shot_update", {"shot_key": key, "changes": changes, "base_digest": (self.card or {}).get("digest") or ""},
                       ok, fail)

        if changes:
            save_shot()
        else:
            save_note()

    # ------------------------------------------------------------------ ревью

    def set_review_letter(self, letter: str) -> None:
        """A/R/X: на отмеченные напрямую, на текущий шот с переключением."""
        keys = M.bulk_target_keys(self.order, self.marked, self.selected)
        if not keys:
            return
        if self.marked:
            status = M.KEY_STATUS.get(letter.lower())
            if status is None:
                return
            self._set_status(keys, status, None)
            return
        key = keys[0]
        current = (self.entry or {}).get("status") if key == self.selected and self.entry else None
        if current is None:
            current = ((self.row_of(key) or {}).get("review") or {}).get("status") or "pending"
        status = M.next_status_on_key(current, letter)
        if status is None:
            return
        expected = None
        if key == self.selected and self.entry and self.entry.get("entry_digest"):
            expected = {key: self.entry["entry_digest"]}
        self._set_status(keys, status, expected)

    def set_status_for_marked(self, status: str) -> None:
        keys = M.bulk_target_keys(self.order, self.marked, self.selected)
        if keys:
            self._set_status(keys, status, None)

    def _set_status(self, keys: List[str], status: str, expected: Optional[dict]) -> None:
        payload = {"keys": keys, "status": status, "surface": SURFACE}
        if expected:
            payload["expected_digests"] = expected

        def ok(result):
            self.items = M.apply_review_status(self.items, keys, status, self.review_titles)
            digests = result.get("entries") or {}
            if self.selected in keys and self.entry is not None:
                self.entry = {**self.entry, "status": status,
                              "status_title": self.review_titles.get(status, status),
                              "entry_digest": digests.get(self.selected) or self.entry.get("entry_digest")}
            self.notify.success(M.toast_for_status(status, keys))
            self.refresh("list")
            self.refresh("card")
            self.reload(quiet=True)

        def fail(result):
            code, reason, message, _ = M.error_parts(result)
            self.notify.error(T.error_text(code, reason, message))
            if code == "conflict" and self.selected:
                self._reload_entry()

        self._call("review_set_status", payload, ok, fail)

    def _reload_entry(self) -> None:
        key, gen = self.selected, self._sel_gen

        def ok(result):
            if gen == self._sel_gen:
                self.entry = (result.get("shots") or {}).get(key) or self.entry
                self.refresh("card")

        self._call("review_get", {"key": key}, ok, lambda r: None)
        self.reload(quiet=True)

    # ------------------------------------------------------------------ отметки

    def toggle_mark(self, key: Optional[str] = None) -> None:
        key = key or self.selected
        if not key:
            return
        if key in self.marked:
            self.marked.discard(key)
        else:
            self.marked.add(key)
        self.refresh("list")
        self.refresh("bulk")

    def clear_marks(self) -> None:
        self.marked = set()
        self.refresh("list")
        self.refresh("bulk")

    def mark_all(self) -> None:
        """Ctrl+A: отметить все шоты по текущему фильтру (до 5000)."""
        if self.project_id is None:
            return

        def ok(result):
            keys = list(result.get("keys") or [])
            if self.filters.predicate:
                allowed = set(self.order)
                keys = [k for k in keys if k in allowed]
            self.marked = set(keys)
            if result.get("truncated"):
                self.notify.warning(T.fmt("marked_truncated", n=len(keys), total=result.get("total", len(keys))))
            self.refresh("list")
            self.refresh("bulk")

        self._call("shots_keys", {"limit": MAX_KEYS, **self.filters.to_params()}, ok,
                   lambda r: self.notify.error(M.result_error_text(r)))

    def adopt_baseline(self) -> None:
        def ok(result):
            self.notify.success(T.fmt("baseline_done", n=result.get("adopted", 0)))
            self.reload(quiet=True)
            if self.selected:
                self.reload_card() if not self.has_unsaved() else None

        self._call("shots_adopt_baseline", {}, ok, lambda r: self.notify.error(M.result_error_text(r)))

    # ------------------------------------------------------------------ перегенерация

    def open_regen(self) -> None:
        reason = self.regen_block()
        if reason:
            self.notify.info(reason)
            return
        keys = M.bulk_target_keys(self.order, self.marked, self.selected)
        if keys:
            self.view.open_regen(keys)

    def regen_rows(self, keys: List[str]) -> List[dict]:
        wanted = set(keys)
        return [r for r in self.items if r.get("shot_key") in wanted]

    def regen_default_what(self, keys: List[str]) -> str:
        return M.default_what_for_rows(self.regen_rows(keys))

    def regen_preview(self, keys: List[str], what: str, on_ok: Callable[[dict], None],
                      on_fail: Callable[[str], None]) -> int:
        """Предпросмотр плана и цены. Ответ на более раннее обращение отбрасывается."""
        self._regen_gen += 1
        gen = self._regen_gen

        def ok(result):
            if gen == self._regen_gen:
                on_ok(result)

        def fail(result):
            if gen == self._regen_gen:
                on_fail(M.result_error_text(result))

        self._call("regenerate_preview", {"what": what, "shot_keys": list(keys)}, ok, fail)
        return gen

    def regen_start(self, keys: List[str], what: str, preview: dict, abandon: bool,
                    on_ok: Callable[[], None], on_fail: Callable[[dict], None]) -> None:
        """Запуск в потоке этого процесса (launcher=local); исходные ключи, сервер расширит их сам."""
        base = {"project_id": self.project_id, "what": what, "launcher": "local", "shot_keys": list(keys)}
        payload = M.start_payload_from_preview(base, preview, True)
        payload["abandon_pending_jobs"] = bool(abandon)
        payload.pop("project_id")
        count = len(preview.get("expanded_keys") or keys)

        def ok(result):
            self.notify.success(T.fmt("regen_started", n=count))
            if self.on_started is not None:
                self.on_started()
            on_ok()
            self.reload(quiet=True)

        def fail(result):
            code, reason, message, details = M.error_parts(result)
            on_fail(M.classify_regen_error(code, reason, message, details))

        self._call("regenerate_start", payload, ok, fail)

    # ------------------------------------------------------------------ версии

    def compare(self, left: str, right: str, on_ok: Callable[[dict], None], on_fail: Callable[[str], None]) -> None:
        key = self.selected
        gen = self._sel_gen
        self._call("versions_compare", {"key": key, "left": left, "right": right},
                   lambda r: on_ok(r) if gen == self._sel_gen else None,
                   lambda r: on_fail(M.result_error_text(r)))

    def restore(self, version_id: str, number, on_done: Optional[Callable[[], None]] = None) -> None:
        """«Вернуть эту версию» после подтверждения."""
        if self.selected is None or self.regen_block():
            return
        if not self.view.confirm_restore(number):
            return
        key, gen = self.selected, self._sel_gen
        tokens = ((self.versions or {}).get("current") or {}).get("tokens")

        def ok(result):
            self.notify.success(T.SHOT_TEXTS["restore_done"])
            for warning in result.get("warnings") or []:
                self.notify.warning(str(warning.get("message") or ""))
            if gen == self._sel_gen:
                self.reload_card()
            self.reload(quiet=True)
            if on_done is not None:
                on_done()

        def fail(result):
            code, reason, message, _ = M.error_parts(result)
            self.notify.error(T.error_text(code, reason, message))
            if reason == "current_changed" and gen == self._sel_gen:
                self.reload_versions()

        payload = {"key": key, "version_id": version_id, "surface": SURFACE}
        if tokens:
            payload["expected_token"] = tokens
        self._call("versions_restore", payload, ok, fail)
