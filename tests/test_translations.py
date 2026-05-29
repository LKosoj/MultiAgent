"""
Тесты для P2.7: переводы полей в ui_config.json.

Проверяет наличие переводов для всех требуемых полей.
"""

import json
import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

CONFIG_PATH = project_root / "StoryBookManager" / "config" / "ui_config.json"


class TestTranslations(unittest.TestCase):

    def setUp(self):
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            self.config = json.load(f)
            self.translations = self.config.get("translations", {})

    def test_prompt_fields_translated(self):
        for field in ["english_prompt", "video_prompt", "negative_prompt"]:
            self.assertIn(field, self.translations, f"Перевод для '{field}' отсутствует")

    def test_shot_fields_translated(self):
        for field in ["shot_type", "atmosphere", "lighting", "camera_position"]:
            self.assertIn(field, self.translations, f"Перевод для '{field}' отсутствует")

    def test_screenplay_fields_translated(self):
        for field in ["scene_number", "shot_number", "storyboard",
                      "dialogue", "action", "sound", "transition"]:
            self.assertIn(field, self.translations, f"Перевод для '{field}' отсутствует")

    def test_character_fields_translated(self):
        for field in ["gesture_set", "speech_patterns", "no_go_rules",
                      "key_objects", "color_palette"]:
            self.assertIn(field, self.translations, f"Перевод для '{field}' отсутствует")

    def test_translations_are_russian(self):
        """Переводы содержат кириллицу"""
        for key, value in self.translations.items():
            has_cyrillic = any('\u0400' <= c <= '\u04ff' for c in value)
            self.assertTrue(has_cyrillic, f"'{key}': '{value}' не содержит кириллицу")

    def test_all_config_fields_are_translated(self):
        """Проверяет, что все поля из field_groups и field_config имеют переводы, 
        исключая появление snake_case или CamelCase в интерфейсе."""
        missing = set()
        for section_name, section_data in self.config.items():
            if section_name in ['translations', 'editor_settings', 'field_labels']:
                continue
                
            if 'field_groups' in section_data:
                for group_name, group_data in section_data['field_groups'].items():
                    for field in group_data.get('fields', []):
                        if field not in self.translations:
                            missing.add(field)
            
            if 'field_config' in section_data:
                for field in section_data['field_config'].keys():
                    if field not in self.translations:
                        missing.add(field)
        
        self.assertEqual(missing, set(), f"Найдены поля без перевода (snake_case/CamelCase в UI): {missing}")


if __name__ == "__main__":
    unittest.main()
