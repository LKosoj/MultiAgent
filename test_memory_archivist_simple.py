"""
Простой тест агента-архивариуса памяти
======================================
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_factory import AgentFactory
import uuid


def test_stats():
    """Простой тест получения статистики"""
    print("\n" + "="*80)
    print("ТЕСТ: Получение статистики памяти через агента-архивариуса")
    print("="*80)
    
    try:
        factory = AgentFactory()
        session_id = str(uuid.uuid4())
        
        # Простая задача
        task = "Найди всю информацию о dbt"

        archivist = factory.create_agent(
            profile_type='memory_archivist',
            session_id=session_id,
            task=task
        )
        
        print(f"✅ Агент создан: {archivist.name}")
        print(f"   Сессия: {session_id}")
        
        
        print(f"\n📋 Задача: {task}\n")
        result = archivist.run(task)
        
        print(f"\n✅ РЕЗУЛЬТАТ:")
        print("="*80)
        print(result)
        print("="*80)
        
        return True
        
    except Exception as e:
        print(f"\n❌ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_stats()
    
    if success:
        print("\n🎉 ТЕСТ ПРОЙДЕН!")
    else:
        print("\n⚠️ ТЕСТ ПРОВАЛЕН!")

