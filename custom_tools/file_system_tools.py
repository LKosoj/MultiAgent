import os
import re
import sys
import subprocess
import logging

# Абсолютный путь к каталогу 'plots' относительно расположения модуля,
# чтобы проверка каталога не зависела от текущей рабочей директории (CWD).
_PROJECT_PLOTS = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', 'plots'))

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
    allowed_dirs = [_PROJECT_PLOTS]
    real_dir = os.path.realpath(dir_name)
    if not any(real_dir == allowed or real_dir.startswith(allowed + os.sep) for allowed in allowed_dirs):
        return f"Ошибка чтения содержимого директории, указан не корректный каталог: {dir_name}"
    try:
        # Получаем список файлов в директории
        files = os.listdir(real_dir)
        # Фильтруем список файлов, оставляя только те, которые содержат идентификатор сессии
        files = [file for file in files if session_id in file]
        return f"Список файлов в директории '{dir_name}': {files}"
    except Exception as e:
        return f"Ошибка при получении списка файлов: {str(e)}"


# Безопасные пакеты для анализа данных и научных вычислений (allow-list).
# install_package разрешает только пакеты из этого множества; прежний deny-list
# DANGEROUS_PACKAGES удалён как мёртвый — фильтрация теперь строго по allow-list.
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

    # Проверяем по разрешённому списку (allowlist): только явно разрешённые пакеты
    if clean_package_name not in SAFE_PACKAGES:
        return (f"БЛОКИРОВАНО: Пакет '{package_name}' не входит в список разрешённых пакетов. "
                f"Для установки пакета вне списка обратитесь к администратору.")

    # Валидируем версию, чтобы исключить инъекцию посторонних символов в spec
    if version is not None and not re.fullmatch(r'[A-Za-z0-9_.+-]+', version):
        return f"Ошибка: некорректная версия пакета '{package_name}': {version}"

    safety_status = "БЕЗОПАСНЫЙ"
    logging.info(f"Статус безопасности пакета '{package_name}': {safety_status}")
    
    try:
        # Проверяем, установлен ли пакет
        __import__(package_name)
        return f"Пакет '{package_name}' уже установлен ({safety_status})"
    except ImportError:
        try:
            # Формируем команду для установки
            package_spec = f"{clean_package_name}=={version}" if version else clean_package_name
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