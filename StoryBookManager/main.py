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

def main():
    """Точка входа в приложение"""
    try:
        # Проверяем наличие корневой директории проекта MultiAgent
        if not (project_root / "plots" / "storybooks").exists():
            logger.error(f"Не найдена директория проектов: {project_root}/plots/storybooks")
            messagebox.showerror(
                "Ошибка инициализации", 
                f"Не найдена директория проектов:\n{project_root}/plots/storybooks\n\n"
                "Убедитесь, что StoryBookManager запущен из корневой директории MultiAgent проекта."
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
