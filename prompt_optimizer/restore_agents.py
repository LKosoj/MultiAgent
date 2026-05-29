#!/usr/bin/env python3
"""Восстановление агентов из резервных копий"""

import argparse
import logging
import sys
import yaml
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional

def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format='%(levelname)s - %(message)s')

def list_backup_files(backup_dir: Path) -> Dict[str, List[Path]]:
    """
    Сканирует директорию резервных копий и группирует файлы по агентам.
    
    Returns:
        Словарь {имя_агента: [список_файлов_резервных_копий]}
    """
    if not backup_dir.exists():
        return {}
    
    backups = {}
    
    for backup_file in backup_dir.glob("*.yaml"):
        # Формат файла: {agent_name}_backup_{timestamp}.yaml
        filename = backup_file.stem
        if '_backup_' in filename:
            agent_name = filename.split('_backup_')[0]
            if agent_name not in backups:
                backups[agent_name] = []
            backups[agent_name].append(backup_file)
    
    # Сортируем файлы по времени создания (новые первые)
    for agent_name in backups:
        backups[agent_name].sort(key=lambda x: x.stat().st_mtime, reverse=True)
    
    return backups

def parse_backup_timestamp(backup_path: Path) -> Optional[datetime]:
    """Извлекает timestamp из имени файла резервной копии."""
    try:
        filename = backup_path.stem
        if '_backup_' in filename:
            timestamp_str = filename.split('_backup_')[1]
            return datetime.strptime(timestamp_str, '%Y%m%d_%H%M%S')
    except Exception:
        pass
    return None

def show_available_backups(backup_dir: Path):
    """Показывает доступные резервные копии."""
    backups = list_backup_files(backup_dir)
    
    if not backups:
        print(f"📁 В директории {backup_dir} не найдено резервных копий")
        return
    
    print(f"\n📁 ДОСТУПНЫЕ РЕЗЕРВНЫЕ КОПИИ в {backup_dir}:")
    print("="*60)
    
    for agent_name in sorted(backups.keys()):
        print(f"\n🤖 {agent_name}:")
        
        for backup_file in backups[agent_name]:
            timestamp = parse_backup_timestamp(backup_file)
            timestamp_str = timestamp.strftime('%Y-%m-%d %H:%M:%S') if timestamp else 'неизвестно'
            file_size = backup_file.stat().st_size
            
            print(f"  • {backup_file.name}")
            print(f"    Дата: {timestamp_str}, Размер: {file_size} байт")

def load_backup_profile(backup_path: Path) -> Optional[Dict[str, Any]]:
    """Загружает профиль из резервной копии."""
    try:
        # Сначала пробуем ruamel.yaml для совместимости с новыми резервными копиями
        try:
            from ruamel.yaml import YAML
            yaml_parser = YAML()
            with open(backup_path, 'r', encoding='utf-8') as f:
                data = yaml_parser.load(f)
                # Конвертируем ruamel.yaml объекты в стандартные Python типы
                return yaml.safe_load(yaml.dump(dict(data)))
        except ImportError:
            # Если ruamel.yaml недоступен, используем стандартный yaml
            pass
        except Exception:
            # Если ruamel.yaml не смог прочитать, пробуем стандартный yaml
            pass
        
        # Fallback на стандартный yaml.safe_load
        with open(backup_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logging.error(f"Ошибка загрузки резервной копии {backup_path}: {e}")
        return None

def restore_agent_profile(agent_name: str, backup_data: Dict[str, Any], profiles_dir: Path) -> bool:
    """
    Восстанавливает профиль агента из резервной копии.
    
    Args:
        agent_name: Имя агента
        backup_data: Данные из резервной копии
        profiles_dir: Директория с профилями агентов
        
    Returns:
        True если восстановление прошло успешно
    """
    try:
        profile_path = profiles_dir / f"{agent_name}.yaml"
        
        # Создаем резервную копию текущего состояния перед восстановлением
        if profile_path.exists():
            current_backup_path = profiles_dir.parent / "agent_profiles_backup" / f"{agent_name}_before_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"
            current_backup_path.parent.mkdir(exist_ok=True)
            
            with open(profile_path, 'r', encoding='utf-8') as f:
                current_data = yaml.safe_load(f)
            
            with open(current_backup_path, 'w', encoding='utf-8') as f:
                yaml.dump(current_data, f, default_flow_style=False, allow_unicode=True)
            
            logging.info(f"💾 Создана резервная копия текущего состояния: {current_backup_path}")
        
        # Восстанавливаем из резервной копии
        with open(profile_path, 'w', encoding='utf-8') as f:
            yaml.dump(backup_data, f, default_flow_style=False, allow_unicode=True)
        
        logging.info(f"✅ Профиль {agent_name} восстановлен из резервной копии")
        return True
        
    except Exception as e:
        logging.error(f"❌ Ошибка восстановления профиля {agent_name}: {e}")
        return False

def restore_specific_agents(agent_names: List[str], backup_dir: Path, profiles_dir: Path, interactive: bool = True) -> Dict[str, Any]:
    """
    Восстанавливает указанных агентов из последних резервных копий.
    
    Args:
        agent_names: Список имен агентов для восстановления
        backup_dir: Директория с резервными копиями
        profiles_dir: Директория с профилями агентов
        interactive: Запрашивать подтверждение для каждого агента
        
    Returns:
        Словарь с результатами восстановления
    """
    results = {
        'total_agents': len(agent_names),
        'restored_successfully': 0,
        'failed_restorations': 0,
        'skipped_agents': 0,
        'agent_results': {},
        'start_time': datetime.now().isoformat()
    }
    
    backups = list_backup_files(backup_dir)
    
    for agent_name in agent_names:
        if agent_name not in backups:
            logging.warning(f"⚠️ Резервные копии для агента {agent_name} не найдены")
            results['skipped_agents'] += 1
            results['agent_results'][agent_name] = {'status': 'no_backup', 'reason': 'No backup files found'}
            continue
        
        # Берем самую свежую резервную копию
        latest_backup = backups[agent_name][0]
        timestamp = parse_backup_timestamp(latest_backup)
        timestamp_str = timestamp.strftime('%Y-%m-%d %H:%M:%S') if timestamp else 'неизвестно'
        
        print(f"\n🔄 Восстановление агента: {agent_name}")
        print(f"   Резервная копия: {latest_backup.name}")
        print(f"   Дата создания: {timestamp_str}")
        
        if interactive:
            response = input("   Продолжить восстановление? (y/N): ")
            if response.lower() != 'y':
                logging.info(f"⏭️ Пропускаем восстановление агента {agent_name}")
                results['skipped_agents'] += 1
                results['agent_results'][agent_name] = {'status': 'skipped', 'reason': 'User declined'}
                continue
        
        # Загружаем данные из резервной копии
        backup_data = load_backup_profile(latest_backup)
        if not backup_data:
            results['failed_restorations'] += 1
            results['agent_results'][agent_name] = {'status': 'failed', 'reason': 'Failed to load backup'}
            continue
        
        # Восстанавливаем профиль
        if restore_agent_profile(agent_name, backup_data, profiles_dir):
            results['restored_successfully'] += 1
            results['agent_results'][agent_name] = {
                'status': 'success',
                'backup_file': latest_backup.name,
                'backup_timestamp': timestamp_str
            }
        else:
            results['failed_restorations'] += 1
            results['agent_results'][agent_name] = {'status': 'failed', 'reason': 'Restoration failed'}
    
    results['end_time'] = datetime.now().isoformat()
    return results

def main():
    """Основная функция командной строки."""
    parser = argparse.ArgumentParser(
        description="Восстановление агентов из резервных копий",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:

  # Показать доступные резервные копии
  python restore_agents.py --list

  # Восстановить конкретных агентов (с интерактивным подтверждением)
  python restore_agents.py --agents manager analyst

  # Восстановить без подтверждения
  python restore_agents.py --agents manager --force

  # Указать custom директорию резервных копий
  python restore_agents.py --list --backup-dir custom_backups
        """
    )
    
    parser.add_argument(
        '--list', '-l',
        action='store_true',
        help='Показать список доступных резервных копий'
    )
    
    parser.add_argument(
        '--agents',
        nargs='+',
        metavar='AGENT',
        help='Список агентов для восстановления'
    )
    
    parser.add_argument(
        '--backup-dir',
        default='../agent_profiles_backup',
        help='Директория с резервными копиями (по умолчанию: ../agent_profiles_backup)'
    )
    
    parser.add_argument(
        '--profiles-dir',
        default='../agent_profiles',
        help='Директория с профилями агентов (по умолчанию: ../agent_profiles)'
    )
    
    parser.add_argument(
        '--force', '-f',
        action='store_true',
        help='Восстановить без интерактивного подтверждения'
    )
    
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Подробное логирование'
    )
    
    args = parser.parse_args()
    
    # Настройка логирования
    setup_logging(args.verbose)
    
    # Проверяем пути
    backup_dir = Path(args.backup_dir)
    profiles_dir = Path(args.profiles_dir)
    
    if not profiles_dir.exists():
        print(f"❌ Директория профилей не найдена: {profiles_dir}")
        sys.exit(1)
    
    # Показать список резервных копий
    if args.list:
        show_available_backups(backup_dir)
        return
    
    # Проверка аргументов для восстановления
    if not args.agents:
        print("❌ Необходимо указать --agents для восстановления или --list для просмотра")
        print("Используйте --help для справки")
        return
    
    if not backup_dir.exists():
        print(f"❌ Директория резервных копий не найдена: {backup_dir}")
        sys.exit(1)
    
    print("\n🔄 ВОССТАНОВЛЕНИЕ АГЕНТОВ ИЗ РЕЗЕРВНЫХ КОПИЙ")
    print("="*60)
    print(f"Агенты для восстановления: {', '.join(args.agents)}")
    print(f"Директория резервных копий: {backup_dir}")
    print(f"Директория профилей: {profiles_dir}")
    print(f"Интерактивный режим: {'НЕТ' if args.force else 'ДА'}")
    print("="*60)
    
    if not args.force:
        print("\n⚠️  ВНИМАНИЕ: Текущие профили агентов будут заменены данными из резервных копий!")
        print("Перед восстановлением будут созданы резервные копии текущего состояния.")
        response = input("\nПродолжить? (y/N): ")
        if response.lower() != 'y':
            print("Операция отменена")
            return
    
    try:
        # Запускаем восстановление
        results = restore_specific_agents(
            args.agents, 
            backup_dir, 
            profiles_dir, 
            interactive=(not args.force)
        )
        
        # Выводим итог
        print("\n" + "="*60)
        print("🎉 ВОССТАНОВЛЕНИЕ ЗАВЕРШЕНО!")
        print("="*60)
        print(f"📊 Всего агентов: {results['total_agents']}")
        print(f"✅ Успешно восстановлено: {results['restored_successfully']}")
        print(f"❌ Ошибки: {results['failed_restorations']}")
        print(f"⏭️  Пропущено: {results['skipped_agents']}")
        print("="*60)
        
        # Детализация по агентам
        for agent_name, result in results['agent_results'].items():
            status_icon = {'success': '✅', 'failed': '❌', 'skipped': '⏭️', 'no_backup': '📁'}
            icon = status_icon.get(result['status'], '❓')
            print(f"{icon} {agent_name}: {result['status']}")
            if 'reason' in result:
                print(f"    Причина: {result['reason']}")
            elif 'backup_file' in result:
                print(f"    Из файла: {result['backup_file']}")
        
        # Возвращаем код выхода
        if results['failed_restorations'] > 0:
            sys.exit(1)
        elif results['restored_successfully'] == 0:
            sys.exit(2)
        else:
            sys.exit(0)
            
    except KeyboardInterrupt:
        print("\n⚠️ Операция прервана пользователем")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
        logging.exception("Критическая ошибка при восстановлении")
        sys.exit(1)


if __name__ == "__main__":
    main()
