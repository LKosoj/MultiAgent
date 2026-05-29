"""
Простой тест для проверки работы агента-архивариуса
===================================================
"""

import sys
import os

# Добавляем путь к проекту
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_factory import AgentFactory
import uuid


def test_archivist_creation():
    """Тест создания агента-архивариуса"""
    print("\n" + "="*80)
    print("ТЕСТ 1: Создание агента-архивариуса")
    print("="*80)
    
    try:
        factory = AgentFactory()
        session_id = str(uuid.uuid4())
        
        archivist = factory.create_agent(
            profile_type='memory_archivist',
            session_id=session_id,
            task="Тестовая задача"
        )
        
        print(f"✅ Агент создан успешно")
        print(f"   - Имя: {archivist.name}")
        print(f"   - Профиль: {archivist.profile_type}")
        print(f"   - Сессия: {session_id}")
        print(f"   - Инструменты: {[t.name for t in archivist.tools]}")
        
        return True
    except Exception as e:
        print(f"❌ Ошибка при создании агента: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_archivist_stats():
    """Тест получения статистики памяти"""
    print("\n" + "="*80)
    print("ТЕСТ 2: Получение статистики памяти")
    print("="*80)
    
    try:
        factory = AgentFactory()
        session_id = str(uuid.uuid4())
        
        archivist = factory.create_agent(
            profile_type='memory_archivist',
            session_id=session_id,
            task="Получить статистику памяти"
        )
        
        task = "Покажи общую статистику системы памяти: количество записей, сессий и агентов."
        
        print(f"📋 Задача: {task}")
        result = archivist.run(task)
        
        print(f"\n✅ Результат получен:")
        print(f"{str(result)[:500]}...")  # Первые 500 символов
        
        return True
    except Exception as e:
        print(f"❌ Ошибка при получении статистики: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_archivist_save_global():
    """Тест сохранения глобальной информации"""
    print("\n" + "="*80)
    print("ТЕСТ 3: Сохранение глобальной информации")
    print("="*80)
    
    try:
        factory = AgentFactory()
        session_id = str(uuid.uuid4())
        
        archivist = factory.create_agent(
            profile_type='memory_archivist',
            session_id=session_id,
            task="Сохранить тестовую информацию"
        )
        
        task = """
        Сохрани следующую тестовую информацию:
        
        Тема: Тестовые данные агента-архивариуса
        Категория: reference
        Содержание: Это тестовая запись для проверки работы агента-архивариуса памяти
        Теги: [тест, архивариус, память]
        
        Сохрани это в глобальную сессию.
        """
        
        print(f"📋 Задача: сохранение тестовых данных")
        result = archivist.run(task)
        
        print(f"\n✅ Данные сохранены:")
        print(f"{str(result)[:500]}...")
        
        return True
    except Exception as e:
        print(f"❌ Ошибка при сохранении: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_archivist_list_sessions():
    """Тест получения списка сессий"""
    print("\n" + "="*80)
    print("ТЕСТ 4: Получение списка сессий")
    print("="*80)
    
    try:
        factory = AgentFactory()
        session_id = str(uuid.uuid4())
        
        archivist = factory.create_agent(
            profile_type='memory_archivist',
            session_id=session_id,
            task="Получить список сессий"
        )
        
        task = "Покажи список всех активных сессий в системе памяти."
        
        print(f"📋 Задача: {task}")
        result = archivist.run(task)
        
        print(f"\n✅ Список получен:")
        print(f"{str(result)[:500]}...")
        
        return True
    except Exception as e:
        print(f"❌ Ошибка при получении списка: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Запускает все тесты"""
    print("\n" + "="*80)
    print("ТЕСТИРОВАНИЕ АГЕНТА-АРХИВАРИУСА ПАМЯТИ")
    print("="*80)
    
    tests = [
        ("Создание агента", test_archivist_creation),
        ("Получение статистики", test_archivist_stats),
        ("Сохранение глобальной информации", test_archivist_save_global),
        ("Получение списка сессий", test_archivist_list_sessions),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            success = test_func()
            results.append((name, success))
        except Exception as e:
            print(f"\n❌ Критическая ошибка в тесте '{name}': {e}")
            results.append((name, False))
    
    # Итоги
    print("\n" + "="*80)
    print("ИТОГИ ТЕСТИРОВАНИЯ")
    print("="*80)
    
    passed = sum(1 for _, success in results if success)
    total = len(results)
    
    for name, success in results:
        status = "✅ PASSED" if success else "❌ FAILED"
        print(f"{status}: {name}")
    
    print(f"\nВсего тестов: {total}")
    print(f"Пройдено: {passed}")
    print(f"Провалено: {total - passed}")
    
    if passed == total:
        print("\n🎉 ВСЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО!")
    else:
        print(f"\n⚠️  {total - passed} тест(ов) провалено")
    
    print("="*80)


if __name__ == "__main__":
    main()


