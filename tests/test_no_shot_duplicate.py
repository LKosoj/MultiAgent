"""
Тесты для P2.8: удаление дубликата секции 'shot'.
"""

import json
import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

CONFIG_PATH = project_root / "StoryBookManager" / "config" / "ui_config.json"


class TestNoShotDuplicate(unittest.TestCase):

    def test_no_shot_section(self):
        """Секция 'shot' (дубликат 'shots') отсутствует"""
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = json.load(f)
        self.assertNotIn("shot", config, "'shot' section should be removed")

    def test_shots_section_exists(self):
        """Секция 'shots' (основная) присутствует"""
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = json.load(f)
        self.assertIn("shots", config)

    def test_shot_type_combobox_in_shots(self):
        """shot_type_field — combobox с values в shots"""
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = json.load(f)
        items_cfg = config["shots"]["field_config"]["items"]
        st = items_cfg.get("shot_type_field", {})
        self.assertEqual(st.get("widget"), "combobox")
        self.assertIn("start", st.get("values", []))
        self.assertIn("end", st.get("values", []))

    def test_composition_stability_combobox_in_shots(self):
        """composition_stability_field — combobox с values в shots"""
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = json.load(f)
        items_cfg = config["shots"]["field_config"]["items"]
        cs = items_cfg.get("composition_stability_field", {})
        self.assertEqual(cs.get("widget"), "combobox")
        self.assertIn("stable", cs.get("values", []))


if __name__ == "__main__":
    unittest.main()
