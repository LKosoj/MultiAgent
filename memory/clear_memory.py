#!/usr/bin/env python3
"""
Скрипт для очистки памяти мультиагентной системы
=================================================

Этот скрипт предоставляет удобный интерфейс командной строки для:
- Просмотра статистики памяти
- Очистки памяти по session_id
- Очистки памяти конкретных агентов
- Полной очистки всей системы памяти

Использование:
    python memory/clear_memory.py --help
    python memory/clear_memory.py --stats SESSION_ID
    python memory/clear_memory.py --clear SESSION_ID
    python memory/clear_memory.py --clear SESSION_ID --agent agent_name
    python memory/clear_memory.py --clear-all
"""

import argparse
import sys
import os
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    from memory.tools import (
        clear_session_memory, 
        clear_agent_memory,
        get_session_memory_stats,
        agent_list
    )
    from memory.manager import memory_manager
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    print("Убедитесь, что вы запускаете скрипт из корневой директории проекта")
    sys.exit(1)


def format_stats(stats):
    """Форматирует статистику памяти для вывода"""
    if "error" in stats:
        return f"❌ {stats['error']}"
    
    lines = []
    lines.append(f"📊 Статистика памяти для сессии: {stats['session_id']}")
    lines.append("")
    
    # Общая статистика
    summary = stats['summary']
    lines.append("📋 Общая статистика:")
    lines.append(f"   SQLite записей: {summary['total_sqlite_records']}")
    lines.append(f"   ChromaDB записей: {summary['total_chromadb_records']}")
    lines.append(f"   Активных агентов: {summary['active_agents']}")
    lines.append("")
    
    # Тактическая память
    tactical = stats['tactical_memory']
    lines.append("🧠 Тактическая память (агенты):")
    lines.append(f"   Всего записей: {tactical['total_records']} (SQLite)")
    lines.append(f"   ChromaDB записей: {tactical['chromadb_records']}")
    
    if tactical['agents']:
        lines.append("   Агенты:")
        for agent in tactical['agents']:
            lines.append(f"     • {agent['agent_name']}: {agent['total_steps']} шагов")
    else:
        lines.append("   Агентов нет")
    lines.append("")
    
    # Стратегическая память
    strategic = stats['strategic_memory']
    lines.append("🎯 Стратегическая память:")
    lines.append(f"   Всего записей: {strategic['total_records']} (SQLite)")
    lines.append(f"   ChromaDB записей: {strategic['chromadb_records']}")
    
    if strategic['by_type']:
        lines.append("   По типам:")
        for mem_type, statuses in strategic['by_type'].items():
            for status, count in statuses.items():
                lines.append(f"     • {mem_type} ({status}): {count}")
    else:
        lines.append("   Записей нет")
    
    return "\n".join(lines)


def show_stats(session_id):
    """Показывает статистику памяти для сессии"""
    print(f"🔍 Получаем статистику памяти для сессии: {session_id}")
    
    try:
        stats = get_session_memory_stats(session_id)
        print(format_stats(stats))
        return True
    except Exception as e:
        print(f"❌ Ошибка получения статистики: {e}")
        return False


def clear_session(session_id, agent_name=None, step=None, memory_type="all"):
    """Очищает память сессии или конкретного агента"""
    if agent_name:
        print(f"🧹 Очищаем память агента '{agent_name}' в сессии: {session_id}")
        if step is not None:
            print(f"   Шаг: {step}")
        
        try:
            result = clear_agent_memory(session_id, agent_name, step)
            print(result)
            return True
        except Exception as e:
            print(f"❌ Ошибка очистки памяти агента: {e}")
            return False
    else:
        print(f"🧹 Очищаем {memory_type} память для сессии: {session_id}")
        
        try:
            result = clear_session_memory(session_id, memory_type)
            print(result)
            return True
        except Exception as e:
            print(f"❌ Ошибка очистки памяти сессии: {e}")
            return False


def clear_all_memory():
    """Очищает всю память системы"""
    print("⚠️  ВНИМАНИЕ: Это удалит ВСЮ память системы!")
    confirm = input("Вы уверены? Введите 'да' для подтверждения: ")
    
    if confirm.lower() not in ['да', 'yes', 'y']:
        print("❌ Операция отменена")
        return False
    
    print("🧹 Очищаем всю память системы...")
    
    try:
        # Получаем все session_id из БД
        conn = memory_manager.db_handler._get_connection()
        try:
            cursor = conn.cursor()
            
            # Получаем уникальные session_id из тактической памяти
            cursor.execute("SELECT DISTINCT session_id FROM agent_memory")
            tactical_sessions = {row[0] for row in cursor.fetchall()}
            
            # Получаем уникальные session_id из стратегической памяти
            cursor.execute("SELECT DISTINCT session_id FROM strategic_memory")
            strategic_sessions = {row[0] for row in cursor.fetchall()}
            
            all_sessions = tactical_sessions | strategic_sessions
            
            if not all_sessions:
                print("✅ Память уже пуста")
                return True
            
            print(f"📋 Найдено сессий для очистки: {len(all_sessions)}")
            
            total_cleared = 0
            for session_id in all_sessions:
                print(f"   🧹 Очищаем сессию: {session_id}")
                try:
                    result = clear_session_memory(session_id)
                    print(f"      ✅ {result.split(':')[0]}")
                    total_cleared += 1
                except Exception as e:
                    print(f"      ❌ Ошибка: {e}")
            
            print(f"\n✅ Очистка завершена. Обработано сессий: {total_cleared}/{len(all_sessions)}")
            return True
            
        finally:
            conn.close()
            
    except Exception as e:
        print(f"❌ Ошибка очистки всей памяти: {e}")
        return False


def list_sessions():
    """Показывает список всех сессий в памяти"""
    print("📋 Список всех сессий в памяти:")
    
    try:
        conn = memory_manager.db_handler._get_connection()
        try:
            cursor = conn.cursor()
            
            # Получаем статистику по сессиям
            cursor.execute("""
                SELECT 
                    session_id,
                    COUNT(*) as total_records,
                    MIN(valid_from) as first_entry,
                    MAX(valid_from) as last_entry
                FROM agent_memory 
                WHERE valid_to IS NULL
                GROUP BY session_id
                ORDER BY last_entry DESC
            """)
            
            tactical_sessions = {}
            for row in cursor.fetchall():
                tactical_sessions[row[0]] = {
                    'tactical_records': row[1],
                    'first_entry': row[2],
                    'last_entry': row[3]
                }
            
            cursor.execute("""
                SELECT 
                    session_id,
                    COUNT(*) as total_records
                FROM strategic_memory 
                WHERE valid_to IS NULL
                GROUP BY session_id
            """)
            
            strategic_sessions = {}
            for row in cursor.fetchall():
                strategic_sessions[row[0]] = row[1]
            
            all_sessions = set(tactical_sessions.keys()) | set(strategic_sessions.keys())
            
            if not all_sessions:
                print("   📭 Сессий не найдено")
                return
            
            for i, session_id in enumerate(sorted(all_sessions, 
                                                 key=lambda x: tactical_sessions.get(x, {}).get('last_entry', ''), 
                                                 reverse=True), 1):
                tactical_info = tactical_sessions.get(session_id, {})
                strategic_count = strategic_sessions.get(session_id, 0)
                
                print(f"\n   {i:2d}. {session_id}")
                print(f"       🧠 Тактическая память: {tactical_info.get('tactical_records', 0)} записей")
                print(f"       🎯 Стратегическая память: {strategic_count} записей")
                
                if tactical_info.get('last_entry'):
                    print(f"       📅 Последняя активность: {tactical_info['last_entry']}")
                    
        finally:
            conn.close()
            
    except Exception as e:
        print(f"❌ Ошибка получения списка сессий: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Скрипт для управления памятью мультиагентной системы",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:

  # Показать список всех сессий
  python memory/clear_memory.py --list

  # Показать статистику конкретной сессии
  python memory/clear_memory.py --stats "duckdb_users_kosoj_documents_multiagent_data_sber_index_prod_db"

  # Очистить всю память сессии
  python memory/clear_memory.py --clear "session_id_here"

  # Очистить только тактическую память сессии
  python memory/clear_memory.py --clear "session_id_here" --type tactical

  # Очистить память конкретного агента
  python memory/clear_memory.py --clear "session_id_here" --agent "schema_rag_agent"

  # Очистить конкретный шаг агента
  python memory/clear_memory.py --clear "session_id_here" --agent "nlu_agent" --step 2

  # ОПАСНО: Очистить всю память системы
  python memory/clear_memory.py --clear-all
        """
    )
    
    parser.add_argument('--list', action='store_true',
                        help='Показать список всех сессий в памяти')
    
    parser.add_argument('--stats', type=str, metavar='SESSION_ID',
                        help='Показать статистику памяти для сессии')
    
    parser.add_argument('--clear', type=str, metavar='SESSION_ID',
                        help='Очистить память сессии')
    
    parser.add_argument('--agent', type=str, metavar='AGENT_NAME',
                        help='Имя конкретного агента для очистки (используется с --clear)')
    
    parser.add_argument('--step', type=int, metavar='STEP_NUMBER',
                        help='Номер шага агента для очистки (используется с --clear и --agent)')
    
    parser.add_argument('--type', type=str, choices=['tactical', 'strategic', 'all'], 
                        default='all', metavar='MEMORY_TYPE',
                        help='Тип памяти для очистки: tactical, strategic или all (по умолчанию: all)')
    
    parser.add_argument('--clear-all', action='store_true',
                        help='⚠️ ОПАСНО: Очистить ВСЮ память системы')
    
    args = parser.parse_args()
    
    # Проверка аргументов
    if not any([args.list, args.stats, args.clear, args.clear_all]):
        parser.print_help()
        return
    
    if args.step and not args.agent:
        print("❌ Ошибка: --step можно использовать только вместе с --agent")
        return
    
    if args.agent and not args.clear:
        print("❌ Ошибка: --agent можно использовать только вместе с --clear")
        return
    
    # Выполнение команд
    success = True
    
    if args.list:
        list_sessions()
    
    if args.stats:
        success = show_stats(args.stats) and success
    
    if args.clear:
        success = clear_session(args.clear, args.agent, args.step, args.type) and success
    
    if args.clear_all:
        success = clear_all_memory() and success
    
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
