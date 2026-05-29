"""
Smoke-тесты для всех публичных API Streamlit
==========================================

Проверяют базовую работоспособность API без глубокого тестирования логики.
Цель: убедиться, что все API инициализируются и основные методы не падают.
"""

import pytest
import tempfile
import os
import sys
import json
import types
from pathlib import Path
from unittest.mock import Mock, patch
import logging

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

logger = logging.getLogger(__name__)


def _clear_workflow_cached_attrs():
    workflow_pkg = sys.modules.get("workflow")
    if workflow_pkg is None:
        return
    for attr in ("engine", "enhanced_engine", "WorkflowEngine", "EnhancedWorkflowEngine"):
        workflow_pkg.__dict__.pop(attr, None)


@pytest.fixture
def stub_mcp_tools(monkeypatch):
    module_names = (
        "agent_streamlit_api",
        "agent_factory",
        "agent_system",
        "workflow.engine",
        "workflow.enhanced_engine",
        "mcp_tools",
    )
    for module_name in module_names:
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    _clear_workflow_cached_attrs()
    monkeypatch.setitem(
        sys.modules,
        "mcp_tools",
        types.SimpleNamespace(mcp_clients=[], mcp_tools=[]),
    )
    yield
    for module_name in module_names:
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    _clear_workflow_cached_attrs()


class TestWorkflowStreamlitAPI:
    """Smoke-тесты для Workflow API"""
    
    def test_workflow_manager_init(self):
        """Тест инициализации WorkflowManager"""
        from workflow.streamlit_api import WorkflowManager
        
        manager = WorkflowManager(use_enhanced=False, pipelines_dir="workflow_pipelines")
        assert manager is not None
        assert manager._engine is None
        assert hasattr(manager, 'list_workflows')
        assert hasattr(manager, 'start_workflow')
        assert hasattr(manager, 'get_workflow_status')
        assert hasattr(manager, 'cancel_workflow')

    def test_list_workflows_empty_dir(self):
        """Тест получения списка пайплайнов из пустой директории"""
        from workflow.streamlit_api import WorkflowManager
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = WorkflowManager(pipelines_dir=tmp_dir)
            workflows = manager.list_workflows()
            assert isinstance(workflows, list)
            assert len(workflows) == 0

    def test_start_workflow_reports_yaml_load_errors(self, tmp_path):
        """При битом YAML API показывает причину загрузки, а не только not found."""
        from workflow.streamlit_api import WorkflowManager

        (tmp_path / "broken.yaml").write_text(
            "\n".join([
                "name: broken_workflow",
                "version: '1.0'",
                "pipeline:",
                "  requires_enhanced_engine: sometimes",
                "steps: []",
            ]),
            encoding="utf-8",
        )

        manager = WorkflowManager(use_enhanced=False, pipelines_dir=str(tmp_path))

        with pytest.raises(ValueError) as exc_info:
            manager.start_workflow("missing_workflow", use_enhanced=False)

        message = str(exc_info.value)
        assert "Ошибки загрузки YAML" in message
        assert "broken.yaml" in message
        assert "requires_enhanced_engine" in message

    def test_workflow_info_dataclass(self):
        """Тест структуры данных WorkflowInfo"""
        from workflow.streamlit_api import WorkflowInfo
        
        info = WorkflowInfo(
            file_path="/test/path.yaml",
            name="test_workflow",
            version="1.0",
            description="Test workflow"
        )
        assert info.file_path == "/test/path.yaml"
        assert info.name == "test_workflow"
        assert isinstance(info.agents_used, list)
        assert isinstance(info.parameters, dict)

    def test_workflow_status_dataclass(self):
        """Тест структуры данных WorkflowRunStatus"""
        from workflow.streamlit_api import WorkflowRunStatus
        
        status = WorkflowRunStatus(
            run_id="test-123",
            workflow_name="test",
            status="running"
        )
        assert status.run_id == "test-123"
        assert status.status == "running"
        assert isinstance(status.step_results, dict)

    def test_workflow_result_event_store_masks_pii(self, monkeypatch, tmp_path):
        """WORKFLOW_RESULT не должен сохранять raw PII/DSN в AG-UI EventStore."""
        from backend.fastapi_app.agui.store import EventStore
        import workflow.streamlit_api as streamlit_api

        monkeypatch.setattr(streamlit_api, "_project_root", lambda: tmp_path)
        run_id = "run-persisted-pii"
        raw_email = "person@example.com"
        raw_phone = "+7 (495) 123-45-67"
        raw_dsn = "postgresql://alice:secret@example.com/app?api_key=abc"

        streamlit_api._append_workflow_result_event(
            run_id,
            {"content": f"{raw_dsn} {raw_email} {raw_phone}"},
            "completed",
            artifacts={"dsn": raw_dsn},
            snapshot={"phone": raw_phone},
        )

        store = EventStore(str(tmp_path / "data" / "agui_events.db"))
        events = list(store.list_after(run_id, 0))
        assert len(events) == 1
        serialized = json.dumps(events[0].payload, ensure_ascii=False)
        assert "secret" not in serialized
        assert "api_key=abc" not in serialized
        assert raw_email not in serialized
        assert raw_phone not in serialized
        assert "***:***@example.com" in serialized
        assert "[EMAIL]" in serialized
        assert "[PHONE]" in serialized

    def test_streamlit_api_redacts_dsn_inside_query_field(self):
        import workflow.streamlit_api as streamlit_api

        raw = (
            "connect postgresql://alice:secret@db/app"
            "?api_key=raw-key&sslmode=require"
        )
        redacted = streamlit_api._redact_payload({"query": raw})

        assert "alice" not in redacted["query"]
        assert "secret" not in redacted["query"]
        assert "raw-key" not in redacted["query"]
        assert "***:***@db" in redacted["query"]
        assert "api_key=***" in redacted["query"]
        assert "sslmode=require" in redacted["query"]


class TestAgentStreamlitAPI:
    """Smoke-тесты для Agent API"""
    
    def test_agent_manager_init(self, stub_mcp_tools):
        """Тест инициализации AgentManager"""
        from agent_streamlit_api import AgentManager
        
        manager = AgentManager()
        assert manager is not None
        assert hasattr(manager, 'list_agents')
        assert hasattr(manager, 'create_agent')
        assert hasattr(manager, 'run_agent')

    def test_list_agents_basic(self, stub_mcp_tools):
        """Тест получения списка агентов"""
        from agent_streamlit_api import AgentManager
        
        manager = AgentManager()
        agents = manager.list_agents()
        assert isinstance(agents, list)
        # Должны быть некоторые базовые агенты из AGENT_PROFILES
        assert len(agents) >= 0

    def test_agent_profile_dataclass(self, stub_mcp_tools):
        """Тест структуры данных AgentProfile"""
        from agent_streamlit_api import AgentProfile
        
        profile = AgentProfile(
            name="test_agent",
            type="code",
            description="Test agent"
        )
        assert profile.name == "test_agent"
        assert profile.type == "code"
        assert isinstance(profile.tools, list)

    def test_dynamic_agent_definition(self, stub_mcp_tools):
        """Тест определения динамического агента"""
        from agent_streamlit_api import DynamicAgentDefinition
        
        definition = DynamicAgentDefinition(
            name="dynamic_test",
            description="Dynamic test agent"
        )
        assert definition.name == "dynamic_test"
        
        profile_dict = definition.to_profile_dict()
        assert isinstance(profile_dict, dict)
        assert "type" in profile_dict
        assert "tools" in profile_dict


class TestDBPluginsStreamlitAPI:
    """Smoke-тесты для DB Plugins API"""
    
    def test_db_plugin_manager_init(self):
        """Тест инициализации DBPluginManager"""
        from db_plugins.streamlit_api import get_db_plugin_manager
        
        manager = get_db_plugin_manager()
        assert manager is not None
        assert hasattr(manager, 'list_plugins')
        assert hasattr(manager, 'test_connection')
        assert hasattr(manager, 'validate_dsn')

    def test_list_plugins_basic(self):
        """Тест получения списка плагинов"""
        from db_plugins.streamlit_api import get_db_plugin_manager
        
        manager = get_db_plugin_manager()
        plugins = manager.list_plugins()
        assert isinstance(plugins, list)
        assert len(plugins) > 0  # Должны быть базовые плагины
        
        # Проверяем структуру первого плагина
        if plugins:
            plugin = plugins[0]
            assert hasattr(plugin, 'scheme')
            assert hasattr(plugin, 'dialect')
            assert hasattr(plugin, 'dialect_label')

    def test_dsn_validation_basic(self):
        """Тест базовой валидации DSN"""
        from db_plugins.streamlit_api import get_db_plugin_manager
        
        manager = get_db_plugin_manager()
        
        # Тест пустого DSN
        result = manager.validate_dsn("")
        assert not result.is_valid
        assert len(result.errors) > 0
        
        # Тест DSN без схемы
        result = manager.validate_dsn("sqlite:///test.db")
        assert isinstance(result.is_valid, bool)
        assert isinstance(result.errors, list)

    def test_plugin_info_dataclass(self):
        """Тест структуры данных PluginInfo"""
        from db_plugins.streamlit_api import PluginInfo
        
        info = PluginInfo(
            scheme="test",
            name="Test Plugin",
            dialect="test_sql",
            dialect_label="Test SQL",
            description="Test plugin"
        )
        assert info.scheme == "test"
        assert isinstance(info.supported_features, list)
        assert isinstance(info.dsn_examples, list)


class TestTextToSQLStreamlitAPI:
    """
    DEPRECATED: Тесты для старого Text-to-SQL API (удалены)
    
    Text-to-SQL теперь работает через workflow pipeline.
    См.: workflow_pipelines/text_to_sql_pipeline.yaml
    """
    
    @pytest.mark.skip(reason="text_to_sql_streamlit_api удален, используйте workflow pipeline")
    def test_text_to_sql_manager_init(self):
        """DEPRECATED: Тест инициализации TextToSQLManager"""
        pass

    @pytest.mark.skip(reason="text_to_sql_streamlit_api удален, используйте workflow pipeline")
    def test_text_to_sql_request_dataclass(self):
        """DEPRECATED: Тест структуры данных TextToSQLRequest"""
        pass

    @pytest.mark.skip(reason="text_to_sql_streamlit_api удален, используйте workflow pipeline")
    def test_validate_sql_basic(self):
        """DEPRECATED: Тест базовой валидации SQL"""
        pass

    @pytest.mark.skip(reason="text_to_sql_streamlit_api удален, используйте workflow pipeline")
    def test_get_supported_dialects(self):
        """DEPRECATED: Тест получения поддерживаемых диалектов"""
        pass


class TestMemoryRAGStreamlitAPI:
    """Smoke-тесты для Memory/RAG API"""
    
    def test_memory_rag_manager_init(self):
        """Тест инициализации MemoryRAGManager"""
        from memory.streamlit_api import get_memory_rag_manager
        
        manager = get_memory_rag_manager()
        assert manager is not None
        assert hasattr(manager, 'get_memory_status')
        assert hasattr(manager, 'rebuild_memory')
        assert hasattr(manager, 'search_memory')

    def test_get_memory_status_basic(self):
        """Тест получения статуса памяти"""
        from memory.streamlit_api import get_memory_rag_manager
        
        manager = get_memory_rag_manager()
        status = manager.get_memory_status()
        
        assert hasattr(status, 'sqlite_available')
        assert hasattr(status, 'chromadb_available')
        assert hasattr(status, 'embedding_model_available')
        assert isinstance(status.tactical_memories_count, int)
        assert isinstance(status.strategic_memories_count, int)

    def test_memory_status_dataclass(self):
        """Тест структуры данных MemoryStatus"""
        from memory.streamlit_api import MemoryStatus
        
        status = MemoryStatus(
            sqlite_available=True,
            chromadb_available=False,
            embedding_model_available=False,
            sqlite_path="/test/path.db",
            chromadb_path="/test/chroma",
            embedding_model_name="test-model"
        )
        assert status.sqlite_available is True
        assert status.chromadb_available is False
        assert isinstance(status.collections_info, dict)

    def test_get_active_agents(self):
        """Тест получения списка активных агентов"""
        from memory.streamlit_api import get_memory_rag_manager
        
        manager = get_memory_rag_manager()
        agents = manager.get_active_agents()
        
        assert isinstance(agents, list)
        # Список может быть пустым, это нормально для smoke-теста


class TestTelemetryAPI:
    """Smoke-тесты для Telemetry API"""
    
    def test_telemetry_manager_init(self):
        """Тест инициализации SmolagentsTelemetryManager"""
        from telemetry.smolagents_telemetry import SmolagentsTelemetryManager
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = SmolagentsTelemetryManager(
                traces_dir=tmp_dir,
                enabled=False  # Отключаем для избежания зависимостей
            )
            assert manager is not None
            assert hasattr(manager, 'get_trace_files')
            assert hasattr(manager, 'read_trace_events')

    def test_get_telemetry_manager(self):
        """Тест получения глобального менеджера телеметрии"""
        from telemetry import get_telemetry_manager
        
        manager = get_telemetry_manager(enabled=False)
        assert manager is not None

    def test_trace_event_dataclass(self):
        """Тест структуры данных TraceEvent"""
        from telemetry.smolagents_telemetry import TraceEvent
        from datetime import datetime
        
        event = TraceEvent(
            run_id="test-123",
            span_id="span-456",
            parent_span_id=None,
            name="test_span",
            start_time=datetime.now(),
            end_time=None,
            duration_ms=None,
            status="ok",
            attributes={},
            events=[]
        )
        
        assert event.run_id == "test-123"
        assert event.span_id == "span-456"
        
        event_dict = event.to_dict()
        assert isinstance(event_dict, dict)
        assert "run_id" in event_dict


class TestUnifiedLoggingAPI:
    """Smoke-тесты для Unified Logging API"""
    
    def test_logging_manager_init(self):
        """Тест инициализации UnifiedLoggingManager"""
        from unified_logging import get_logging_manager
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = get_logging_manager(logs_dir=tmp_dir)
            assert manager is not None
            assert hasattr(manager, 'get_logger')
            assert hasattr(manager, 'emit_progress')

    def test_get_run_logger(self):
        """Тест получения логгера с run_id"""
        from unified_logging import get_run_logger
        
        logger = get_run_logger("test-run-123")
        assert logger is not None
        assert hasattr(logger, 'info')
        assert hasattr(logger, 'error')
        assert logger.run_id == "test-run-123"

    def test_emit_progress_basic(self):
        """Тест отправки события прогресса"""
        from unified_logging import emit_progress
        
        # Не должно падать
        emit_progress("test-run-123", "started", "test_component", {"test": "data"})

    def test_log_event_dataclass(self):
        """Тест структуры данных LogEvent"""
        from unified_logging import LogEvent
        from datetime import datetime
        
        event = LogEvent(
            run_id="test-123",
            timestamp=datetime.now(),
            level="INFO",
            logger_name="test_logger",
            message="Test message"
        )
        
        assert event.run_id == "test-123"
        assert event.level == "INFO"
        
        event_dict = event.to_dict()
        assert isinstance(event_dict, dict)
        assert "run_id" in event_dict


class TestConfigurationAPI:
    """Smoke-тесты для Configuration API"""
    
    def test_configuration_manager_init(self):
        """Тест инициализации ConfigurationManager"""
        from configuration_api import ConfigurationManager
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = os.path.join(tmp_dir, "test_config.yaml")
            manager = ConfigurationManager(config_file)
            assert manager is not None
            assert hasattr(manager, 'get_config')
            assert hasattr(manager, 'update_telemetry_config')

    def test_get_config_basic(self):
        """Тест получения конфигурации"""
        from configuration_api import get_configuration_manager
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = os.path.join(tmp_dir, "test_config.yaml")
            manager = get_configuration_manager(config_file)
            config = manager.get_config()
            
            assert config is not None
            assert hasattr(config, 'telemetry')
            assert hasattr(config, 'logging')
            assert hasattr(config, 'llm')
            assert hasattr(config, 'security')

    def test_configuration_dataclasses(self):
        """Тест структур данных конфигурации"""
        from configuration_api import (
            TelemetryConfig, LoggingConfig, LLMConfig, 
            SecurityConfig, ResourceLimits, UIConfig, SystemConfiguration
        )
        
        telemetry = TelemetryConfig()
        logging_cfg = LoggingConfig()
        llm = LLMConfig()
        security = SecurityConfig()
        limits = ResourceLimits()
        ui = UIConfig()
        
        config = SystemConfiguration(
            telemetry=telemetry,
            logging=logging_cfg,
            llm=llm,
            security=security,
            resource_limits=limits,
            ui=ui
        )
        
        assert config.telemetry.enabled is True  # Default value
        assert config.security.sql_execution_enabled is False  # Default value
        
        config_dict = config.to_dict()
        assert isinstance(config_dict, dict)
        assert "telemetry" in config_dict

    def test_get_environment_info(self):
        """Тест получения информации об окружении"""
        from configuration_api import get_configuration_manager
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = os.path.join(tmp_dir, "test_config.yaml")
            manager = get_configuration_manager(config_file)
            env_info = manager.get_environment_info()
            
            assert isinstance(env_info, dict)
            assert "python_version" in env_info
            assert "platform" in env_info
            assert "environment_variables" in env_info


class TestAPIIntegration:
    """Интеграционные smoke-тесты между API"""
    
    def test_all_managers_can_be_imported(self, stub_mcp_tools):
        """Тест что все менеджеры можно импортировать"""
        try:
            from workflow.streamlit_api import WorkflowManager
            from agent_streamlit_api import AgentManager
            from db_plugins.streamlit_api import get_db_plugin_manager
            # DEPRECATED: text_to_sql_streamlit_api удален, используйте workflow pipeline
# from text_to_sql_streamlit_api import get_text_to_sql_manager
            from memory.streamlit_api import get_memory_rag_manager
            from telemetry import get_telemetry_manager
            from unified_logging import get_logging_manager
            from configuration_api import get_configuration_manager
            
            # Все импорты прошли успешно
            assert True
            
        except ImportError as e:
            pytest.fail(f"Не удалось импортировать API: {e}")

    def test_managers_basic_initialization(self, stub_mcp_tools):
        """Тест базовой инициализации всех менеджеров"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                # Инициализируем все менеджеры
                from workflow.streamlit_api import WorkflowManager
                wf_manager = WorkflowManager(pipelines_dir=tmp_dir)
                
                from agent_streamlit_api import AgentManager
                agent_manager = AgentManager()
                
                from db_plugins.streamlit_api import get_db_plugin_manager
                db_manager = get_db_plugin_manager()
                
                # DEPRECATED: text_to_sql_streamlit_api удален, используйте workflow pipeline
                # sql_manager = get_text_to_sql_manager()  # Больше не используется
                
                from memory.streamlit_api import get_memory_rag_manager
                memory_manager = get_memory_rag_manager()
                
                from telemetry import get_telemetry_manager
                telemetry_manager = get_telemetry_manager(enabled=False)
                
                from unified_logging import get_logging_manager
                logging_manager = get_logging_manager(logs_dir=tmp_dir)
                
                from configuration_api import get_configuration_manager
                config_file = os.path.join(tmp_dir, "config.yaml")
                config_manager = get_configuration_manager(config_file)
                
                # Все менеджеры должны быть инициализированы
                # Note: sql_manager удален, используется workflow pipeline
                assert all([
                    wf_manager, agent_manager, db_manager,
                    memory_manager, telemetry_manager, logging_manager, config_manager
                ])
                
            except Exception as e:
                pytest.fail(f"Ошибка инициализации менеджеров: {e}")


if __name__ == "__main__":
    # Настраиваем логирование для тестов
    logging.basicConfig(
        level=logging.WARNING,  # Уменьшаем уровень логирования для тестов
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Запускаем тесты
    pytest.main([__file__, "-v"])
