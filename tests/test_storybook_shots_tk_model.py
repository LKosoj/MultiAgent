"""Чистые модули экрана «Кадры» (Tk): модель, раскладка, клавиши, очередь миниатюр, пути болванки."""

import json
import unittest
from pathlib import Path

from StoryBookManager.gui.shots import keys, layout, model as M, texts as T
from StoryBookManager.gui.shots.thumb_queue import LruCache, ThumbRequest, ThumbScheduler

FIXTURES = Path(__file__).parent / "fixtures"


class TestTexts(unittest.TestCase):
    def test_match_fixture(self):
        data = json.loads((FIXTURES / "storybook_shots_texts.json").read_text(encoding="utf-8"))
        data = data.get("texts", data)
        self.assertEqual(T.SHOT_TEXTS, data)

    def test_rub_amount(self):
        self.assertEqual(T.format_rub_amount(1180), "1\u00a0180\u00a0₽")
        self.assertEqual(T.format_rub_amount(0), "0\u00a0₽")
        self.assertEqual(T.format_rub_amount(0.3), T.format_rub_exact(0.3))

    def test_reason_title(self):
        for code in ("prompt_changed", "unknown_code"):
            self.assertTrue(T.reason_title(code))


class TestFilters(unittest.TestCase):
    def test_params_only_non_empty(self):
        self.assertEqual(M.ListFilters().to_params(), {})
        self.assertEqual(M.ListFilters(scene=2, stale_only=True).to_params(), {"scene": 2, "stale_only": True})

    def test_client_predicate_not_sent(self):
        f = M.filters_from_intent("missing_images")
        self.assertEqual(f.predicate, "missing_images")
        self.assertNotIn("predicate", f.to_params())


class TestReviewHelpers(unittest.TestCase):
    def test_letter_toggles_back_to_pending(self):
        self.assertEqual(M.next_status_on_key("pending", "a"), "approved")
        self.assertEqual(M.next_status_on_key("approved", "a"), "pending")

    def test_neighbor(self):
        self.assertEqual(M.neighbor_key(["a", "b", "c"], "a", 1), "b")
        self.assertEqual(M.neighbor_key(["a", "b", "c"], "c", 1), "c")

    def test_bulk_targets(self):
        self.assertEqual(M.bulk_target_keys(["1-1", "1-2", "1-3"], {"1-3", "1-1"}, "1-2"), ["1-1", "1-3"])
        self.assertEqual(M.bulk_target_keys(["1-1"], set(), "1-1"), ["1-1"])


class TestLayoutKeys(unittest.TestCase):
    def test_list_has_scene_headers(self):
        rows = layout.layout_rows(["1-1", "1-2", "2-1"], "list", 1)["rows"]
        self.assertEqual([r["kind"] for r in rows], ["header", "row", "row", "header", "row"])

    def test_grid_columns(self):
        self.assertGreaterEqual(layout.columns_for_width(10), 1)

    def test_keys_by_russian_layout(self):
        self.assertIn("<Key-Cyrillic_ef>", keys.sequences_for("approve"))

    def test_keys_ignored_in_text_inputs_and_modals(self):
        self.assertFalse(keys.should_handle("Text", "approve", False, True))
        self.assertFalse(keys.should_handle("Canvas", "approve", True, True))
        self.assertFalse(keys.should_handle("TButton", "mark", False, True))
        self.assertTrue(keys.should_handle("Canvas", "approve", False, True))


class TestThumbQueue(unittest.TestCase):
    def test_lru_evicts_oldest(self):
        c = LruCache(2)
        c.put("a", 1)
        c.put("b", 2)
        c.get("a")
        c.put("c", 3)
        self.assertNotIn("b", c)
        self.assertIn("a", c)

    def test_scheduler_priority_and_drop(self):
        s = ThumbScheduler()
        r1 = ThumbRequest("1-1", "a.png", 128, None, 5)
        r2 = ThumbRequest("1-2", "b.png", 128, None, 1)
        s.set_wanted([r1, r2])
        self.assertEqual(s.pop(1), [r2])
        s.set_wanted([])
        self.assertFalse(s.still_wanted(r1))


class TestBlockoutPaths(unittest.TestCase):
    def test_convention(self):
        self.assertEqual(M.blockout_rel(2, 3, "start"), "93_blockout/scene_02_shot_03/ref_start.png")
        self.assertEqual(M.blockout_rel(2, 3, "video"), "93_blockout/scene_02_shot_03/blockout_ref.mp4")


class TestRegen(unittest.TestCase):
    def test_busy_run_blocks(self):
        self.assertTrue(M.regen_block_reason("running", None))
        self.assertEqual(M.regen_block_reason("idle", {"state": "ready"}), "")

    def test_classify_paid_pending(self):
        r = M.classify_regen_error("conflict", "paid_job_pending", "m", {"keys": ["2-1", "1-1"]})
        self.assertEqual((r["kind"], r["keys"]), ("need_abandon", ["1-1", "2-1"]))


if __name__ == "__main__":
    unittest.main()


class TestThumbLoaderCancel(unittest.TestCase):
    def test_cancelled_request_is_not_cached_as_missing(self):
        import threading
        import time

        from StoryBookManager.gui.shots.thumb_loader import ThumbLoader

        class W:
            def __init__(self):
                self.cbs = []

            def after(self, ms, fn):
                self.cbs.append(fn)

        w, gate = W(), threading.Event()

        def worker(pid, req):
            if req.rel in ("x", "y"):
                gate.wait(5)
            return object()

        ld = ThumbLoader(w, lambda: "p", lambda r, p: None, worker=worker, photo_factory=lambda pil: "PHOTO")
        R = lambda rel: ThumbRequest(rel, rel, 128, "r", 0, None)  # noqa: E731
        try:
            ld.request([R("x"), R("y")])
            time.sleep(0.2)
            ld.request([R("x"), R("y"), R("a")])
            time.sleep(0.2)
            ld.request([R("x"), R("y")])   # "a" отменён до старта
            gate.set()
            time.sleep(0.5)
            for _ in range(8):   # _poll сам ставит следующий вызов: берём по одному
                if w.cbs:
                    w.cbs.pop(0)()
                time.sleep(0.1)
            self.assertIsNone(ld.cached(R("a")))   # не False: при возврате к нему загрузка повторится
        finally:
            ld.close()
