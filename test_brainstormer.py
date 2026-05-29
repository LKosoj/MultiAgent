"""
Тестовый скрипт для проверки работы агента brainstormer

Использует единый подход к логированию через setup_comprehensive_logging
из logging_setup.py для согласованности с остальными компонентами проекта.
"""

import os
import sys
import uuid
from datetime import datetime
import logging

from custom_tools.brainstorm_tool import brainstorm
# Добавляем путь к проекту
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_factory import AgentFactory
from logging_setup import setup_comprehensive_logging

# Настройка единого логирования для всего проекта
# Уровень можно изменить: logging.DEBUG для детальных логов, logging.WARNING для краткости
setup_comprehensive_logging(log_level=logging.INFO, log_to_file=False)
logger = logging.getLogger(__name__)


def test_brainstormer_simple():
    """Простой тест: генерация идей для стартапа"""
    print("\n" + "="*80)
    print("ТЕСТ 1: Генерация идей для стартапа (creative методологии)")
    print("="*80 + "\n")
    
    session_id = f"brainstorm_test_{uuid.uuid4().hex[:8]}"
    
    factory = AgentFactory()
    
    task = """
Помоги придумать инновационные идеи для EdTech стартапа, 
который поможет студентам лучше усваивать сложный материал. 
"""
    
    try:
        agent = factory.create_agent(
            profile_type='brainstormer',
            session_id=session_id,
            task=task,
            pipeline_type='general'
        )
        
        print(f"✅ Агент создан: {agent.name}")
        
        # Безопасный вывод инструментов
        tool_names = []
        for t in agent.tools:
            if hasattr(t, 'name'):
                tool_names.append(t.name)
            elif hasattr(t, '__name__'):
                tool_names.append(t.__name__)
            else:
                tool_names.append(str(type(t).__name__))
        
        print(f"🔧 Инструменты ({len(tool_names)}): {', '.join(tool_names)}")
        print(f"📋 Задача: {task.strip()[:250]}...\n")
        
        print("🚀 Запуск мозгового штурма...\n")
        result = agent.run(task)
        
        print("\n" + "="*80)
        print("РЕЗУЛЬТАТ ТЕСТА 1:")
        print("="*80)
        print(result)
        print("\n")
        
        return True
        
    except Exception as e:
        print(f"\n❌ ОШИБКА В ТЕСТЕ 1: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_brainstormer_problem_solving():
    """Тест решения проблемы"""
    print("\n" + "="*80)
    print("ТЕСТ 2: Решение проблемы снижения вовлеченности (problem_solving)")
    print("="*80 + "\n")
    
    session_id = f"brainstorm_test_{uuid.uuid4().hex[:8]}"
    
    factory = AgentFactory()
    
    task = """
У нас есть мобильное приложение для изучения языков. 
Проблема: после первой недели использования 70% пользователей перестают заходить в приложение.
Найди решения этой проблемы используя методологии problem_solving.
"""
    
    try:
        agent = factory.create_agent(
            profile_type='brainstormer',
            session_id=session_id,
            task=task,
            pipeline_type='general'
        )
        
        print(f"✅ Агент создан: {agent.name}")
        print(f"📋 Задача: {task[:100]}...\n")
        
        print("🚀 Запуск мозгового штурма...\n")
        result = agent.run(task)
        
        print("\n" + "="*80)
        print("РЕЗУЛЬТАТ ТЕСТА 2:")
        print("="*80)
        print(result)
        print("\n")
        
        return True
        
    except Exception as e:
        print(f"\n❌ ОШИБКА В ТЕСТЕ 2: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_brainstormer_analytical():
    """Тест аналитического подхода"""
    print("\n" + "="*80)
    print("ТЕСТ 3: Анализ внедрения ИИ в компанию (analytical)")
    print("="*80 + "\n")
    
    session_id = f"brainstorm_test_{uuid.uuid4().hex[:8]}"
    
    factory = AgentFactory()
    
    task = """
Компания рассматривает внедрение ИИ-ассистентов для поддержки клиентов.
Проведи всесторонний анализ этого решения используя аналитические методологии.
Нужно оценить все аспекты: технические, финансовые, риски, возможности.
"""
    
    try:
        agent = factory.create_agent(
            profile_type='brainstormer',
            session_id=session_id,
            task=task,
            pipeline_type='general'
        )
        
        print(f"✅ Агент создан: {agent.name}")
        print(f"📋 Задача: {task[:100]}...\n")
        
        print("🚀 Запуск мозгового штурма...\n")
        result = agent.run(task)
        
        print("\n" + "="*80)
        print("РЕЗУЛЬТАТ ТЕСТА 3:")
        print("="*80)
        print(result)
        print("\n")
        
        return True
        
    except Exception as e:
        print(f"\n❌ ОШИБКА В ТЕСТЕ 3: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_brainstormer_all_methods():
    """Тест всех методологий"""
    print("\n" + "="*80)
    print("ТЕСТ 4: Все методологии - стратегия развития продукта")
    print("="*80 + "\n")
    
    session_id = f"brainstorm_test_{uuid.uuid4().hex[:8]}"
    
    factory = AgentFactory()
    
    task = """
Нужно придумать идею для нового продукта с использованием ИИ, чтобы этот продукт был полезен для пользователей, просто реализовать и приносил прибыль.
"""
    
    try:
        agent = factory.create_agent(
            profile_type='brainstormer',
            session_id=session_id,
            task=task,
            pipeline_type='general'
        )
        
        print(f"✅ Агент создан: {agent.name}")
        print(f"📋 Задача: {task[:100]}...\n")
        
        print("🚀 Запуск мозгового штурма со ВСЕМИ методологиями...\n")
        print("⏱️  Это может занять несколько минут...\n")
        
        result = agent.run(task)
        
        print("\n" + "="*80)
        print("РЕЗУЛЬТАТ ТЕСТА 4:")
        print("="*80)
        print(result)
        print("\n")
        
        return True
        
    except Exception as e:
        print(f"\n❌ ОШИБКА В ТЕСТЕ 4: {e}")
        import traceback
        traceback.print_exc()
        return False


def run_all_tests():
    """Запуск всех тестов"""
    print("\n" + "🧠"*40)
    print("НАЧАЛО ТЕСТИРОВАНИЯ АГЕНТА BRAINSTORMER")
    print("🧠"*40 + "\n")
    
    results = []

    session_id = f"brainstorm_test_{uuid.uuid4().hex[:8]}"

    task = """
Нужна стратегия развития нашего SaaS-продукта для управления проектами.
Используй все доступные методологии мозгового штурма для создания комплексной стратегии.
"""
    #results.append(("Тест 0 (Brainstorm)", brainstorm(topic=task, methods="all", session_id=session_id, parallel=True)))
            
    # Тест 1: Creative
    #results.append(("Тест 1 (Creative)", test_brainstormer_simple()))
    
    # Тест 2: Problem Solving
    #results.append(("Тест 2 (Problem Solving)", test_brainstormer_problem_solving()))
    
    # Тест 3: Analytical
    #results.append(("Тест 3 (Analytical)", test_brainstormer_analytical()))
    
    # Тест 4: All Methods (самый долгий, можно закомментировать)
    results.append(("Тест 4 (All Methods)", test_brainstormer_all_methods()))
    
    # Итоги
    print("\n" + "="*80)
    print("ИТОГИ ТЕСТИРОВАНИЯ:")
    print("="*80)
    for test_name, success in results:
        status = "✅ УСПЕШНО" if success else "❌ ОШИБКА"
        print(f"{status}: {test_name}", {success})
    
    total = len(results)
    passed = sum(1 for _, success in results if success)
    print(f"\nВсего тестов: {total}")
    print(f"Успешных: {passed}")
    print(f"Неудачных: {total - passed}")
    
    if passed == total:
        print("\n🎉 ВСЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО!")
    else:
        print(f"\n⚠️  {total - passed} тестов завершились с ошибкой")
    
    print("\n" + "🧠"*40 + "\n")


if __name__ == "__main__":
    # Запуск одного конкретного теста или всех
    import sys
    
    if len(sys.argv) > 1:
        test_num = sys.argv[1]
        if test_num == "1":
            test_brainstormer_simple()
        elif test_num == "2":
            test_brainstormer_problem_solving()
        elif test_num == "3":
            test_brainstormer_analytical()
        elif test_num == "4":
            test_brainstormer_all_methods()
        else:
            print(f"Неизвестный номер теста: {test_num}")
            print("Используйте: python test_brainstormer.py [1|2|3|4]")
    else:
        # Запуск всех тестов
        run_all_tests()

