import asyncio
import logging
import os
import sys
import traceback
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from agent_system import DynamicAgentSystem
from logging_setup import setup_comprehensive_logging

def auto_optimize_prompts():
    """Автоматическая оптимизация промптов при старте системы"""
    try:
        from prompt_optimizer.prompt_optimizer import PromptOptimizer
        
        print("🔧 Проверка актуальности промптов агентов...")
        optimizer = PromptOptimizer()
        results = optimizer.optimize_all_agents()
        
        if results['optimized_successfully'] > 0:
            print(f"✅ Оптимизировано агентов: {results['optimized_successfully']}")
        
        if results.get('skipped_already_optimized', 0) > 0:
            print(f"ℹ️  Уже актуальных агентов: {results['skipped_already_optimized']}")
            
        if results['failed_optimizations'] > 0:
            print(f"⚠️  Ошибок оптимизации: {results['failed_optimizations']}")
            
        print("🚀 Система готова к работе!\n")
        
    except Exception as e:
        print(f"⚠️  Ошибка автооптимизации: {e}")
        print("🚀 Система запускается без оптимизации...\n")

def get_execution_mode() -> tuple[str, Optional[str]]:
    """
    Определить режим выполнения на основе переменных окружения
    
    Returns:
        tuple: (mode, workflow_template)
        mode: 'workflow' или 'dynamic_agents'
        workflow_template: имя шаблона workflow или None
    """
    # Проверяем переменную окружения EXECUTION_MODE
    execution_mode = os.getenv('EXECUTION_MODE', '').lower()
    
    # Проверяем переменную окружения WORKFLOW_TEMPLATE
    workflow_template = os.getenv('WORKFLOW_TEMPLATE', '').strip()
    
    # Если указан шаблон workflow - используем workflow режим
    if workflow_template:
        # Проверяем существование файла
        workflow_path = Path(f"workflow_pipelines/{workflow_template}.yaml")
        if workflow_path.exists():
            print(f"🔧 Найден шаблон workflow: {workflow_template}")
            return 'workflow', workflow_template
        else:
            print(f"⚠️  Шаблон workflow '{workflow_template}' не найден в workflow_pipelines/")
            print(f"📁 Доступные шаблоны:")
            pipelines_dir = Path("workflow_pipelines")
            if pipelines_dir.exists():
                for yaml_file in pipelines_dir.glob("*.yaml"):
                    print(f"   - {yaml_file.stem}")
            print("🔄 Переключаемся на динамических агентов...")
            return 'dynamic_agents', None
    
    # Если режим явно указан как workflow, но шаблон не задан
    if execution_mode == 'workflow':
        print("⚠️  Режим WORKFLOW указан, но WORKFLOW_TEMPLATE не задан")
        print("🔄 Переключаемся на динамических агентов...")
        return 'dynamic_agents', None
    
    # По умолчанию используем динамических агентов
    return 'dynamic_agents', None

async def run_dynamic_agents_mode(task: str = None):
    """Запуск в режиме динамических агентов"""
    print("🤖 Режим: Динамические агенты")
    print("=" * 50)
    
    system = DynamicAgentSystem()
    
    # Используем задачу из переменной окружения или дефолтную
    if not task:
        print("\n📝 Введите задачу для выполнения:")
        task = input("> ").strip()
        if not task:
            task = "Исследовать современные тренды в IT индустрии"
            print(f"🔄 Используется дефолтная задача: {task}")
    
    print(f"📋 Задача: {task}")
    print()
    start_time = time.time()
    content = await system.coordinate(task, session_id=None, show=True)
    end_time = time.time()
    duration = end_time - start_time
    
    print("\n" + "=" * 50)
    print("🎯 РЕЗУЛЬТАТ:")
    print("=" * 50)
    print(content)
    print("=" * 50)
    print(f"⏱️  Время выполнения: {duration:.1f}s")


async def run_workflow_mode(workflow_template: str, task: str = None):
    """Запуск в режиме workflow"""
    print("⚙️  Режим: Workflow Engine")
    print(f"📄 Шаблон: {workflow_template}")
    print("=" * 50)
    
    try:
        # Определяем какой движок использовать
        use_enhanced = os.getenv('USE_ENHANCED_ENGINE', 'true').lower() == 'true'
        
        if use_enhanced:
            from workflow.enhanced_engine import EnhancedWorkflowEngine
            engine = EnhancedWorkflowEngine()
            print("🚀 Используется Enhanced Workflow Engine")
        else:
            from workflow.engine import WorkflowEngine  
            engine = WorkflowEngine()
            print("🔧 Используется базовый Workflow Engine")
        
        # Загружаем workflow из файла
        from workflow.models import WorkflowDefinition
        workflow_path = Path(f"workflow_pipelines/{workflow_template}.yaml")
        
        print(f"📁 Загрузка workflow: {workflow_path}")
        workflow_def = WorkflowDefinition.from_yaml(workflow_path)
        
        # Получаем задачу из переменной окружения или используем интерактивный ввод
        if not task:
            task = os.getenv('TASK')
            
            if not task:
                print("\n📝 Введите задачу для выполнения:")
                task = input("> ").strip()
                if not task:
                    task = "Исследовать современные тренды в IT индустрии"
                    print(f"🔄 Используется дефолтная задача: {task}")
        
        print(f"📋 Задача: {task}")
        print()
        
        # Создаем контекст с переменными
        from workflow.models import WorkflowContext
        context = WorkflowContext(
            workflow_id=f"{workflow_template}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            session_id=f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            variables={"topic": task, "task": task}
        )
        
        # Выполняем workflow
        print("🚀 Запуск workflow...")
        result = await engine.execute_workflow(workflow_def, context)
        
        print("\n" + "=" * 50)
        print("🎯 РЕЗУЛЬТАТ WORKFLOW:")
        print("=" * 50)
        print(f"📊 Статус: {result.status.value}")
        print(f"⏱️  Время выполнения: {result.duration_seconds:.1f}s")
        
        if result.status.name == 'COMPLETED':
            print(f"✅ Workflow завершен успешно")
            if hasattr(result, 'step_results') and result.step_results:
                print("\n📋 Результаты шагов:")
                for step_id, step_result in result.step_results.items():
                    status_emoji = "✅" if step_result.status.name == 'COMPLETED' else "❌"
                    print(f"  {status_emoji} {step_id}: {step_result.status.value}")
                    if step_result.output and len(str(step_result.output)) < 200:
                        print(f"     {step_result.output}")
            
            # Показываем финальный результат
            if hasattr(result, 'final_output') and result.final_output:
                print(f"\n🎉 Финальный результат:")
                print(result.final_output)
        else:
            print(f"❌ Workflow завершен с ошибкой: {result.error}")
            
        # Показываем enhanced статистику если доступна
        if use_enhanced and hasattr(engine, 'get_enhanced_stats'):
            stats = engine.get_enhanced_stats()
            print(f"\n📊 Enhanced статистика:")
            if 'metrics_summary' in stats:
                metrics = stats['metrics_summary']['aggregated_metrics']
                print(f"   Success rate: {metrics.get('workflow_success_rate', 0):.1f}%")
                print(f"   Avg duration: {metrics.get('avg_workflow_duration', 0):.1f}s")
                print(f"   Cache hit rate: {metrics.get('cache_hit_rate', 0):.1f}%")
                
        print("=" * 50)
        
    except Exception as e:
        print(f"❌ Ошибка выполнения workflow: {e}")
        print(f"📋 Traceback: {traceback.format_exc()}")
        print("🔄 Переключаемся на динамических агентов...")
        await run_dynamic_agents_mode(task)

def print_usage_info():
    """Показать информацию об использовании"""
    print("🔧 НАСТРОЙКА РЕЖИМА ВЫПОЛНЕНИЯ")
    print("=" * 50)
    print("Переменные окружения:")
    print("  WORKFLOW_TEMPLATE  - имя шаблона workflow (без .yaml)")
    print("  EXECUTION_MODE     - 'workflow' или 'dynamic_agents'")
    print("  TASK              - задача для выполнения")
    print("  USE_ENHANCED_ENGINE - 'true' для Enhanced Engine (по умолчанию)")
    print()
    print("Примеры запуска:")
    print("  # Динамические агенты (по умолчанию)")
    print("  python main.py")
    print()
    print("  # Workflow с шаблоном")
    print("  WORKFLOW_TEMPLATE=simple_research python main.py")
    print()
    print("  # С кастомной задачей")
    print("  TASK='Анализ рынка ИИ' WORKFLOW_TEMPLATE=data_analysis python main.py")
    print()
    print("  # Базовый Workflow Engine")
    print("  USE_ENHANCED_ENGINE=false WORKFLOW_TEMPLATE=simple_research python main.py")
    print()
    print("📁 Доступные шаблоны workflow:")
    pipelines_dir = Path("workflow_pipelines")
    if pipelines_dir.exists():
        for yaml_file in pipelines_dir.glob("*.yaml"):
            print(f"   - {yaml_file.stem}")
    print("=" * 50)

async def main():
    """Главная функция с поддержкой выбора режима выполнения"""
    
    # Настройка логирования
    setup_comprehensive_logging(log_level=logging.WARNING)
    
    # Проверяем аргументы командной строки
    if len(sys.argv) > 1 and sys.argv[1] in ['--help', '-h']:
        print_usage_info()
        return
    
    # Автооптимизация промптов при старте
    auto_optimize_prompts()
    
    # Определяем режим выполнения
    mode, workflow_template = get_execution_mode()
    
    # Получаем задачу из аргументов командной строки если передана
    task = None
    if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
        task = ' '.join(sys.argv[1:])
    else:
        task = "Нарисуй диаграмму, которая отображает процесс создания системы ИИ агентов с помощью кодогенерации."
   
    print("🤖 MULTIAGENT SYSTEM")
    print("=" * 50)
    print(f"🔧 Режим выполнения: {mode}")
    if workflow_template:
        print(f"📄 Шаблон workflow: {workflow_template}")
    print()
    
    # Запускаем в соответствующем режиме
    try:
        if mode == 'workflow' and workflow_template:
            await run_workflow_mode(workflow_template, task)
        else:
            await run_dynamic_agents_mode(task)
    except KeyboardInterrupt:
        print("\n\n⏹️  Выполнение прервано пользователем")
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        print(f"📋 Traceback: {traceback.format_exc()}")

if __name__ == "__main__":
    asyncio.run(main())
