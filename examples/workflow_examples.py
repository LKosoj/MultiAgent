"""
Примеры использования Workflow Engine с YAML пайплайнами
======================================================

Демонстрирует различные сценарии применения workflow для автоматизации
сложных многоэтапных процессов с агентами ИИ используя YAML определения.
"""

import asyncio
import os
from pathlib import Path
from workflow import WorkflowEngine
from workflow.models import WorkflowContext, WorkflowDefinition


async def simple_research_example():
    """Пример: Простое исследование из YAML пайплайна"""
    
    print("🔍 Запуск простого исследовательского пайплайна...")
    
    engine = WorkflowEngine()
    
    try:
        # Выполняем готовый YAML пайплайн
        result = await engine.execute_pipeline_by_name(
            pipeline_name="simple_research",
            topic="Квантовые вычисления в 2024 году"
        )
        
        print(f"✅ Исследование завершено: {result.status}")
        print(f"📊 Обработано шагов: {result.completed_steps}/{result.total_steps}")
        print(f"⏱️ Время выполнения: {result.duration_seconds:.1f} сек")
        
        if result.final_output:
            print("📄 Результаты исследования:")
            print(f"   - Найдено источников: {len(result.final_output.get('sources', []))}")
            print(f"   - Ключевых выводов: {len(result.final_output.get('key_findings', []))}")
        
        return result
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return None


async def content_creation_example():
    """Пример: Создание образовательного контента"""
    
    print("📚 Запуск пайплайна создания образовательного контента...")
    
    engine = WorkflowEngine()
    
    # Создаем контекст с дополнительными параметрами
    context = WorkflowContext(
        workflow_id="content_creation_demo",
        session_id="session_123",
        client_id="educational_dept",
        variables={
            "topic": "Машинное обучение для начинающих",
            "target_level": "beginner",
            "course_duration": 4
        }
    )
    
    try:
        result = await engine.execute_workflow_from_yaml(
            yaml_path="workflow_pipelines/content_creation.yaml",
            context=context
        )
        
        print(f"✅ Контент создан: {result.status}")
        print(f"📊 Выполнено: {result.completed_steps}/{result.total_steps} шагов")
        print(f"⏱️ Время: {result.duration_seconds:.1f} сек")
        
        # Показываем созданные материалы
        if result.step_results:
            print("\n📋 Созданные материалы:")
            for step_id, step_result in result.step_results.items():
                if step_result.status.value == "completed":
                    print(f"   ✅ {step_id}: {step_result.agent_name}")
        
        return result
        
    except Exception as e:
        print(f"❌ Ошибка создания контента: {e}")
        return None


async def data_analysis_example():
    """Пример: Анализ данных с SQL генерацией"""
    
    print("📊 Запуск пайплайна анализа данных...")
    
    engine = WorkflowEngine()
    
    try:
        result = await engine.execute_pipeline_by_name(
            pipeline_name="data_analysis",
            analysis_request="Покажи топ-10 самых популярных продуктов по продажам за последний квартал"
        )
        
        print(f"✅ Анализ завершен: {result.status}")
        print(f"📊 Шагов выполнено: {result.completed_steps}/{result.total_steps}")
        
        # Показываем результаты анализа
        if result.final_output:
            output = result.final_output
            print("\n📈 Результаты анализа:")
            if 'sql_query' in output:
                print(f"   🔍 Сгенерированный SQL: {output['sql_query'][:100]}...")
            if 'insights' in output:
                print(f"   💡 Найдено инсайтов: {len(output['insights'])}")
            if 'visualizations' in output:
                print(f"   📊 Создано графиков: {len(output['visualizations'])}")
        
        return result
        
    except Exception as e:
        print(f"❌ Ошибка анализа: {e}")
        return None


async def architecture_review_example():
    """Пример: Архитектурный анализ проекта"""
    
    print("🏗️ Запуск пайплайна архитектурного анализа...")
    
    engine = WorkflowEngine()
    
    # Анализируем текущий проект
    project_path = os.getcwd()
    
    try:
        result = await engine.execute_pipeline_by_name(
            pipeline_name="architecture_review",
            project_path=project_path
        )
        
        print(f"✅ Анализ архитектуры завершен: {result.status}")
        print(f"📊 Проанализировано этапов: {result.completed_steps}/{result.total_steps}")
        
        # Показываем результаты анализа
        if result.final_output:
            output = result.final_output
            print("\n🏗️ Результаты архитектурного анализа:")
            if 'components_found' in output:
                print(f"   🧩 Найдено компонентов: {output['components_found']}")
            if 'architecture_issues' in output:
                print(f"   ⚠️ Выявлено проблем: {len(output['architecture_issues'])}")
            if 'diagrams_created' in output:
                print(f"   📈 Создано диаграмм: {len(output['diagrams_created'])}")
        
        return result
        
    except Exception as e:
        print(f"❌ Ошибка анализа архитектуры: {e}")
        return None


async def custom_yaml_workflow_example():
    """Пример: Создание и выполнение пользовательского YAML workflow"""
    
    print("🛠️ Создание пользовательского YAML workflow...")
    
    # Создаем временный YAML файл
    custom_yaml = """
name: "custom_demo_workflow"
version: "1.0"
description: "Демонстрационный пользовательский workflow"

global_retry_policy:
  max_retries: 1
  backoff_strategy: "fixed"
  base_delay: 2.0

steps:
  - id: "research_step"
    agent_type: "researcher"
    task: "Исследуй тему: {custom_topic}"
    timeout: 60
    metadata:
      priority: "high"

  - id: "summarize_step"
    agent_type: "analyst"
    task: "Создай краткое резюме исследования"
    depends_on: ["research_step"]
    timeout: 30

metadata:
  author: "Demo User"
  category: "demo"
  estimated_duration: "2-3 minutes"
"""
    
    # Сохраняем во временный файл
    temp_yaml_path = Path("temp_demo_workflow.yaml")
    with open(temp_yaml_path, 'w', encoding='utf-8') as f:
        f.write(custom_yaml)
    
    engine = WorkflowEngine()
    
    try:
        # Загружаем и валидируем
        workflow_def = await engine.load_and_validate_yaml(temp_yaml_path)
        print(f"✅ YAML загружен: {workflow_def.name}")
        
        # Выполняем
        result = await engine.execute_workflow_from_yaml(
            yaml_path=temp_yaml_path,
            custom_topic="Современные тренды в области ИИ"
        )
        
        print(f"✅ Пользовательский workflow завершен: {result.status}")
        print(f"📊 Выполнено: {result.completed_steps}/{result.total_steps} шагов")
        
        return result
        
    except Exception as e:
        print(f"❌ Ошибка пользовательского workflow: {e}")
        return None
    
    finally:
        # Удаляем временный файл
        if temp_yaml_path.exists():
            temp_yaml_path.unlink()
            print("🗑️ Временный YAML файл удален")


async def pipeline_management_example():
    """Пример: Управление пайплайнами"""
    
    print("📋 Демонстрация управления пайплайнами...")
    
    engine = WorkflowEngine()
    
    # Получаем список доступных пайплайнов
    pipelines = engine.list_available_pipelines()
    
    print(f"\n📁 Найдено пайплайнов: {len(pipelines)}")
    for pipeline in pipelines:
        print(f"   📄 {pipeline['name']} v{pipeline['version']}")
        print(f"      📝 {pipeline['description']}")
        print(f"      🔧 Шагов: {pipeline['steps_count']}")
        print(f"      ⏱️ Время: {pipeline['estimated_duration']}")
        print(f"      🤖 Агенты: {', '.join(pipeline['agents_used'])}")
        print()
    
    # Получаем детальную информацию о конкретном пайплайне
    if pipelines:
        first_pipeline = pipelines[0]['name']
        
        # Извлекаем имя файла без расширения
        pipeline_name = Path(first_pipeline).stem if first_pipeline.endswith('.yaml') else first_pipeline
        
        try:
            info = engine.get_pipeline_info(pipeline_name)
            
            print(f"🔍 Детальная информация о '{info['name']}':")
            print(f"   📂 Файл: {info['file_path']}")
            print(f"   ⚙️ Ресурсы: {info['resource_requirements']}")
            print(f"   🌐 Граф зависимостей: {info['dependency_graph']}")
            print(f"   🎯 Агенты: {', '.join(info['agents_used'])}")
            
        except Exception as e:
            print(f"⚠️ Не удалось получить информацию о пайплайне: {e}")


async def error_handling_example():
    """Пример: Обработка ошибок и восстановление"""
    
    print("🛡️ Демонстрация обработки ошибок...")
    
    engine = WorkflowEngine()
    
    # Создаем YAML с потенциальной ошибкой
    error_yaml = """
name: "error_demo_workflow"
version: "1.0"
description: "Демонстрация обработки ошибок"

global_retry_policy:
  max_retries: 2
  backoff_strategy: "exponential"
  base_delay: 1.0

steps:
  - id: "normal_step"
    agent_type: "researcher"
    task: "Выполни нормальную задачу: {task}"
    timeout: 30

  - id: "error_prone_step"
    agent_type: "nonexistent_agent"  # Несуществующий агент
    task: "Эта задача должна упасть"
    depends_on: ["normal_step"]
    timeout: 20
    retry_policy:
      max_retries: 1
      backoff_strategy: "fixed"
      base_delay: 2.0

  - id: "recovery_step"
    agent_type: "analyst"
    task: "Этот шаг должен выполниться несмотря на ошибку предыдущего"
    depends_on: ["normal_step"]  # Зависит от успешного шага
    timeout: 30
"""
    
    # Сохраняем во временный файл
    temp_yaml_path = Path("temp_error_demo.yaml")
    with open(temp_yaml_path, 'w', encoding='utf-8') as f:
        f.write(error_yaml)
    
    try:
        result = await engine.execute_workflow_from_yaml(
            yaml_path=temp_yaml_path,
            task="Простое исследование"
        )
        
        print(f"📊 Workflow завершен с результатом: {result.status}")
        print(f"✅ Успешно: {result.completed_steps} шагов")
        print(f"❌ Ошибок: {result.failed_steps} шагов")
        
        # Показываем детали по шагам
        print("\n📋 Детали выполнения:")
        for step_id, step_result in result.step_results.items():
            status_emoji = "✅" if step_result.status.value == "completed" else "❌"
            print(f"   {status_emoji} {step_id}: {step_result.status.value}")
            if step_result.error:
                print(f"      🚨 Ошибка: {step_result.error}")
        
        return result
        
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        return None
    
    finally:
        # Удаляем временный файл
        if temp_yaml_path.exists():
            temp_yaml_path.unlink()


async def workflow_state_management_example():
    """Пример: Управление состоянием workflow"""
    
    print("💾 Демонстрация управления состоянием workflow...")
    
    engine = WorkflowEngine()
    
    # Создаем workflow с checkpoint'ами
    checkpointed_yaml = """
name: "checkpointed_workflow"
version: "1.0"
description: "Workflow с checkpoint'ами для восстановления"

steps:
  - id: "step1"
    agent_type: "researcher"
    task: "Первый шаг: {task_description}"
    timeout: 30

  - id: "step2"
    agent_type: "analyst"
    task: "Второй шаг: анализ результатов"
    depends_on: ["step1"]
    timeout: 40

  - id: "step3"
    agent_type: "validator"
    task: "Третий шаг: валидация результатов"
    depends_on: ["step2"]
    timeout: 20

error_handling:
  save_checkpoint_interval: 30
  on_failure: "pause_and_notify"
"""
    
    temp_yaml_path = Path("temp_checkpointed.yaml")
    with open(temp_yaml_path, 'w', encoding='utf-8') as f:
        f.write(checkpointed_yaml)
    
    try:
        # Создаем контекст с определенным ID для отслеживания
        context = WorkflowContext(
            workflow_id="checkpoint_demo_001",
            session_id="checkpoint_session",
            variables={"task_description": "Сбор информации о новых технологиях"}
        )
        
        # Выполняем workflow
        result = await engine.execute_workflow_from_yaml(
            yaml_path=temp_yaml_path,
            context=context
        )
        
        print(f"✅ Workflow выполнен: {result.status}")
        
        # Получаем checkpoint'ы
        checkpoints = await engine.state_manager.get_checkpoints(context.workflow_id)
        
        print(f"\n💾 Сохранено checkpoint'ов: {len(checkpoints)}")
        for i, checkpoint in enumerate(checkpoints):
            print(f"   📍 Checkpoint {i+1}: {checkpoint.timestamp.strftime('%H:%M:%S')}")
            print(f"      🔄 Статус: {checkpoint.status.value}")
            print(f"      📝 Текущий шаг: {checkpoint.current_step}")
            print(f"      ✅ Завершено: {len(checkpoint.completed_steps)} шагов")
        
        return result
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return None
    
    finally:
        if temp_yaml_path.exists():
            temp_yaml_path.unlink()


async def main():
    """Главная функция с демонстрацией всех примеров"""
    
    print("=" * 60)
    print("🎯 ДЕМОНСТРАЦИЯ WORKFLOW ENGINE С YAML ПАЙПЛАЙНАМИ")
    print("=" * 60)
    
    examples = [
        ("🔍 Простое исследование", simple_research_example),
        ("📚 Создание контента", content_creation_example),
        ("📊 Анализ данных", data_analysis_example),
        ("🏗️ Архитектурный анализ", architecture_review_example),
        ("🛠️ Пользовательский workflow", custom_yaml_workflow_example),
        ("📋 Управление пайплайнами", pipeline_management_example),
        ("🛡️ Обработка ошибок", error_handling_example),
        ("💾 Управление состоянием", workflow_state_management_example),
    ]
    
    for name, func in examples:
        print(f"\n{'-' * 50}")
        print(f"▶️ {name}")
        print(f"{'-' * 50}")
        
        try:
            result = await func()
            if result:
                print(f"✅ {name} - выполнено успешно")
            else:
                print(f"⚠️ {name} - завершено с предупреждениями")
        except Exception as e:
            print(f"❌ {name} - ошибка: {e}")
        
        print("\n⏸️ Пауза 2 секунды...")
        await asyncio.sleep(2)
    
    print(f"\n{'=' * 60}")
    print("🎉 Демонстрация завершена!")
    print("=" * 60)


if __name__ == "__main__":
    # Устанавливаем корректную рабочую директорию
    script_dir = Path(__file__).parent.parent
    os.chdir(script_dir)
    
    # Запускаем демонстрацию
    asyncio.run(main())