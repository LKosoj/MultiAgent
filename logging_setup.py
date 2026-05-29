"""
Настройка логирования для smolagents
===================================

Этот файл демонстрирует различные способы включения и настройки
логирования в библиотеке smolagents.
"""

import os
import logging
from datetime import datetime
from pathlib import Path

# Импортируем необходимые компоненты smolagents
from smolagents import CodeAgent, OpenAIServerModel, logger

def setup_smolagents_logging(level=logging.INFO, log_to_file=False, log_dir="logs"):
    """
    Настраивает логирование для smolagents с различными опциями.
    
    Args:
        level: Уровень логирования (DEBUG, INFO, WARNING, ERROR)
        log_to_file: Сохранять логи в файл
        log_dir: Директория для сохранения логов
    """
    
    # 1. Базовая настройка логирования для smolagents
    smolagents_logger = logging.getLogger('smolagents')
    smolagents_logger.setLevel(level)
    
    # Очищаем существующие обработчики, чтобы избежать дублирования
    for handler in smolagents_logger.handlers[:]:
        smolagents_logger.removeHandler(handler)
    
    # 2. Настройка форматирования логов
    formatter = logging.Formatter(
        '[%(asctime)s] %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 3. Консольный вывод
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    smolagents_logger.addHandler(console_handler)
    
    # 4. Логирование в файл (опционально)
    if log_to_file:
        # Создаем директорию для логов
        log_path = Path(log_dir)
        log_path.mkdir(exist_ok=True)
        
        # Имя файла с текущей датой
        log_filename = f"smolagents_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        log_file_path = log_path / log_filename
        
        file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        smolagents_logger.addHandler(file_handler)
        
        print(f"📝 Логи smolagents сохраняются в: {log_file_path}")
    
    # 5. Предотвращаем передачу логов в родительские логгеры
    smolagents_logger.propagate = False
    
    print(f"🔧 Логирование smolagents настроено. Уровень: {logging.getLevelName(level)}")
    return smolagents_logger


def setup_comprehensive_logging(log_level=logging.DEBUG, log_to_file=False, log_dir="logs"):
    """
    Комплексная настройка логирования для всего проекта,
    включая smolagents и другие компоненты.
    """
    
    # Для базового уровня используем тот же уровень, что и для smolagents,
    # но не ниже INFO (чтобы не спамить DEBUG сообщениями от всех библиотек)
    if log_level == logging.DEBUG:
        base_log_level = logging.INFO
    elif log_level == logging.INFO:
        base_log_level = logging.INFO
    else:
        base_log_level = logging.WARNING

    # Базовая настройка для всего проекта
    logging.basicConfig(
        level=base_log_level,
        format='[%(asctime)s] %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Специальная настройка для smolagents
    setup_smolagents_logging(level=log_level, log_to_file=log_to_file)
    
    # Настройка логирования для других компонентов проекта
    loggers_config = {
        'agent_system': base_log_level,
        'agent_factory': base_log_level,
        'memory.rag_memory': base_log_level,
        'custom_tools.text_to_sql': base_log_level,
        'custom_tools.brainstorm_tool': base_log_level,
        'retry_openai_model': base_log_level,
    }
    
    for logger_name, logger_level in loggers_config.items():
        logger_instance = logging.getLogger(logger_name)
        logger_instance.setLevel(logger_level)
    
    print(f"🚀 Комплексное логирование настроено для всего проекта")
    print(f"   📊 Уровень smolagents: {logging.getLevelName(log_level)}")
    print(f"   📊 Базовый уровень: {logging.getLevelName(base_log_level)}")


def demo_logging_levels():
    """
    Демонстрирует различные уровни логирования smolagents.
    """
    print("\n" + "="*60)
    print("🔍 ДЕМОНСТРАЦИЯ УРОВНЕЙ ЛОГИРОВАНИЯ SMOLAGENTS")
    print("="*60)
    
    levels = [
        (logging.DEBUG, "DEBUG - максимальная детализация"),
        (logging.INFO, "INFO - основная информация"),
        (logging.WARNING, "WARNING - только предупреждения и ошибки"),
        (logging.ERROR, "ERROR - только ошибки"),
    ]
    
    for level, description in levels:
        print(f"\n📊 {description}")
        print("-" * 40)
        
        # Настройка логирования на конкретном уровне
        setup_smolagents_logging(level=level)
        
        # Примеры логов разных уровней (эмуляция работы smolagents)
        smolagents_logger = logging.getLogger('smolagents')
        
        smolagents_logger.debug("🔍 DEBUG: Детальная отладочная информация")
        smolagents_logger.info("ℹ️ INFO: Общая информация о работе")
        smolagents_logger.warning("⚠️ WARNING: Предупреждение о потенциальной проблеме")
        smolagents_logger.error("❌ ERROR: Критическая ошибка")
        
        print()


def setup_environment_based_logging():
    """
    Настройка логирования на основе переменных окружения.
    Удобно для разных сред: development, production, testing.
    """
    
    # Получаем уровень логирования из переменной окружения
    log_level_str = os.getenv('SMOLAGENTS_LOG_LEVEL', 'INFO').upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    
    # Определяем, нужно ли сохранять в файл
    log_to_file = os.getenv('SMOLAGENTS_LOG_TO_FILE', 'false').lower() == 'true'
    
    # Директория для логов
    log_dir = os.getenv('SMOLAGENTS_LOG_DIR', 'logs')
    
    setup_smolagents_logging(
        level=log_level,
        log_to_file=log_to_file,
        log_dir=log_dir
    )
    
    print(f"🌍 Логирование настроено на основе переменных окружения:")
    print(f"   - SMOLAGENTS_LOG_LEVEL: {log_level_str}")
    print(f"   - SMOLAGENTS_LOG_TO_FILE: {log_to_file}")
    print(f"   - SMOLAGENTS_LOG_DIR: {log_dir}")


def main():
    """
    Главная демонстрационная функция.
    """
    print("🎯 НАСТРОЙКА ЛОГИРОВАНИЯ ДЛЯ SMOLAGENTS")
    print("=" * 50)
    
    print("\n1️⃣ Быстрая настройка:")
    setup_smolagents_logging(level=logging.INFO)
    
    print("\n2️⃣ Демонстрация уровней логирования:")
    demo_logging_levels()
    
    print("\n3️⃣ Настройка через переменные окружения:")
    setup_environment_based_logging()
    
    print("\n4️⃣ Комплексная настройка проекта:")
    setup_comprehensive_logging()
    
    print("\n✅ Все методы настройки продемонстрированы!")
    print("\n📚 ИНСТРУКЦИИ ПО ИСПОЛЬЗОВАНИЮ:")
    print("""
    # В начале вашего main.py или agent_system.py добавьте:
    from logging_setup import setup_smolagents_logging
    import logging
    
    # Простая настройка
    setup_smolagents_logging(level=logging.DEBUG)
    
    # Или с сохранением в файл
    setup_smolagents_logging(level=logging.DEBUG, log_to_file=True)
    
    # Для разных сред через переменные окружения:
    # export SMOLAGENTS_LOG_LEVEL=DEBUG
    # export SMOLAGENTS_LOG_TO_FILE=true
    from logging_setup import setup_environment_based_logging
    setup_environment_based_logging()
    """)


if __name__ == "__main__":
    main()
