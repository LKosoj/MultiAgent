"""
Настройка логирования для StoryBook Manager
==========================================
"""

import logging
import logging.handlers
from pathlib import Path
from config.settings import app_settings

def setup_logging():
    """Настраивает систему логирования"""
    
    # Создаем директорию для логов
    logs_dir = app_settings.get_logs_directory()
    logs_dir.mkdir(parents=True, exist_ok=True)
    
    # Определяем уровень логирования
    log_level = getattr(logging, app_settings.get("log_level", "INFO").upper())
    
    # Создаем форматтер
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Настраиваем root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    # Очищаем существующие handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Консольный handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # Файловый handler с ротацией
    log_file = logs_dir / "storybook_manager.log"
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, 
        maxBytes=10*1024*1024,  # 10 MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    # Отдельный handler для ошибок
    error_log_file = logs_dir / "storybook_manager_errors.log"
    error_handler = logging.handlers.RotatingFileHandler(
        error_log_file,
        maxBytes=5*1024*1024,  # 5 MB
        backupCount=3,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root_logger.addHandler(error_handler)
    
    # Подавляем излишне болтливые логи сторонних библиотек
    logging.getLogger('PIL').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    
    logging.info("Система логирования инициализирована")
