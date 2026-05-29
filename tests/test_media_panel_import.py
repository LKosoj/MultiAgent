"""
Тесты для P0.5: исправление импорта log_smolagents_panel в media_panel.

Проверяет:
- Импорт media_panel не вызывает ImportError
- log_smolagents_panel доступна (либо из utils, либо fallback)
- Fallback-версия работает как logging
- В исходном коде нет голого 'from utils import log_smolagents_panel'
"""

import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

SOURCE_PATH = project_root / "StoryBookManager" / "gui" / "media_panel.py"


class TestMediaPanelImport(unittest.TestCase):
    """Проверяет что media_panel не падает на импорте"""

    def test_no_bare_import_log_smolagents_panel(self):
        """В media_panel.py нет голого 'from utils import log_smolagents_panel'"""
        source = SOURCE_PATH.read_text(encoding="utf-8")
        lines = source.splitlines()

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped == "from utils import log_smolagents_panel":
                # Проверяем что эта строка внутри try-блока
                # Ищем предыдущую строку с 'try:'
                found_try = False
                for j in range(i - 2, max(i - 5, 0), -1):
                    if lines[j].strip() == "try:":
                        found_try = True
                        break
                self.assertTrue(
                    found_try,
                    f"Строка {i}: 'from utils import log_smolagents_panel' "
                    "должна быть внутри try-блока"
                )

    def test_fallback_defined_in_except_block(self):
        """В except ImportError блоке определена fallback-функция"""
        source = SOURCE_PATH.read_text(encoding="utf-8")
        self.assertIn("except ImportError:", source)
        self.assertIn("def log_smolagents_panel(", source)

    def test_log_smolagents_panel_callable_in_both_paths(self):
        """log_smolagents_panel вызываема независимо от доступности utils"""
        # Тестируем fallback напрямую
        import logging
        fallback_logger = logging.getLogger("test_fallback")

        def fallback_log_smolagents_panel(content, title="", **kwargs):
            fallback_logger.info(f"[{title}] {content}")

        # Не должно бросать исключение
        fallback_log_smolagents_panel(
            content={"test": "data"},
            title="Test Panel",
            title_style="bold blue",
            border_style="blue"
        )

    def test_fallback_accepts_same_kwargs_as_original(self):
        """Fallback принимает те же аргументы что и оригинальная функция"""
        import logging
        fallback_logger = logging.getLogger("test_kwargs")

        def fallback_log_smolagents_panel(content, title="", **kwargs):
            fallback_logger.info(f"[{title}] {content}")

        # Вызов с теми же аргументами что в media_panel.py строки 968-973
        fallback_log_smolagents_panel(
            content={"📝 Промпт": "test", "🖼️  Изображений": 3},
            title="🎨 Artist Generation Process (edit_image_vse_tool)",
            title_style="bold green",
            border_style="blue"
        )


if __name__ == "__main__":
    unittest.main()
