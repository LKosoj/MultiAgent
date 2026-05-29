"""
Тесты для P1.3: дублирование логов генерации в файловый logger.

Проверяет:
- add_log() вызывает logger перед обновлением UI
- Сообщения содержат префикс [GENERATION]
- level='success' маппится на logger.info
"""

import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

SOURCE_PATH = project_root / "StoryBookManager" / "gui" / "generation_panel.py"


class TestAddLogDuplicatesToFile(unittest.TestCase):
    """Проверяет что add_log дублирует в файловый logger"""

    def _get_add_log_body(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        start = source.index("def add_log(self")
        next_def = source.index("\n    def ", start + 1)
        return source[start:next_def]

    def test_add_log_calls_logger(self):
        """add_log вызывает logger для файлового логирования"""
        body = self._get_add_log_body()
        self.assertIn("logger", body)
        # Должен быть вызов до update_log (до self.after)
        logger_pos = body.index("logger")
        after_pos = body.index("self.after")
        self.assertLess(logger_pos, after_pos,
                        "logger должен вызываться до self.after (до UI-обновления)")

    def test_generation_prefix(self):
        """Сообщения содержат префикс [GENERATION]"""
        body = self._get_add_log_body()
        self.assertIn("[GENERATION]", body)

    def test_success_maps_to_info(self):
        """level='success' маппится на logger.info"""
        body = self._get_add_log_body()
        self.assertIn("success", body)
        self.assertIn("info", body)


if __name__ == "__main__":
    unittest.main()
