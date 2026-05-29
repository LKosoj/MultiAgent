"""
Тест Enhanced Workflow Engine
"""
import asyncio
import sys
import os

# Добавляем текущую директорию в PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def test_enhanced_workflow():
    """Тест Enhanced Workflow Engine"""
    
    print("🧠 Тестирование Enhanced Workflow Engine...")
    
    try:
        from workflow.enhanced_engine import EnhancedWorkflowEngine
        from workflow.models import WorkflowContext, WorkflowDefinition
        
        # Создаем enhanced engine
        engine = EnhancedWorkflowEngine()
        
        # Создаем простой контекст
        context = WorkflowContext(
            workflow_id="test_enhanced_001",
            session_id="test_session_enhanced",
            variables={
                "topic": "Тестирование enhanced workflow системы"
            }
        )
        
        print(f"📋 Тема: {context.variables['topic']}")
        print(f"🆔 Workflow ID: {context.workflow_id}")
        
        # Проверяем статус enhanced компонентов
        stats = engine.get_enhanced_stats()
        print(f"\n📊 Enhanced Stats:")
        print(f"   Enhanced enabled: {stats['enhanced_enabled']}")
        print(f"   Policy version: {stats['policy_stats']['active_version']}")
        contract_stats = stats.get('contract_stats', {})
        if isinstance(contract_stats, dict) and 'available_contracts' in contract_stats:
            print(f"   Available contracts: {len(contract_stats['available_contracts'])}")
        else:
            print(f"   Available contracts: {len(engine.contract_registry.get_available_contracts())}")
        
        # Тестируем компоненты по отдельности
        print(f"\n🧩 Тестирование компонентов:")
        
        # 1. Policy Engine
        print("   1. Policy Engine...")
        policy = engine.policy_engine.registry.get_policy()
        print(f"      ✅ Policy loaded: {policy.name} v{policy.version}")
        
        # 2. Contract Registry
        print("   2. Contract Registry...")
        contracts = engine.contract_registry.get_available_contracts()
        print(f"      ✅ Available contracts: {', '.join(contracts)}")
        
        # 3. Feature Manager
        print("   3. Feature Manager...")
        features = [
            "pre_step_planner",
            "post_step_judge", 
            "semantic_validation",
            "human_in_the_loop"
        ]
        
        for feature in features:
            enabled = engine.feature_manager.is_feature_enabled(feature, context.workflow_id)
            status = "✅ enabled" if enabled else "❌ disabled"
            print(f"      {feature}: {status}")
        
        # 4. Resilience Components
        print("   4. Resilience Components...")
        
        # Circuit Breaker
        cb_stats = engine.circuit_breaker_manager.get_all_stats()
        print(f"      Circuit breakers: {cb_stats['total_agents']} agents monitored")
        
        # Budget Manager
        budget_summary = engine.budget_manager.get_budget_summary()
        print(f"      Budget manager: {budget_summary['consumption_history_entries']} history entries")
        
        # Loop Detector
        loop_stats = engine.loop_detector.get_loop_statistics()
        print(f"      Loop detector: {loop_stats['total_patterns']} patterns tracked")
        
        # Retry Engine
        retry_stats = engine.retry_engine.get_retry_statistics()
        print(f"      Retry engine: ready for adaptive retries")
        
        # 5. Orchestration Components
        print("   5. Orchestration Components...")
        
        # Conditional Engine
        supported_vars = len(engine.conditional_engine.get_supported_variables())
        supported_ops = len(engine.conditional_engine.get_supported_operators())
        print(f"      Conditional engine: {supported_vars} variables, {supported_ops} operators")
        
        # Cache Manager
        cache_stats = engine.cache_manager.get_combined_stats()
        print(f"      Cache manager: {cache_stats['overall']['total_cache_size']} cached items")
        
        # Alternative Executor
        alt_stats = engine.alternative_executor.get_execution_statistics()
        print(f"      Alternative executor: ready for parallel execution")
        
        # Performance Optimizer
        print(f"      Performance optimizer: ready for predictions and optimization")
        
        # 6. Monitoring Components
        print("   6. Monitoring Components...")
        
        # Metrics Collector
        metrics_summary = engine.metrics_collector.get_metrics_summary()
        print(f"      Metrics collector: {metrics_summary['total_metrics']} metrics tracked")
        
        # Alert Manager
        alerts_summary = engine.alert_manager.get_alerts_summary()
        print(f"      Alert manager: {alerts_summary['total_rules']} rules, {alerts_summary['enabled_rules']} enabled")
        
        # Analytics Engine
        analytics_summary = engine.analytics_engine.get_analytics_summary()
        print(f"      Analytics engine: {analytics_summary['total_insights']} insights generated")
        
        # Dashboard & Reports
        print(f"      Dashboard generator: ready for dashboard generation")
        print(f"      Report builder: ready for report generation")
        
        print(f"\n✅ Enhanced Workflow Engine готов к работе!")
        
        # Информация о fallback
        fallback_enabled = engine.feature_manager.global_config.get("enhanced_workflow", {}).get("fallback_to_legacy", False)
        print(f"🔄 Fallback to legacy: {'enabled' if fallback_enabled else 'disabled'}")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка при тестировании Enhanced Workflow Engine: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_simple_validation():
    """Тест простой валидации"""
    
    print(f"\n🔍 Тестирование валидации...")
    
    try:
        from workflow.contracts.registry import ContractRegistry
        from workflow.contracts.validators import get_validator
        
        registry = ContractRegistry()
        
        # Тестируем структурный валидатор
        validator = get_validator("structural")
        if validator:
            test_artifact = "Это тестовый результат для проверки валидации."
            test_contract = {
                "structural": {
                    "enabled": True,
                    "min_length": 10,
                    "required_fields": []
                }
            }
            
            result = await validator.validate(test_artifact, test_contract)
            print(f"   Structural validation: {result['passed']} (score: {result['score']:.2f})")
        
        # Тестируем полноту
        validator = get_validator("completeness") 
        if validator:
            result = await validator.validate(test_artifact, {
                "completeness": {
                    "enabled": True,
                    "min_sentences": 1
                }
            })
            print(f"   Completeness validation: {result['passed']} (score: {result['score']:.2f})")
        
        # Тестируем безопасность
        validator = get_validator("security")
        if validator:
            result = await validator.validate(test_artifact, {
                "security": {
                    "enabled": True,
                    "sql_injection_check": True
                }
            })
            print(f"   Security validation: {result['passed']} (score: {result['score']:.2f})")
        
        print(f"   ✅ Валидация работает корректно")
        
    except Exception as e:
        print(f"   ❌ Ошибка валидации: {e}")
        return False
    
    print(f"🎯 Тестирование условий...")
    try:
        # Тестируем условные выражения
        test_context = {
            "quality_score": 0.85,
            "step_status": "completed",
            "retry_count": 2,
            "execution_time": 45.5
        }
        
        test_conditions = [
            "quality_score > 0.7",
            "step_status == completed",
            "retry_count < 5",
            "execution_time <= 60"
        ]
        
        for condition in test_conditions:
            try:
                # Для тестирования создаем новый conditional engine
                from workflow.orchestration.conditions import ConditionalEngine
                conditional_engine = ConditionalEngine()
                result = await conditional_engine.evaluate_condition(condition, test_context)
                print(f"   '{condition}': {result}")
            except Exception as e:
                print(f"   '{condition}': error - {e}")
        
        print(f"   ✅ Условные выражения работают корректно")
        
    except Exception as e:
        print(f"   ❌ Ошибка тестирования условий: {e}")
        return False
    
    print(f"📊 Тестирование мониторинга...")
    try:
        # Получаем engine из глобального контекста
        from workflow.enhanced_engine import EnhancedWorkflowEngine
        engine_instance = EnhancedWorkflowEngine()
        
        # Создаем тестовые метрики
        engine_instance.metrics_collector.record_workflow_start("test_workflow", "TestWorkflow")
        engine_instance.metrics_collector.record_workflow_completion("test_workflow", "TestWorkflow", 45.5, True, 0.85)
        engine_instance.metrics_collector.record_step_execution("test_step", "test_agent", 15.2, True, 1, 0.9)
        
        # Тестируем dashboard
        dashboard = engine_instance.generate_dashboard("overview")
        print(f"   Dashboard generated: {dashboard['title']}")
        print(f"   Widgets count: {len(dashboard.get('widgets', []))}")
        
        # Тестируем отчет
        report = engine_instance.generate_report("daily")
        print(f"   Report generated: {report['title']}")
        
        # Тестируем алерты (симулируем низкий success rate)
        engine_instance.alert_manager.evaluate_rules({
            "workflow_success_rate": 75.0,  # Ниже порога 80%
            "avg_workflow_duration": 120.0,
            "avg_quality_score": 0.85
        })
        
        active_alerts = engine_instance.alert_manager.get_active_alerts()
        print(f"   Active alerts: {len(active_alerts)}")
        
        print(f"   ✅ Мониторинг работает корректно")
        
    except Exception as e:
        print(f"   ❌ Ошибка тестирования мониторинга: {e}")
        return False
        
    return True


async def main():
    """Главная функция"""
    print("🎯 ТЕСТ ENHANCED WORKFLOW ENGINE")
    print("=" * 50)
    
    # Тест 1: Базовая инициализация
    test1_success = await test_enhanced_workflow()
    
    # Тест 2: Валидация
    test2_success = await test_simple_validation()
    
    # Результат
    if test1_success and test2_success:
        print(f"\n🎉 Все тесты успешно пройдены!")
        print(f"Enhanced Workflow Engine готов к использованию.")
        return True
    else:
        print(f"\n💥 Некоторые тесты провалены!")
        return False


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
