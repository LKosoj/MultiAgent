"""
Тест запуска YAML пайплайна
"""

import asyncio
import sys
import os

# Добавляем текущую директорию в PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

async def test_simple_research_pipeline():
    """Тест запуска простого исследовательского пайплайна"""
    
    print("🔍 Запуск простого исследовательского пайплайна...")
    
    try:
        from workflow.engine import WorkflowEngine
        from workflow.models import WorkflowContext
        
        # Создаем workflow engine
        engine = WorkflowEngine()
        
        # Создаем контекст с переменными
        context = WorkflowContext(
            workflow_id="test_research_001",
            session_id="test_session_123",
            variables={
                "topic": "Последние достижения в области квантовых вычислений"
            }
        )
        
        print(f"📋 Тема исследования: {context.variables['topic']}")
        print(f"🆔 Workflow ID: {context.workflow_id}")
        
        # Запускаем пайплайн
        print("\n🚀 Запуск пайплайна...")
        result = await engine.execute_pipeline_by_name(
            pipeline_name="simple_research",
            context=context
        )
        
        # Выводим результаты
        print(f"\n✅ Пайплайн завершен!")
        print(f"📊 Статус: {result.status}")
        print(f"⏱️ Время выполнения: {result.duration_seconds:.1f} сек")
        print(f"📈 Выполнено шагов: {result.completed_steps}/{result.total_steps}")
        
        # Показываем результаты по шагам
        print(f"\n📋 Детали выполнения:")
        for step_id, step_result in result.step_results.items():
            status_emoji = "✅" if step_result.status.value == "completed" else "❌"
            print(f"   {status_emoji} {step_id}: {step_result.status.value}")
            if step_result.duration_seconds:
                print(f"      ⏱️ Время: {step_result.duration_seconds:.1f}s")
            if step_result.error:
                print(f"      🚨 Ошибка: {step_result.error}")
        
        # Финальный результат
        if result.final_output:
            print(f"\n📄 Итоговый результат:")
            if isinstance(result.final_output, dict):
                for key, value in result.final_output.items():
                    print(f"   📌 {key}: {str(value)[:100]}...")
            else:
                print(f"   📌 {str(result.final_output)[:200]}...")
        
        return result
        
    except Exception as e:
        print(f"❌ Ошибка при запуске пайплайна: {e}")
        import traceback
        traceback.print_exc()
        return None

async def main():
    """Главная функция"""
    print("🎯 ТЕСТ ЗАПУСКА YAML ПАЙПЛАЙНА")
    print("=" * 50)
    
    result = await test_simple_research_pipeline()
    
    if result:
        print("\n🎉 Тест успешно завершен!")
        return True
    else:
        print("\n💥 Тест провален!")
        return False

if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
