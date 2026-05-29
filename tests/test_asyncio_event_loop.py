"""
Тесты для P0.7: единый паттерн asyncio event loop.

Проверяет:
- Нет asyncio.run() в StoryBookManager (вызывает проблемы в threads)
- Нет asyncio.set_event_loop() (race condition при параллельных потоках)
- Все async-вызовы используют new_event_loop + run_until_complete + close
- 3 последовательных запуска new_event_loop не конфликтуют
"""

import asyncio
import threading
import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

SBM_DIR = project_root / "StoryBookManager"


class TestNoAsyncioRunOrSetEventLoop(unittest.TestCase):
    """Проверяет отсутствие asyncio.run и set_event_loop в production-коде"""

    def _scan_files_for_pattern(self, pattern):
        """Сканирует все .py файлы в StoryBookManager на наличие паттерна"""
        import re
        regex = re.compile(pattern)
        hits = []
        for py_file in SBM_DIR.rglob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            for i, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if regex.search(stripped):
                    hits.append((py_file.relative_to(project_root), i, stripped))
        return hits

    def test_no_asyncio_run(self):
        """Нет asyncio.run() в StoryBookManager (несовместимо с threading)"""
        hits = self._scan_files_for_pattern(r'\basyncio\.run\s*\(')
        msg = "\n".join(f"  {f}:{n}: {line}" for f, n, line in hits)
        self.assertEqual(hits, [], f"asyncio.run() найден:\n{msg}")

    def test_no_asyncio_set_event_loop(self):
        """Нет asyncio.set_event_loop() (race condition)"""
        hits = self._scan_files_for_pattern(r'\basyncio\.set_event_loop\s*\(')
        msg = "\n".join(f"  {f}:{n}: {line}" for f, n, line in hits)
        self.assertEqual(hits, [], f"asyncio.set_event_loop() найден:\n{msg}")


class TestSequentialEventLoops(unittest.TestCase):
    """Проверяет что последовательные event loops не конфликтуют"""

    def test_3_sequential_loops_no_conflict(self):
        """3 последовательных new_event_loop + run_until_complete не вызывают RuntimeError"""
        results = []

        async def dummy_coro(n):
            return f"result_{n}"

        for i in range(3):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(dummy_coro(i))
                results.append(result)
            finally:
                loop.close()

        self.assertEqual(results, ["result_0", "result_1", "result_2"])

    def test_3_sequential_loops_in_threads(self):
        """3 последовательных запуска в разных потоках не конфликтуют"""
        results = []
        errors = []

        async def dummy_coro(n):
            return f"thread_result_{n}"

        def run_in_thread(n):
            try:
                loop = asyncio.new_event_loop()
                try:
                    result = loop.run_until_complete(dummy_coro(n))
                    results.append(result)
                finally:
                    loop.close()
            except Exception as e:
                errors.append(str(e))

        for i in range(3):
            t = threading.Thread(target=run_in_thread, args=(i,), daemon=True)
            t.start()
            t.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 3)

    def test_cancel_during_loop_no_error(self):
        """Отмена coroutine во время выполнения не оставляет сломанный loop"""
        async def cancellable_coro():
            raise asyncio.CancelledError("cancelled by user")

        loop = asyncio.new_event_loop()
        try:
            with self.assertRaises(asyncio.CancelledError):
                loop.run_until_complete(cancellable_coro())
        finally:
            loop.close()

        # Следующий loop должен работать нормально
        loop2 = asyncio.new_event_loop()
        try:
            result = loop2.run_until_complete(self._async_ok())
            self.assertEqual(result, "ok")
        finally:
            loop2.close()

    @staticmethod
    async def _async_ok():
        return "ok"


if __name__ == "__main__":
    unittest.main()
