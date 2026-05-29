import os
import sys
import subprocess
import logging

def file_write(session_id: str, filename: str, content: str, append: bool = False) -> str:
    """Записывает содержимое в файл в каталог 'plots'. Если append=True, дописывает в конец файла.
    
    Args:
        session_id: Идентификатор сессии для отслеживания выполнения задач
        filename: Путь к файлу
        content: Содержимое файла
        append: Если True, дописывает в конец файла, иначе перезаписывает (опционально, по умолчанию False)
    Returns:
        str: Сообщение о результате записи файла
    """
    # Разрешены только два варианта:
    # 1) путь начинается с 'plots/'
    # 2) указан только файл без каталога — тогда добавляем 'plots/'
    allowed_root = 'plots'
    rel_filename = filename.lstrip(os.sep)
    normalized_rel = os.path.normpath(rel_filename) if rel_filename else ''

    if not normalized_rel or normalized_rel in ('.', '..'):
        return f"Ошибка записи файла: не указано корректное имя файла: {filename}"

    if normalized_rel.startswith(allowed_root + os.sep):
        filename = normalized_rel
    elif os.sep in normalized_rel:
        return ("Ошибка записи файла: путь должен начинаться с 'plots/' "
                "или быть только именем файла без каталога: " + str(filename))
    elif normalized_rel == allowed_root:
        return ("Ошибка записи файла: требуется имя файла, например 'plots/name.png' "
                "или 'name.png'")
    else:
        filename = os.path.join(allowed_root, normalized_rel)

    dir_to_create = os.path.dirname(filename) or allowed_root
    os.makedirs(dir_to_create, exist_ok=True)

    filename_with_session = filename

    # Проверяем, если в имени файла нет идентификатора сессии, то добавляем его
    if session_id not in filename:
        # Получаем имя файла без расширения
        filename_without_ext = os.path.splitext(filename)[0]
        # Получаем расширение файла
        file_extension = os.path.splitext(filename)[1]
        # Добавляем идентификатор сессии в конец имени файла перед расширением
        filename_with_session = f"{filename_without_ext}_{session_id}{file_extension}"

    mode = 'a' if append else 'w'
    with open(filename_with_session, mode, encoding='utf-8') as f:
        f.write(content)
    
    return f"Файл {filename_with_session} успешно {'дополнен' if append else 'создан'}"

def file_read(session_id: str, filename: str) -> str:
    """Читает содержимое файла из разрешённых директорий ('plots')

    Args:
        session_id: Идентификатор сессии для отслеживания выполнения задач
        filename: Путь к файлу

    Returns:
        str: Содержимое файла или сообщение об ошибке
    """
    # Разрешены только два варианта пути:
    # 1) путь начинается с 'plots/'
    # 2) указано только имя файла — тогда читаем из 'plots/'
    allowed_root = 'plots'
    rel_filename = filename.lstrip(os.sep)
    normalized_rel = os.path.normpath(rel_filename) if rel_filename else ''

    if not normalized_rel or normalized_rel in ('.', '..'):
        return f"Ошибка чтения файла: не указано корректное имя файла: {filename}"

    if normalized_rel.startswith(allowed_root + os.sep):
        filename = normalized_rel
    elif os.sep in normalized_rel:
        return ("Ошибка чтения файла: путь должен начинаться с 'plots/' "
                "или быть только именем файла без каталога: " + str(filename))
    elif normalized_rel == allowed_root:
        return ("Ошибка чтения файла: требуется имя файла, например 'plots/name.png' "
                "или 'name.png'")
    else:
        filename = os.path.join(allowed_root, normalized_rel)

    filename_with_session = filename

    # Проверяем, если в имени файла нет идентификатора сессии, то добавляем его
    if session_id not in filename:
        # Получаем имя файла без расширения
        filename_without_ext = os.path.splitext(filename)[0]
        # Получаем расширение файла
        file_extension = os.path.splitext(filename)[1]
        # Добавляем идентификатор сессии в конец имени файла перед расширением
        filename_with_session = f"{filename_without_ext}_{session_id}{file_extension}"

    if not os.path.exists(filename_with_session):
        return f"Файл {filename_with_session} не найден"

    try:
        with open(filename_with_session, 'r', encoding='utf-8') as f:
            content = f.read()
        return content
    except Exception as e:
        return f"Ошибка при чтении файла: {str(e)}"

def file_list(session_id: str, dir_name: str) -> str:
    """Читает директорию ('plots') и возвращает список файлов в ней

    Args:
        session_id: Идентификатор сессии для отслеживания выполнения задач
        dir_name: Имя директории ('plots')

    Returns:
        str: Список файлов в директории
    """
    allowed_dirs = ['plots']
    if not any(allowed_dir in dir_name for allowed_dir in allowed_dirs):
        return f"Ошибка чтения содержимого директории, указан не корректный каталог: {dir_name}"
    try:
        # Получаем список файлов в директории
        files = os.listdir(dir_name)
        # Фильтруем список файлов, оставляя только те, которые содержат идентификатор сессии
        files = [file for file in files if session_id in file]
        return f"Список файлов в директории '{dir_name}': {files}"
    except Exception as e:
        return f"Ошибка при получении списка файлов: {str(e)}"


# Список потенциально опасных пакетов для системной безопасности
DANGEROUS_PACKAGES = {
    # Пакеты для выполнения системных команд
    'plumbum', 'pexpect', 'sh', 'invoke', 'fabric', 'paramiko',
    
    # Пакеты для работы с процессами и системой
    'psutil', 'supervisor', 'daemon', 'python-daemon',
    
    # Пакеты для удаленного выполнения кода
    'rpyc', 'pyro4', 'celery', 'rq', 'dramatiq',
    
    # Пакеты для работы с файловой системой (потенциально опасные)
    'pathlib2', 'send2trash', 'watchdog', 'scandir',
    
    # Сетевые пакеты с возможностью атак
    'scapy', 'nmap', 'python-nmap', 'netaddr', 'netifaces',
    
    # Пакеты для работы с безопасностью (могут быть злоупотреблены)
    'keyring', 'cryptography', 'pycrypto', 'pycryptodome',
    
    # Пакеты для веб-скрейпинга (могут перегружать сервера)
    'scrapy', 'selenium', 'playwright', 'pyautogui',
    
    # Пакеты для работы с базами данных (могут повредить данные)
    'sqlalchemy-utils', 'alembic', 'migrate',
    
    # Пакеты для компиляции и выполнения кода
    'cython', 'numba', 'cffi', 'pybind11',
    
    # Пакеты для работы с архивами (zip bombs)
    'zipfile36', 'patool', 'pyunpack',
    
    # Другие потенциально опасные пакеты
    'schedule', 'APScheduler', 'crontab', 'python-crontab'
}

# Безопасные пакеты для анализа данных и научных вычислений
SAFE_PACKAGES = {
    'numpy', 'pandas', 'matplotlib', 'seaborn', 'plotly', 'bokeh',
    'scipy', 'scikit-learn', 'statsmodels', 'sympy',
    'jupyter', 'ipython', 'notebook', 'jupyterlab',
    'requests', 'urllib3', 'httpx', 'aiohttp',
    'beautifulsoup4', 'lxml', 'html5lib',
    'pillow', 'opencv-python', 'imageio',
    'openpyxl', 'xlrd', 'xlwt', 'xlsxwriter',
    'python-dateutil', 'pytz', 'arrow',
    'tqdm', 'progressbar2', 'alive-progress',
    'click', 'argparse', 'docopt',
    'pyyaml', 'toml', 'configparser',
    'pytest', 'unittest2', 'nose2',
    'black', 'autopep8', 'flake8', 'pylint',
    'mypy', 'typing-extensions',
    'rich', 'colorama', 'termcolor',
    'jsonschema', 'marshmallow', 'pydantic',
    'faker', 'factory-boy', 'mimesis'
}


def install_package(package_name: str, version: str = None) -> str:
    """
    Устанавливает Python-пакет используя pip с проверками безопасности.
    Блокирует установку потенциально опасных пакетов для системной безопасности.
    
    Args:
        package_name (str): Имя пакета для установки
        version (str, optional): Версия пакета для установки
        
    Returns:
        str: Сообщение о результате установки
    """
    # Нормализуем имя пакета (убираем лишние символы, приводим к нижнему регистру)
    clean_package_name = package_name.strip().lower().replace('_', '-')
    
    # Проверяем безопасность пакета
    if clean_package_name in DANGEROUS_PACKAGES:
        return (f"🚫 БЛОКИРОВАНО: Пакет '{package_name}' заблокирован по соображениям безопасности. "
                f"Этот пакет может выполнять системные команды или иметь другие потенциально опасные возможности. "
                f"Если установка действительно необходима, обратитесь к администратору.")
    
    # Информируем о статусе безопасности
    #safety_status = "✅ БЕЗОПАСНЫЙ" if clean_package_name in SAFE_PACKAGES else "⚠️ НЕ ОПРЕДЕЛЕН"
    safety_status = "✅ БЕЗОПАСНЫЙ"
    logging.info(f"Статус безопасности пакета '{package_name}': {safety_status}")
    
    try:
        # Проверяем, установлен ли пакет
        __import__(package_name)
        return f"Пакет '{package_name}' уже установлен ({safety_status})"
    except ImportError:
        try:
            # Формируем команду для установки
            package_spec = f"{package_name}=={version}" if version else package_name
            logging.info(f"Устанавливаем пакет: {package_spec} ({safety_status})")
            
            # Выполняем установку
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", package_spec],
                capture_output=True,
                text=True,
                timeout=300  # 5 минут таймаут
            )
            
            if result.returncode == 0:
                return f"Пакет '{package_spec}' успешно установлен ({safety_status})"
            else:
                error_msg = result.stderr.strip() if result.stderr else "Неизвестная ошибка"
                return f"Ошибка при установке пакета '{package_spec}': {error_msg}"
                
        except subprocess.TimeoutExpired:
            return f"Превышено время ожидания при установке пакета '{package_name}'"
        except Exception as e:
            return f"Ошибка при установке пакета '{package_name}': {str(e)}"
    except Exception as e:
        return f"Ошибка при проверке пакета '{package_name}': {str(e)}" 