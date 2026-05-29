#!/usr/bin/env python3
"""
Тестовый скрипт для проверки workflow
"""
import asyncio
import os
from workflow.engine import WorkflowEngine
from workflow.models import WorkflowDefinition, WorkflowContext

async def test_workflow():
    """Тестирование workflow с передачей контекста"""
    
    # Загружаем workflow
    workflow_def = WorkflowDefinition.from_yaml("workflow_pipelines/text_to_sql_pipeline.yaml")
    os.environ["DB_DSN"] = "duckdb:/Users/kosoj/Documents/MultiAgent/data/sber_index_prod.db"
    
    # Входные параметры
    test_query = "Какие муниципалитеты имеют наибольший процент населения с доходами ниже прожиточного минимума?"
    workflow_inputs = {
        'query': test_query,
        'dsn': os.environ["DB_DSN"],
        'max_rows': 10
    }
    workflow_def.inputs = workflow_inputs
    
    # Создаем контекст с переменными
    workflow_context = WorkflowContext(
        workflow_id="test_workflow",
        client_id='test_user',
        variables=workflow_inputs.copy()  # ← передаем переменные!
    )
    
    print("📋 Входные параметры:")
    for key, value in workflow_inputs.items():
        print(f"  {key}: {value if isinstance(value, str) and len(value) > 50 else value}")
    
    print("\n🚀 Запускаем workflow...")
    
    # Создаем engine и выполняем
    engine = WorkflowEngine()
    result = await engine.execute_workflow(workflow_def, context=workflow_context)
    
    print(f"\n✅ Статус: {result.status}")
    print(f"⏱️  Время выполнения: {result.duration_seconds:.2f}s")
    
    if result.step_results:
        print("\n📊 Результаты шагов:")
        for step_id, step_result in result.step_results.items():
            if step_result:
                status_emoji = "✅" if step_result.status == "completed" else "⚠️"
                print(f"  {status_emoji} {step_id}: {step_result.status} ({step_result.duration_seconds:.2f}s)")
    
    # Выводим финальный результат
    if 'sql_pipeline' in result.step_results:
        sql_result = result.step_results['sql_pipeline']
        if sql_result and sql_result.output:
            print("\n📄 Итоговый отчет менеджера:")
            print("=" * 80)
            print(sql_result.output)  # Первые 500 символов
            print("=" * 80)
    
    return result

if __name__ == "__main__":
    print("=" * 80)
    print("🧪 ТЕСТИРОВАНИЕ WORKFLOW С КОНТЕКСТОМ")
    print("=" * 80)
    
    result = asyncio.run(test_workflow())
    
    print("\n✅ Тест завершен!")

