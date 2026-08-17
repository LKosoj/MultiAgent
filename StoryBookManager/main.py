#!/usr/bin/env python3
"""
StoryBook Manager - Главное приложение
====================================

Десктопный интерфейс для управления данными storybook_pipeline.
Позволяет редактировать JSON файлы, просматривать медиа и управлять генерацией.
"""

import sys
import tkinter as tk
from tkinter import messagebox
import logging
from pathlib import Path

# Добавляем корневую директорию проекта в sys.path для импорта модулей
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Настройка логирования
from StoryBookManager.utils.logging_config import setup_logging
setup_logging()

logger = logging.getLogger(__name__)

# Импорт GUI компонентов
from StoryBookManager.gui.main_window import MainWindow


def _prepare_projects_root():
    """Выставляет STORYBOOK_PROJECTS_DIR из настроек и вычисляет корень каталога проектов.

    Без обращений к tkinter/messagebox (раздел 18.0 ТЗ) — только определение пути,
    чтобы логику можно было проверить напрямую, без запуска GUI.
    Возвращает пару (вычисленный корень, существует ли он).
    """
    import os

    from StoryBookManager.config.settings import app_settings
    from custom_tools.storybook.project_paths import storybook_projects_root

    os.environ["STORYBOOK_PROJECTS_DIR"] = str(app_settings.get_projects_directory())
    root = storybook_projects_root()
    return root, root.exists()


def main():
    """Точка входа в приложение"""
    try:
        # Проверяем наличие директории проектов, определённой настройками
        storybook_root, storybook_root_exists = _prepare_projects_root()
        if not storybook_root_exists:
            logger.error(f"Не найдена директория проектов: {storybook_root}")
            messagebox.showerror(
                "Ошибка инициализации",
                f"Не найдена директория проектов:\n{storybook_root}\n\n"
                "Проверьте настройку «Директория проектов» в диалоге настроек StoryBookManager."
            )
            return 1

        logger.info("🚀 Запуск StoryBook Manager")
        
        # Создаем главное окно приложения
        root = tk.Tk()
        app = MainWindow(root)
        
        # Запускаем основной цикл приложения
        root.mainloop()
        
        logger.info("✅ StoryBook Manager завершен")
        return 0
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
        messagebox.showerror("Критическая ошибка", f"Произошла критическая ошибка:\n{e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
