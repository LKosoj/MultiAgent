"""Логика экранов «Проекты» и «Обзор» (без tkinter): фильтр, сортировка, тексты, очередь размеров, модель обзора."""

import unittest

from StoryBookManager.gui.sections import overview_logic as O
from StoryBookManager.gui.sections import projects_logic as L
from StoryBookManager.gui.shell import status_model


def card(pid, title="", **kw):
    base = {"project_id": pid, "title": title or pid, "state": "ready", "busy": False,
            "stage": {"title": "Бриф"}, "modified": "2026-11-03T10:00:00"}
    base.update(kw)
    return base


class TestFilterSort(unittest.TestCase):
    def test_filter_by_title_and_id_ignores_case(self):
        cards = [card("a1", "Зимняя сказка"), card("b2", "Лето")]
        self.assertEqual([c["project_id"] for c in L.filter_cards(cards, "ЗИМ")], ["a1"])
        self.assertEqual([c["project_id"] for c in L.filter_cards(cards, "b2")], ["b2"])
        self.assertEqual(len(L.filter_cards(cards, "  ")), 2)

    def test_size_sort_puts_unknown_last(self):
        cards = [card("a"), card("b"), card("c")]
        order = [c["project_id"] for c in L.sort_cards(cards, "size", {"b": 10, "c": 50})]
        self.assertEqual(order, ["c", "b", "a"])

    def test_modified_sort_newest_first(self):
        cards = [card("old", modified="2026-01-01"), card("new", modified="2026-05-01")]
        self.assertEqual(L.sort_cards(cards, "modified")[0]["project_id"], "new")


class TestGates(unittest.TestCase):
    def test_busy_blocks_delete_but_not_copy(self):
        busy = card("p", busy=True)
        self.assertFalse(L.gate_change(busy).allowed)
        self.assertTrue(L.gate_copy(busy).allowed)

    def test_creating_blocks_everything(self):
        creating = card("p", state="creating")
        self.assertFalse(L.gate_open(creating).allowed)
        self.assertFalse(L.gate_copy(creating).allowed)

    def test_second_job_blocks_copy(self):
        self.assertFalse(L.gate_copy(card("p"), job_running=True).allowed)


class TestListState(unittest.TestCase):
    def test_states(self):
        self.assertEqual(L.list_state(loading=True, error=None, total=0, shown=0, query="").kind, "loading")
        self.assertEqual(L.list_state(loading=False, error=None, total=0, shown=0, query="").kind, "empty")
        self.assertEqual(L.list_state(loading=False, error=None, total=3, shown=0, query="x").kind, "nothing")
        self.assertEqual(L.list_state(loading=False, error=None, total=3, shown=3, query="").kind, "rows")
        err = L.list_state(loading=False, error={"code": "not_found", "message": "Папка проектов не найдена: /x"},
                           total=0, shown=0, query="")
        self.assertEqual(err.kind, "error")
        self.assertIn("settings", err.actions)

    def test_error_text_locked(self):
        self.assertEqual(L.error_text({"code": "locked"}), L.TXT_PROJECT_LOCKED)


class TestSizeQueue(unittest.TestCase):
    def test_one_at_a_time_and_stale_answer_dropped(self):
        q = L.SizeQueue()
        q.reset(["a", "b"])
        gen = q.generation
        self.assertEqual(q.next_id(), "a")
        self.assertIsNone(q.next_id())
        q.reset(["a", "b"])
        self.assertFalse(q.done("a", 5, gen))
        self.assertEqual(q.sizes, {})
        self.assertEqual(q.next_id(), "a")
        self.assertTrue(q.done("a", 5, q.generation))
        self.assertEqual(q.next_id(), "b")


class TestForms(unittest.TestCase):
    def test_prompt_limits(self):
        self.assertFalse(L.validate_prompt("коротко"))
        self.assertTrue(L.validate_prompt("a" * 10))
        self.assertFalse(L.validate_prompt("a" * 8001))

    def test_backup_scope_without_project_is_all(self):
        self.assertEqual(L.default_backup_scope(None), (True, ""))
        self.assertEqual(L.default_backup_scope("p"), (False, "p"))

    def test_delete_spec_is_destructive_and_names_project(self):
        spec = L.delete_spec("Сказка", "1 МБ")
        self.assertIn("Сказка", spec.title)


class TestOverviewModel(unittest.TestCase):
    def test_build_and_error_keeps_old_data_stale(self):
        view = O.build_overview({"project": {"id": "p", "title": "Т"}, "active_run": {"state": "running", "text": "шаг"}})
        self.assertEqual((view.mode, view.title, view.run_tone), ("ready", "Т", "running"))
        stale = O.with_error(view, {"message": "нет связи"})
        self.assertTrue(stale.stale)
        self.assertEqual(stale.title, "Т")
        self.assertEqual(O.with_error(None, {"message": "x"}).mode, "error")

    def test_not_found_is_gone(self):
        self.assertTrue(O.with_error(None, {"code": "not_found"}).gone)

    def test_unknown_screen_falls_back_to_generation(self):
        view = O.build_overview({"next_action": {"title": "t", "target": {"screen": "zzz"}}})
        self.assertEqual(view.next_action.screen, "generation")

    def test_brief_from_status(self):
        brief = status_model.brief_from_status({"project": {"id": "p", "title": "Т"},
                                                "active_run": {"state": "idle"}})
        self.assertEqual(brief["project"]["id"], "p")


if __name__ == "__main__":
    unittest.main()
