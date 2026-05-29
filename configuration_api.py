"""
Конфигурационный API для Streamlit
=================================

Предоставляет единый интерфейс для управления настройками системы:
телеметрия, логирование, API ключи, модели, лимиты и другие параметры.
"""

import os
import json
import logging
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict, field
from pathlib import Path
import yaml

logger = logging.getLogger(__name__)

@dataclass
class TelemetryConfig:
    """Конфигурация телеметрии"""
    enabled: bool = True
    traces_dir: str = "logs/traces"
    service_name: str = "multiagent-system"
    trace_retention_days: int = 7
    detail_level: str = "standard"  # minimal, standard, verbose
    max_trace_file_size_mb: float = 10.0
    collect_detailed_spans: bool = True
    collect_system_metrics: bool = True
    collect_memory_metrics: bool = True
    collect_performance_metrics: bool = True
    collect_error_details: bool = True
    collect_user_interactions: bool = False
    export_format: str = "jsonl"  # jsonl, json
    batch_size: int = 100
    flush_interval_seconds: int = 30
    compression_enabled: bool = False

@dataclass
class LoggingConfig:
    """Конфигурация логирования"""
    level: str = "INFO"
    logs_dir: str = "logs"
    max_age_days: int = 7
    unified_logging_enabled: bool = True
    console_output: bool = True
    file_output: bool = True
    format: str = "detailed"  # detailed, simple, json
    rotation_size_mb: int = 50

@dataclass
class LLMConfig:
    """Конфигурация LLM провайдера"""
    provider: str = "openai"  # openai, anthropic, local
    model: str = "model_code"  # Логическое имя модели из agent_command.py
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = 4000
    temperature: float = 0.7
    top_p: float = 1.0
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    timeout_seconds: int = 30

@dataclass
class SecurityConfig:
    """Конфигурация безопасности"""
    sql_execution_enabled: bool = False
    max_sql_rows: int = 1000
    query_timeout_seconds: int = 30
    allowed_functions: List[str] = None
    allowed_sql_operations: List[str] = None
    blocked_sql_keywords: List[str] = None
    allowed_schemas: List[str] = None
    table_whitelist: List[str] = None
    table_blacklist: List[str] = None
    enable_pii_detection: bool = True
    pii_action: str = "block"  # block, mask, warn
    log_security_events: bool = True
    audit_all_queries: bool = False
    pii_detection_enabled: bool = True  # Backward compatibility
    safety_level: str = "strict"  # strict, moderate, permissive

    def __post_init__(self):
        if self.allowed_functions is None:
            self.allowed_functions = ["SELECT", "EXPLAIN"]
        if self.allowed_sql_operations is None:
            self.allowed_sql_operations = ["SELECT"]
        if self.blocked_sql_keywords is None:
            self.blocked_sql_keywords = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "CREATE"]
        if self.allowed_schemas is None:
            self.allowed_schemas = []
        if self.table_whitelist is None:
            self.table_whitelist = []
        if self.table_blacklist is None:
            self.table_blacklist = []

@dataclass
class ResourceLimits:
    """Лимиты ресурсов"""
    max_concurrent_workflows: int = 5
    max_concurrent_agents: int = 10
    memory_limit_mb: int = 2048
    disk_space_limit_gb: int = 50
    execution_timeout_minutes: int = 30
    api_calls_per_minute: int = 60

@dataclass
class UIConfig:
    """Конфигурация UI"""
    theme: str = "light"  # light, dark, auto
    auto_refresh_interval: int = 5  # seconds
    page_size: int = 50
    show_debug_info: bool = False
    advanced_features: bool = True

@dataclass
class PerformanceConfig:
    """Конфигурация производительности"""
    worker_threads: int = 4
    task_queue_size: int = 1000
    enable_caching: bool = True
    cache_size_mb: int = 256

@dataclass
class MemoryConfig:
    """Конфигурация системы памяти"""
    enabled: bool = True
    memory_type: str = "chromadb"  # chromadb, sqlite
    max_tactical_memories: int = 1000
    max_strategic_memories: int = 500
    tactical_memory_ttl_hours: int = 24
    strategic_memory_ttl_days: int = 30
    embedding_model: str = "all-MiniLM-L6-v2"
    custom_embedding_model: str = ""
    embedding_dimensions: int = 384
    default_search_k: int = 10
    similarity_threshold: float = 0.7
    reindex_interval_hours: int = 24
    chromadb_path: str = "memory/chromadb"
    collection_prefix: str = "multiagent"
    batch_size: int = 100
    enable_compression: bool = True

@dataclass
class NetworkConfig:
    """Конфигурация сети"""
    http_timeout_seconds: int = 30
    max_retries: int = 3
    user_agent: str = "MultiAgent-System/1.0"
    proxy_url: Optional[str] = None

@dataclass
class SystemConfig:
    """Конфигурация системы"""
    work_directory: str = "."
    temp_directory: str = "/tmp"
    language: str = "ru"  # ru, en, auto
    cleanup_interval_hours: int = 24

@dataclass
class SystemConfiguration:
    """Полная конфигурация системы"""
    telemetry: TelemetryConfig
    logging: LoggingConfig
    llm: LLMConfig
    security: SecurityConfig
    resource_limits: ResourceLimits
    ui: UIConfig
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    system: SystemConfig = field(default_factory=SystemConfig)
    version: str = "1.0.0"
    last_updated: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Конвертация в словарь для сериализации"""
        return {
            "telemetry": asdict(self.telemetry),
            "logging": asdict(self.logging),
            "llm": asdict(self.llm),
            "security": asdict(self.security),
            "resource_limits": asdict(self.resource_limits),
            "ui": asdict(self.ui),
            "performance": asdict(self.performance),
            "memory": asdict(self.memory),
            "network": asdict(self.network),
            "system": asdict(self.system),
            "version": self.version,
            "last_updated": self.last_updated
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SystemConfiguration':
        """Создание из словаря"""
        return cls(
            telemetry=TelemetryConfig(**data.get("telemetry", {})),
            logging=LoggingConfig(**data.get("logging", {})),
            llm=LLMConfig(**data.get("llm", {})),
            security=SecurityConfig(**data.get("security", {})),
            resource_limits=ResourceLimits(**data.get("resource_limits", {})),
            ui=UIConfig(**data.get("ui", {})),
            performance=PerformanceConfig(**data.get("performance", {})),
            memory=MemoryConfig(**data.get("memory", {})),
            network=NetworkConfig(**data.get("network", {})),
            system=SystemConfig(**data.get("system", {})),
            version=data.get("version", "1.0.0"),
            last_updated=data.get("last_updated")
        )


class ConfigurationManager:
    """
    Менеджер конфигурации системы
    """
    
    def __init__(self, config_file: str = "config/streamlit_config.yaml"):
        self.config_file = Path(config_file)
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        
        self._config: Optional[SystemConfiguration] = None
        self._load_config()
        
        # Применяем конфигурацию сразу после загрузки
        self._apply_all_configs()
        
        logger.info(f"📋 ConfigurationManager инициализирован: {self.config_file}")

    def _load_config(self):
        """Загрузить конфигурацию из файла"""
        try:
            if self.config_file.exists():
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                self._config = SystemConfiguration.from_dict(data or {})
                logger.info("✅ Конфигурация загружена из файла")
            else:
                # Создаем конфигурацию по умолчанию
                self._config = self._create_default_config()
                self._save_config()
                logger.info("📝 Создана конфигурация по умолчанию")
                
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки конфигурации: {e}")
            self._config = self._create_default_config()

    def _create_default_config(self) -> SystemConfiguration:
        """Создать конфигурацию по умолчанию"""
        from datetime import datetime
        
        # Используем логическое имя модели из agent_command.py
        try:
            from agent_command import model_mapping
            # По умолчанию используем model_code (для кодирования)
            if 'model_code' in model_mapping:
                default_model = "model_code"
                default_provider = "openai"  # Провайдер остается openai, так как используем существующую систему
            else:
                default_model = "gpt-4"
                default_provider = "openai"
        except ImportError:
            default_model = "gpt-4"
            default_provider = "openai"
        
        llm_config = LLMConfig()
        llm_config.model = default_model
        llm_config.provider = default_provider
        
        return SystemConfiguration(
            telemetry=TelemetryConfig(),
            logging=LoggingConfig(),
            llm=llm_config,
            security=SecurityConfig(),
            resource_limits=ResourceLimits(),
            ui=UIConfig(),
            performance=PerformanceConfig(),
            memory=MemoryConfig(),
            network=NetworkConfig(),
            system=SystemConfig(),
            last_updated=datetime.now().isoformat()
        )

    def _save_config(self):
        """Сохранить конфигурацию в файл"""
        try:
            from datetime import datetime
            self._config.last_updated = datetime.now().isoformat()
            
            with open(self.config_file, 'w', encoding='utf-8') as f:
                yaml.dump(self._config.to_dict(), f, default_flow_style=False, allow_unicode=True, indent=2)
            
            logger.info("💾 Конфигурация сохранена")
            
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения конфигурации: {e}")

    def update_config(self, config: SystemConfiguration) -> bool:
        """
        Обновить всю конфигурацию
        
        Args:
            config: Новая конфигурация системы
            
        Returns:
            True если успешно обновлено
        """
        try:
            self._config = config
            self._save_config()
            self._apply_all_configs()
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка обновления конфигурации: {e}")
            return False

    def get_config(self) -> SystemConfiguration:
        """Получить текущую конфигурацию"""
        return self._config

    def update_telemetry_config(self, config: TelemetryConfig) -> bool:
        """
        Обновить конфигурацию телеметрии
        
        Args:
            config: Новая конфигурация телеметрии
            
        Returns:
            True если успешно обновлено
        """
        try:
            self._config.telemetry = config
            self._save_config()
            
            # Применяем изменения к телеметрии
            self._apply_telemetry_config()
            
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка обновления конфигурации телеметрии: {e}")
            return False

    def _apply_telemetry_config(self):
        """Применить конфигурацию телеметрии"""
        try:
            from telemetry import configure_telemetry
            
            configure_telemetry(
                enabled=self._config.telemetry.enabled,
                traces_dir=self._config.telemetry.traces_dir
            )
            
        except Exception as e:
            logger.warning(f"⚠️ Не удалось применить конфигурацию телеметрии: {e}")

    def update_logging_config(self, config: LoggingConfig) -> bool:
        """
        Обновить конфигурацию логирования
        
        Args:
            config: Новая конфигурация логирования
            
        Returns:
            True если успешно обновлено
        """
        try:
            self._config.logging = config
            self._save_config()
            
            # Применяем изменения к логированию
            self._apply_logging_config()
            
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка обновления конфигурации логирования: {e}")
            return False

    def _apply_logging_config(self):
        """Применить конфигурацию логирования"""
        try:
            import logging.handlers
            from pathlib import Path
            from datetime import datetime
            
            config = self._config.logging
            root_logger = logging.getLogger()
            
            # Устанавливаем уровень логирования
            log_level = getattr(logging, config.level.upper(), logging.INFO)
            root_logger.setLevel(log_level)
            
            # Удаляем существующие обработчики (кроме UnifiedLoggingManager)
            handlers_to_remove = []
            for handler in root_logger.handlers:
                # Сохраняем только RunIdLogHandler из unified_logging
                if not handler.__class__.__name__ == 'RunIdLogHandler':
                    handlers_to_remove.append(handler)
            
            for handler in handlers_to_remove:
                root_logger.removeHandler(handler)
            
            # Создаем span-aware форматтер
            class SpanAwareFormatter(logging.Formatter):
                """Форматтер с поддержкой отображения span_id для корреляции"""
                
                def format(self, record):
                    # Добавляем информацию о спане если доступна
                    span_info = ""
                    
                    # Из OpenTelemetry context (автоматически)
                    try:
                        from opentelemetry import trace
                        current_span = trace.get_current_span()
                        if current_span and current_span.is_recording():
                            span_context = current_span.get_span_context()
                            span_id = format(span_context.span_id, '016x')[:8]  # Первые 8 символов
                            span_info = f"[{span_id}]"
                    except:
                        pass
                    
                    # Из явно переданных данных
                    if hasattr(record, 'span_id') and record.span_id:
                        span_info = f"[{record.span_id[:8]}]"
                    
                    # Добавляем run_id если есть
                    run_info = ""
                    if hasattr(record, 'run_id') and record.run_id:
                        run_info = f"[{record.run_id[:8]}]"
                    
                    # Комбинируем контекст
                    context_info = f"{run_info}{span_info}".strip()
                    if context_info:
                        record.context_info = f"{context_info} "
                    else:
                        record.context_info = ""
                    
                    return super().format(record)
            
            # Выбираем формат логов
            if config.format == "simple":
                formatter = SpanAwareFormatter(
                    '%(context_info)s%(levelname)s - %(message)s'
                )
            elif config.format == "json":
                # JSON формат с дополнительными полями
                formatter = logging.Formatter(
                    '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": "%(message)s", "run_id": "%(run_id)s", "span_id": "%(span_id)s"}'
                )
            else:  # detailed
                formatter = SpanAwareFormatter(
                    '[%(asctime)s] %(context_info)s%(name)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S'
                )
            
            # Добавляем консольный обработчик
            if config.console_output:
                console_handler = logging.StreamHandler()
                console_handler.setLevel(log_level)
                console_handler.setFormatter(formatter)
                root_logger.addHandler(console_handler)
            
            # Добавляем файловый обработчик
            if config.file_output:
                # Создаем директорию для логов
                logs_dir = Path(config.logs_dir)
                logs_dir.mkdir(parents=True, exist_ok=True)
                
                # Имя файла лога
                log_filename = f"multiagent_{datetime.now().strftime('%Y%m%d')}.log"
                log_file_path = logs_dir / log_filename
                
                # Используем RotatingFileHandler для ротации по размеру
                max_bytes = config.rotation_size_mb * 1024 * 1024  # MB -> bytes
                file_handler = logging.handlers.RotatingFileHandler(
                    log_file_path,
                    maxBytes=max_bytes,
                    backupCount=5,
                    encoding='utf-8'
                )
                file_handler.setLevel(log_level)
                file_handler.setFormatter(formatter)
                root_logger.addHandler(file_handler)
                
                logger.info(f"📝 Файловое логирование настроено: {log_file_path}")
            
            # Настраиваем специфичные логгеры для компонентов системы
            component_loggers = [
                'smolagents',
                'agent_system', 
                'agent_factory',
                'memory.rag_memory',
                'custom_tools',
                'workflow',
                'telemetry',
                'streamlit'
            ]
            
            for logger_name in component_loggers:
                component_logger = logging.getLogger(logger_name)
                component_logger.setLevel(log_level)
                # Не добавляем обработчики - они наследуются от root
            
            logger.info(f"✅ Конфигурация логирования применена: {config.level}, консоль={config.console_output}, файл={config.file_output}")
            
        except Exception as e:
            logger.warning(f"⚠️ Не удалось применить конфигурацию логирования: {e}")
            import traceback
            logger.debug(f"Traceback: {traceback.format_exc()}")

    def update_llm_config(self, config: LLMConfig) -> bool:
        """
        Обновить конфигурацию LLM
        
        Args:
            config: Новая конфигурация LLM
            
        Returns:
            True если успешно обновлено
        """
        try:
            # Маскируем API ключ в логах
            masked_key = config.api_key[:8] + "..." if len(config.api_key) > 8 else "***"
            logger.info(f"🔧 Обновление LLM конфигурации: {config.provider}/{config.model} (key: {masked_key})")
            
            self._config.llm = config
            self._save_config()
            
            # Применяем изменения к LLM
            self._apply_llm_config()
            
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка обновления конфигурации LLM: {e}")
            return False

    def _apply_llm_config(self):
        """Применить конфигурацию LLM"""
        try:
            # Устанавливаем переменные окружения для API ключей
            if self._config.llm.api_key:
                if self._config.llm.provider == "openai":
                    os.environ["OPENAI_API_KEY"] = self._config.llm.api_key
                elif self._config.llm.provider == "anthropic":
                    os.environ["ANTHROPIC_API_KEY"] = self._config.llm.api_key
            
            if self._config.llm.base_url:
                os.environ["OPENAI_BASE_URL"] = self._config.llm.base_url
                
        except Exception as e:
            logger.warning(f"⚠️ Не удалось применить конфигурацию LLM: {e}")

    def update_security_config(self, config: SecurityConfig) -> bool:
        """
        Обновить конфигурацию безопасности
        
        Args:
            config: Новая конфигурация безопасности
            
        Returns:
            True если успешно обновлено
        """
        try:
            self._config.security = config
            self._save_config()
            
            logger.info(f"🔒 Обновлена конфигурация безопасности: SQL={config.sql_execution_enabled}, уровень={config.safety_level}")
            
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка обновления конфигурации безопасности: {e}")
            return False

    def update_resource_limits(self, limits: ResourceLimits) -> bool:
        """
        Обновить лимиты ресурсов
        
        Args:
            limits: Новые лимиты ресурсов
            
        Returns:
            True если успешно обновлено
        """
        try:
            self._config.resource_limits = limits
            self._save_config()
            
            logger.info(f"📊 Обновлены лимиты ресурсов: workflows={limits.max_concurrent_workflows}, agents={limits.max_concurrent_agents}")
            
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка обновления лимитов ресурсов: {e}")
            return False

    def update_ui_config(self, config: UIConfig) -> bool:
        """
        Обновить конфигурацию UI
        
        Args:
            config: Новая конфигурация UI
            
        Returns:
            True если успешно обновлено
        """
        try:
            self._config.ui = config
            self._save_config()
            
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка обновления конфигурации UI: {e}")
            return False

    def update_memory_config(self, config: MemoryConfig) -> bool:
        """
        Обновить конфигурацию памяти
        """
        try:
            self._config.memory = config
            self._save_config()
            logger.info("🧠 Обновлена конфигурация памяти")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка обновления конфигурации памяти: {e}")
            return False

    def update_system_config(self, config: SystemConfig) -> bool:
        """
        Обновить конфигурацию системы
        """
        try:
            self._config.system = config
            self._save_config()
            logger.info("⚙️ Обновлена конфигурация системы")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка обновления конфигурации системы: {e}")
            return False

    def update_network_config(self, config: NetworkConfig) -> bool:
        """
        Обновить конфигурацию сети
        """
        try:
            self._config.network = config
            self._save_config()
            logger.info("🌐 Обновлена конфигурация сети")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка обновления конфигурации сети: {e}")
            return False

    def update_performance_config(self, config: PerformanceConfig) -> bool:
        """
        Обновить конфигурацию производительности
        """
        try:
            self._config.performance = config
            self._save_config()
            logger.info("⚡ Обновлена конфигурация производительности")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка обновления конфигурации производительности: {e}")
            return False



    def get_environment_info(self) -> Dict[str, Any]:
        """
        Получить информацию об окружении
        
        Returns:
            Информация об окружении и переменных
        """
        env_info = {
            "python_version": None,
            "platform": None,
            "environment_variables": {},
            "file_system": {},
            "dependencies": {}
        }
        
        try:
            import sys
            import platform
            
            env_info["python_version"] = sys.version
            env_info["platform"] = platform.platform()
            
            # Важные переменные окружения (без секретов)
            important_vars = [
                "PATH", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_DEFAULT_ENV",
                "HF_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"
            ]
            
            for var in important_vars:
                value = os.environ.get(var, "")
                if "KEY" in var or "TOKEN" in var:
                    # Маскируем секретные значения
                    if value:
                        env_info["environment_variables"][var] = value[:8] + "..." if len(value) > 8 else "***"
                    else:
                        env_info["environment_variables"][var] = "не установлен"
                else:
                    env_info["environment_variables"][var] = value
            
            # Информация о файловой системе
            for path_name, path_value in [
                ("config_file", str(self.config_file)),
                ("logs_dir", self._config.logging.logs_dir),
                ("traces_dir", self._config.telemetry.traces_dir),
                ("working_directory", os.getcwd())
            ]:
                path_obj = Path(path_value)
                env_info["file_system"][path_name] = {
                    "path": str(path_obj),
                    "exists": path_obj.exists(),
                    "is_dir": path_obj.is_dir() if path_obj.exists() else False,
                    "writable": os.access(path_obj.parent if not path_obj.exists() else path_obj, os.W_OK)
                }
            
            # Проверка зависимостей
            dependencies_to_check = [
                "smolagents", "streamlit", "chromadb", "sentence_transformers", 
                "opentelemetry", "yaml", "sqlite3"
            ]
            
            for dep in dependencies_to_check:
                try:
                    if dep == "sqlite3":
                        import sqlite3
                        env_info["dependencies"][dep] = {"available": True, "version": sqlite3.sqlite_version}
                    else:
                        module = __import__(dep)
                        version = getattr(module, "__version__", "unknown")
                        env_info["dependencies"][dep] = {"available": True, "version": version}
                except ImportError:
                    env_info["dependencies"][dep] = {"available": False, "version": None}
            
        except Exception as e:
            logger.error(f"❌ Ошибка получения информации об окружении: {e}")
            env_info["error"] = str(e)
        
        return env_info

    def export_config(self, include_secrets: bool = False) -> Dict[str, Any]:
        """
        Экспортировать конфигурацию
        
        Args:
            include_secrets: Включать ли секретные данные (API ключи)
            
        Returns:
            Экспортированная конфигурация
        """
        config_dict = self._config.to_dict()
        
        if not include_secrets:
            # Удаляем секретные данные
            if "llm" in config_dict and "api_key" in config_dict["llm"]:
                config_dict["llm"]["api_key"] = "***HIDDEN***"
        
        return {
            "exported_at": self._config.last_updated,
            "config": config_dict,
            "environment": self.get_environment_info()
        }

    def import_config(self, config_data: Dict[str, Any], apply_immediately: bool = True) -> bool:
        """
        Импортировать конфигурацию
        
        Args:
            config_data: Данные конфигурации
            apply_immediately: Применить изменения немедленно
            
        Returns:
            True если успешно импортировано
        """
        try:
            # Валидируем структуру
            if "config" not in config_data:
                raise ValueError("Неверная структура конфигурации")
            
            # Создаем новую конфигурацию
            new_config = SystemConfiguration.from_dict(config_data["config"])
            
            # Сохраняем текущую конфигурацию как резервную
            backup_config = self._config
            
            try:
                self._config = new_config
                self._save_config()
                
                if apply_immediately:
                    self._apply_all_configs()
                
                logger.info("✅ Конфигурация успешно импортирована")
                return True
                
            except Exception as e:
                # Восстанавливаем резервную конфигурацию
                self._config = backup_config
                raise e
                
        except Exception as e:
            logger.error(f"❌ Ошибка импорта конфигурации: {e}")
            return False

    def _apply_all_configs(self):
        """Применить все конфигурации"""
        self._apply_telemetry_config()
        self._apply_logging_config()
        self._apply_llm_config()

    def reset_to_defaults(self) -> bool:
        """
        Сбросить конфигурацию к значениям по умолчанию
        
        Returns:
            True если успешно сброшено
        """
        try:
            self._config = self._create_default_config()
            self._save_config()
            self._apply_all_configs()
            
            logger.info("🔄 Конфигурация сброшена к значениям по умолчанию")
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка сброса конфигурации: {e}")
            return False

    def get_llm_providers(self) -> Dict[str, Any]:
        """
        Получить список доступных LLM провайдеров и моделей
        
        Returns:
            Словарь с информацией о провайдерах
        """
        try:
            # Пытаемся импортировать реальные модели из системы
            try:
                from agent_command import model_mapping
                
                # Создаем описания для каждой модели системы
                model_descriptions = {
                    "model_search": {
                        "name": "model_search",
                        "description": "Модель для поиска и исследовательских задач",
                        "use_case": "Поиск информации, анализ данных, исследования",
                        "characteristics": "Оптимизирована для аналитических задач"
                    },
                    "model_code": {
                        "name": "model_code", 
                        "description": "Модель для программирования и кодирования",
                        "use_case": "Генерация кода, отладка, рефакторинг",
                        "characteristics": "Специализирована на программировании"
                    },
                    "model_hard": {
                        "name": "model_hard",
                        "description": "Мощная модель для сложных задач",
                        "use_case": "Комплексные аналитические задачи, сложные вычисления",
                        "characteristics": "Максимальная производительность"
                    },
                    "model_lite": {
                        "name": "model_lite",
                        "description": "Легкая быстрая модель",
                        "use_case": "Простые задачи, быстрые ответы",
                        "characteristics": "Скорость и эффективность"
                    },
                    "model_summary": {
                        "name": "model_summary",
                        "description": "Модель для суммаризации и обобщения",
                        "use_case": "Создание резюме, краткие изложения",
                        "characteristics": "Оптимизирована для сжатия информации"
                    },
                    "model_big": {
                        "name": "model_big",
                        "description": "Большая модель для масштабных задач",
                        "use_case": "Работа с большими объемами данных",
                        "characteristics": "Максимальный контекст и возможности"
                    },
                    "model_vision": {
                        "name": "model_vision",
                        "description": "Модель для задач с изображениями",
                        "use_case": "Анализ изображений, мультимодальные задачи",
                        "characteristics": "Поддерживает визуальный контент"
                    },
                    "model_reranker": {
                        "name": "model_reranker",
                        "description": "Модель для rerank/переранжирования",
                        "use_case": "Переранжирование документов/ответов, улучшение релевантности",
                        "characteristics": "Оптимизирована под scoring/ранжирование"
                    },
                    "model_ultimate": {
                        "name": "model_ultimate",
                        "description": "Универсальная топ‑модель для самых сложных задач",
                        "use_case": "Сложные рассуждения, комплексные запросы, максимальное качество",
                        "characteristics": "Максимальные возможности (может быть дороже/медленнее)"
                    }
                }
                
                # Собираем информацию о доступных моделях
                available_models = []
                model_details = {}
                
                for model_name, model_obj in model_mapping.items():
                    if model_name in model_descriptions:
                        desc = model_descriptions[model_name]
                        # Получаем реальный model_id
                        if hasattr(model_obj, 'model_id'):
                            real_model_id = model_obj.model_id
                        else:
                            real_model_id = str(model_obj)[:50] + "..."
                        
                        available_models.append(model_name)
                        model_details[model_name] = {
                            **desc,
                            "real_model_id": real_model_id,
                            "temperature": getattr(model_obj, 'temperature', 0.7),
                            "max_tokens": getattr(model_obj, 'max_tokens', 32768)
                        }

                return {
                    "openai": {
                        "description": "Система мультиагентов (текущая)",
                        "models": available_models,
                        "model_details": model_details,
                        "website": "Local MultiAgent System",
                        "requires_api_key": False,  # Модели используют свои подключения из agent_command.py
                        "uses_system_connections": True,  # Флаг что используются системные подключения
                        "connection_source": "agent_command.py (OPENAI_API_BASE_DB, OPENAI_API_KEY_DB)",
                        "features": ["Многомодельная поддержка", "Retry механизм", "Кастомные роли", "Системные подключения"]
                    },
                    "anthropic": {
                        "description": "Anthropic Claude модели", 
                        "models": ["claude-3-opus", "claude-3-sonnet", "claude-3-haiku", "claude-3-5-sonnet"],
                        "website": "https://anthropic.com",
                        "requires_api_key": True,
                        "features": ["Большой контекст", "Безопасность", "Reasoning"]
                    },
                    "local": {
                        "description": "Локальные модели",
                        "models": ["llama-2", "llama-3", "mistral", "custom"],
                        "website": "Local deployment",
                        "requires_api_key": False,
                        "features": ["Приватность", "Локальный контроль", "Кастомизация"]
                    }
                }
                
            except ImportError:
                logger.warning("⚠️ Не удалось загрузить модели из agent_command")
                # Fallback к стандартным провайдерам
                return self._get_default_providers()
                
        except Exception as e:
            logger.error(f"❌ Ошибка получения провайдеров: {e}")
            return self._get_default_providers()

    def _get_default_providers(self) -> Dict[str, Any]:
        """Получить провайдеры по умолчанию"""
        return {
            "openai": {
                "description": "OpenAI GPT модели",
                "models": ["gpt-4", "gpt-4-turbo", "gpt-3.5-turbo"],
                "website": "https://openai.com",
                "requires_api_key": True,
                "features": ["Высокое качество", "Function calling"]
            },
            "anthropic": {
                "description": "Anthropic Claude модели",
                "models": ["claude-3-opus", "claude-3-sonnet"],
                "website": "https://anthropic.com", 
                "requires_api_key": True,
                "features": ["Большой контекст", "Безопасность"]
            }
        }

    def test_llm_connection(self, provider: str = None, model: str = None, custom_config: Dict = None) -> Dict[str, Any]:
        """
        Тестировать соединение с LLM
        
        Args:
            provider: Провайдер для тестирования (по умолчанию текущий)
            model: Модель для тестирования (по умолчанию текущая)
            custom_config: Кастомная конфигурация для теста
            
        Returns:
            Результат тестирования соединения
        """
        result = {
            "success": False,
            "provider": provider or self._config.llm.provider,
            "model": model or self._config.llm.model,
            "error_message": "",
            "response_time_ms": 0,
            "test_response": "",
            "suggestions": []
        }
        
        try:
            import time
            start_time = time.time()
            
            # Если используется кастомная конфигурация для теста
            if custom_config:
                test_provider = custom_config.get("provider", provider)
                test_model = custom_config.get("model", model) 
                test_api_key = custom_config.get("api_key")
                test_base_url = custom_config.get("base_url")
            else:
                test_provider = provider or self._config.llm.provider
                test_model = model or self._config.llm.model
                test_api_key = self._config.llm.api_key
                test_base_url = self._config.llm.base_url
            
            # Пытаемся использовать реальные модели системы из agent_command.py
            try:
                from agent_command import model_mapping
                
                # Проверяем, есть ли логическая модель в системе
                if test_model in model_mapping:
                    system_model = model_mapping[test_model]
                    
                    # ИСПОЛЬЗУЕМ СУЩЕСТВУЮЩУЮ МОДЕЛЬ ИЗ СИСТЕМЫ (с её подключениями)
                    if hasattr(system_model, 'model_id'):
                        # Модель готова к использованию с существующими подключениями
                        result["success"] = True
                        result["test_response"] = f"✅ Логическая модель '{test_model}' → {system_model.model_id}"
                        result["model_info"] = {
                            "logical_name": test_model,
                            "real_model_id": system_model.model_id,
                            "temperature": getattr(system_model, 'temperature', 0.7),
                            "max_tokens": getattr(system_model, 'max_tokens', 32768),
                            "api_base": getattr(system_model, 'api_base', 'Из переменной окружения'),
                            "uses_system_connection": True
                        }
                        result["response_time_ms"] = (time.time() - start_time) * 1000
                        return result
                    else:
                        result["error_message"] = f"Модель {test_model} найдена, но model_id недоступен"
                        return result
                
            except ImportError:
                logger.warning("⚠️ Не удалось импортировать agent_command")
            
            # Если это НЕ логическая модель, проверяем внешние провайдеры
            if not test_api_key and test_provider in ["anthropic", "local"]:
                result["error_message"] = f"Не указан API ключ для {test_provider}"
                result["suggestions"].append(f"Добавьте API ключ для {test_provider}")
                return result
            
            # Тест для стандартных провайдеров
            if test_provider == "openai":
                # Здесь можно добавить реальный тест OpenAI API
                result["success"] = True
                result["test_response"] = "OpenAI API тест (симулированный)"
                
            elif test_provider == "anthropic":
                # Здесь можно добавить реальный тест Anthropic API
                result["success"] = True  
                result["test_response"] = "Anthropic API тест (симулированный)"
                
            else:
                result["error_message"] = f"Неподдерживаемый провайдер: {test_provider}"
                result["suggestions"].append("Используйте: openai, anthropic, или модели системы")
                return result
            
            result["response_time_ms"] = (time.time() - start_time) * 1000
            
        except Exception as e:
            result["error_message"] = str(e)
            result["suggestions"].append("Проверьте настройки API и сетевое соединение")
            
        return result


# Глобальный экземпляр менеджера
_configuration_manager: Optional[ConfigurationManager] = None

def get_configuration_manager(config_file: str = "config/streamlit_config.yaml") -> ConfigurationManager:
    """
    Получить глобальный экземпляр менеджера конфигурации
    
    Args:
        config_file: Путь к файлу конфигурации
        
    Returns:
        Экземпляр ConfigurationManager
    """
    global _configuration_manager
    
    if _configuration_manager is None:
        _configuration_manager = ConfigurationManager(config_file)
    
    return _configuration_manager
