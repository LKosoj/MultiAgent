#!/usr/bin/env python3
"""
Скрипт запуска Streamlit приложения MultiAgent System
===================================================

Этот скрипт обеспечивает правильный запуск Streamlit UI с необходимыми проверками.
"""

import os
import sys
import subprocess
import warnings
from pathlib import Path

# Фильтруем предупреждения Streamlit о ScriptRunContext в потоках
warnings.filterwarnings("ignore", message=".*missing ScriptRunContext.*")

def check_virtual_env():
    """Проверка активации виртуального окружения"""
    venv_active = os.environ.get('VIRTUAL_ENV') is not None
    
    if not venv_active:
        print("❌ Виртуальное окружение не активировано!")
        print("Пожалуйста, активируйте окружение:")
        print("  source .venv/bin/activate")
        print()
        sys.exit(1)
    
    print("✅ Виртуальное окружение активировано")
    print(f"   Путь: {os.environ['VIRTUAL_ENV']}")

def check_dependencies():
    """Проверка наличия необходимых зависимостей"""
    required_packages = [
        'streamlit',
        'pandas', 
        'plotly'
    ]
    
    missing_packages = []
    
    for package in required_packages:
        try:
            __import__(package)
            print(f"✅ {package} установлен")
        except ImportError:
            missing_packages.append(package)
            print(f"❌ {package} не найден")
    
    if missing_packages:
        print()
        print("Установите недостающие пакеты:")
        print(f"  pip install {' '.join(missing_packages)}")
        print()
        sys.exit(1)

def check_project_structure():
    """Проверка структуры проекта"""
    current_dir = Path.cwd()
    required_dirs = [
        'agent_profiles',
        'workflow_pipelines', 
        'custom_tools',
        'db_plugins',
        'memory',
        'streamlit_app'
    ]
    
    missing_dirs = []
    
    for dir_name in required_dirs:
        dir_path = current_dir / dir_name
        if dir_path.exists():
            print(f"✅ {dir_name}/ найдена")
        else:
            missing_dirs.append(dir_name)
            print(f"❌ {dir_name}/ не найдена")
    
    if missing_dirs:
        print()
        print("Запустите скрипт из корневой директории проекта MultiAgent")
        print()
        sys.exit(1)

def check_streamlit_app():
    """Проверка файлов Streamlit приложения"""
    streamlit_dir = Path.cwd() / 'streamlit_app'
    
    if not streamlit_dir.exists():
        print("❌ Директория streamlit_app не найдена")
        sys.exit(1)
    
    app_file = streamlit_dir / 'app.py'
    if not app_file.exists():
        print("❌ Файл streamlit_app/app.py не найден")
        sys.exit(1)
    
    print("✅ Streamlit приложение найдено")

def check_and_mark_incomplete_traces():
    """Проверка и пометка незавершенных трасс"""
    try:
        print("🔍 Проверка трасс на незавершенные...")

        # Импортируем менеджер телеметрии
        try:
            from telemetry import get_telemetry_manager
        except ImportError:
            print("⚠️ Модуль телеметрии недоступен - пропуск проверки трасс")
            return

        # Получаем менеджер телеметрии
        try:
            telemetry_manager = get_telemetry_manager()
        except Exception as e:
            print(f"⚠️ Не удалось получить менеджер телеметрии: {e} - пропуск проверки трасс")
            return

        if not telemetry_manager.is_enabled():
            print("⚠️ Телеметрия отключена - пропуск проверки трасс")
            return

        # Проверяем и помечаем незавершенные трассы
        try:
            result = telemetry_manager.check_and_mark_incomplete_traces()
        except Exception as e:
            print(f"⚠️ Ошибка при вызове check_and_mark_incomplete_traces: {e} - пропуск проверки трасс")
            return

        total = result.get("total_traces", 0)
        incomplete = result.get("incomplete_traces", 0)
        marked = len(result.get("marked_traces", []))
        errors = result.get("errors", [])

        if total == 0:
            print("📝 Трассы не найдены")
        elif incomplete == 0:
            print(f"✅ Все {total} трасс завершены корректно")
        else:
            if marked > 0:
                print(f"🔧 Помечено {marked} незавершенных трасс как содержащих ошибки")
                for trace_info in result.get("marked_traces", []):
                    print(f"   • {trace_info['run_id']} - {trace_info['reason']}")
            else:
                print(f"⚠️ Найдено {incomplete} незавершенных трасс, но не удалось их пометить")

        if errors:
            print("⚠️ Ошибки при проверке трасс:")
            for error in errors[:3]:  # Показываем максимум 3 ошибки
                print(f"   • {error}")
            if len(errors) > 3:
                print(f"   • ... и еще {len(errors) - 3} ошибок")

    except Exception as e:
        print(f"❌ Непредвиденная ошибка при проверке трасс: {e}")
        print("⚠️ Проверка трасс пропущена - продолжаем запуск")

def run_streamlit():
    import os

    server_port = int(os.environ.get("STREAMLIT_SERVER_PORT", 8501))
    server_address = os.environ.get("STREAMLIT_SERVER_ADDRESS", "localhost")
    browser_gather_usage_stats = os.environ.get("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "False").lower() == "true"

    """Запуск Streamlit приложения"""
    print()
    print("🚀 Запуск Streamlit приложения...")
    print(f"   URL: http://{server_address}:{server_port}")
    print("   Для остановки: Ctrl+C")
    print()

    # Запускаем фоновый монитор
    monitor = None
    try:
        from streamlit_app.monitoring import get_stale_run_monitor
        monitor = get_stale_run_monitor()
        monitor.start()
        print("✅ Фоновый монитор запущен")
    except ImportError as e:
        print(f"⚠️ Не удалось импортировать модуль мониторинга: {e}")
    except Exception as e:
        print(f"⚠️ Не удалось запустить фоновый монитор: {e}")

    # Определяем команду запуска
    streamlit_cmd = [
        sys.executable, "-m", "streamlit", "run",
        "streamlit_app/app.py",
        f"--server.port={server_port}",
        f"--server.address={server_address}",
        f"--browser.gatherUsageStats={browser_gather_usage_stats}"
    ]

    try:
        # Запускаем Streamlit
        subprocess.run(streamlit_cmd, check=True)
    except KeyboardInterrupt:
        print("\n👋 Streamlit приложение остановлено")
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Ошибка запуска Streamlit: {e}")
        sys.exit(1)
    except FileNotFoundError:
        print("\n❌ Streamlit не найден. Установите его:")
        print("   pip install streamlit")
        sys.exit(1)
    finally:
        # Останавливаем монитор при выходе
        if monitor is not None:
            try:
                monitor.stop()
                print("✅ Фоновый монитор остановлен")
            except Exception as e:
                print(f"⚠️ Ошибка при остановке монитора: {e}")

def main():
    """Главная функция"""
    print("🤖 MultiAgent System - Streamlit UI")
    print("=" * 40)
    print()

    # Оптимизируем промпты
    try:
        from prompt_optimizer.prompt_optimizer import PromptOptimizer
        optimizer = PromptOptimizer()
        results = optimizer.optimize_all_agents()

        if results['optimized_successfully'] > 0:
            print(f"✅ Оптимизировано агентов: {results['optimized_successfully']}")
    except ImportError:
        print("⚠️ Модуль оптимизации промптов недоступен - пропуск оптимизации")
    except Exception as e:
        print(f"⚠️ Ошибка при оптимизации промптов: {e}")

    # Выполняем проверки
    print("🔍 Проверка окружения...")
    check_virtual_env()
    print()

    print("📦 Проверка зависимостей...")
    check_dependencies()
    print()

    print("📁 Проверка структуры проекта...")
    check_project_structure()
    print()

    print("🎯 Проверка Streamlit приложения...")
    check_streamlit_app()
    print()

    print("🔍 Проверка состояния трасс...")
    check_and_mark_incomplete_traces()

    # Запускаем приложение
    run_streamlit()

if __name__ == "__main__":
    main()
