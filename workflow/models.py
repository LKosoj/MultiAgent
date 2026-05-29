"""
Модели данных для Workflow Engine
===============================

Определяет структуры данных для описания, выполнения и мониторинга workflow.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Union
from datetime import datetime
from enum import Enum
import yaml
import json
from pathlib import Path

SUPPORTED_STEP_OUTPUT_SCHEMAS = frozenset({"json_object"})


def _coerce_pipeline_bool(value: Any, *, field_name: str) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"", "0", "false", "no", "off"}:
            return False
    raise ValueError(f"{field_name} must be boolean")


class WorkflowStatus(Enum):
    """Статусы выполнения workflow"""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(Enum):
    """Статусы выполнения шагов workflow"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


@dataclass
class RetryPolicy:
    """Политика повторных попыток для шагов workflow"""
    max_retries: int = 3
    backoff_strategy: str = "exponential"  # exponential, linear, fixed
    base_delay: float = 1.0  # секунды
    max_delay: float = 60.0  # секунды
    retry_on_errors: List[str] = field(default_factory=lambda: [
        "network_error", "rate_limit", "temporary_failure", "timeout"
    ])


@dataclass
class ResourceLimits:
    """Ограничения ресурсов для выполнения шагов"""
    max_memory_mb: Optional[int] = None
    max_duration_seconds: Optional[int] = None
    max_api_calls_per_minute: Optional[int] = None
    max_concurrent_steps: int = 1


@dataclass
class WorkflowStep:
    """Определение шага workflow"""
    id: str
    task: str
    depends_on: List[str] = field(default_factory=list)
    condition: Optional[str] = None  # Условие выполнения шага
    retry_policy: Optional[RetryPolicy] = None
    resource_limits: Optional[ResourceLimits] = None
    timeout: Optional[int] = None  # секунды
    rollback_action: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Новые поля для поддержки разных типов шагов
    step_type: str = "agent"  # "agent" или "tool"
    agent_type: Optional[str] = None  # Для step_type="agent"
    tool_name: Optional[str] = None   # Для step_type="tool"
    tool_params: Dict[str, Any] = field(default_factory=dict)  # Параметры инструмента

    # Cross-step feedback retry: после успешного выполнения шага, если условие
    # output_retry_policy.condition выполнено (например verifier=Rejected), движок
    # пакует output в context.variables[feedback_field] и заново запускает
    # rerun_step. Loop guard: max_iterations. Обрабатывает workflow.engine.
    # Намеренно НЕ кладём в metadata — metadata подставляется fail-fast ДО шага,
    # а condition ссылается на собственный output (placeholder ещё не resolved).
    output_retry_policy: Optional[Dict[str, Any]] = None

    # Декларативная схема ожидаемого output шага. На текущий момент поддерживается
    # только "json_object": движок парсит str-output через json.loads и сохраняет
    # dict в context.step_outputs (для корректного резолва {step.field} в condition
    # и task последующих шагов). Невалидный JSON → WorkflowStepError.
    output_schema: Optional[str] = None
    output_schema_requirements: Optional[Dict[str, Any]] = None


@dataclass
class WorkflowDefinition:
    """Полное определение workflow"""
    name: str
    version: str = "1.0"
    description: str = ""
    inputs: Dict[str, Any] = field(default_factory=dict)  # Инициирующие переменные
    outputs: Dict[str, Any] = field(default_factory=dict)  # Маппинг финального результата
    steps: List[WorkflowStep] = field(default_factory=list)
    global_retry_policy: Optional[RetryPolicy] = None
    global_resource_limits: Optional[ResourceLimits] = None
    error_handling: Dict[str, Any] = field(default_factory=dict)
    notifications: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Настройки параллельного выполнения
    parallel_execution: bool = False  # Включить параллельное выполнение шагов
    max_parallel_steps: int = 3  # Максимальное количество одновременно выполняемых шагов

    # Контракт пайплайна: если True, пайплайн требует EnhancedWorkflowEngine
    # (например, для output_retry_policy). WorkflowManager.start_workflow
    # делает fail-fast, если use_enhanced=False. Поле читается из yaml
    # секции `pipeline.requires_enhanced_engine`.
    requires_enhanced_engine: bool = False

    @classmethod
    def from_yaml(cls, yaml_path: Union[str, Path]) -> 'WorkflowDefinition':
        """Загрузка workflow definition из YAML файла"""
        with open(yaml_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data)
    
    @classmethod
    def from_yaml_string(cls, yaml_string: str) -> 'WorkflowDefinition':
        """Загрузка workflow definition из YAML строки"""
        data = yaml.safe_load(yaml_string)
        return cls.from_dict(data)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WorkflowDefinition':
        """Создание WorkflowDefinition из словаря"""
        # Обработка шагов
        steps = []
        for step_data in data.get('steps', []):
            # Обработка retry_policy
            retry_policy = None
            if 'retry_policy' in step_data:
                retry_data = step_data['retry_policy']
                retry_policy = RetryPolicy(
                    max_retries=retry_data.get('max_retries', 3),
                    backoff_strategy=retry_data.get('backoff_strategy', 'exponential'),
                    base_delay=retry_data.get('base_delay', 1.0),
                    max_delay=retry_data.get('max_delay', 60.0),
                    retry_on_errors=retry_data.get('retry_on_errors', [
                        "network_error", "rate_limit", "temporary_failure", "timeout"
                    ])
                )
            
            # Обработка resource_limits
            resource_limits = None
            if 'resource_limits' in step_data:
                limit_data = step_data['resource_limits']
                resource_limits = ResourceLimits(
                    max_memory_mb=limit_data.get('max_memory_mb'),
                    max_duration_seconds=limit_data.get('max_duration_seconds'),
                    max_api_calls_per_minute=limit_data.get('max_api_calls_per_minute'),
                    max_concurrent_steps=limit_data.get('max_concurrent_steps', 1)
                )
            
            output_schema = step_data.get('output_schema')
            if output_schema is not None and output_schema not in SUPPORTED_STEP_OUTPUT_SCHEMAS:
                raise ValueError(
                    f"Unsupported output_schema for step '{step_data['id']}': {output_schema!r}. "
                    f"Supported values: {sorted(SUPPORTED_STEP_OUTPUT_SCHEMAS)}"
                )

            # Определяем тип шага и соответствующие поля
            step_type = step_data.get('step_type', 'agent')
            agent_type = step_data.get('agent_type')
            tool_name = step_data.get('tool_name')
            tool_params = step_data.get('tool_params', {})
            
            # Для обратной совместимости: если agent_type указан, но step_type нет - это агент
            if agent_type and step_type == 'agent':
                step_type = 'agent'
            elif tool_name and not agent_type:
                step_type = 'tool'
            
            step = WorkflowStep(
                id=step_data['id'],
                task=step_data['task'],
                depends_on=step_data.get('depends_on', []),
                condition=step_data.get('condition'),
                retry_policy=retry_policy,
                resource_limits=resource_limits,
                timeout=step_data.get('timeout'),
                rollback_action=step_data.get('rollback_action'),
                metadata=step_data.get('metadata', {}),
                step_type=step_type,
                agent_type=agent_type,
                tool_name=tool_name,
                tool_params=tool_params,
                output_retry_policy=step_data.get('output_retry_policy'),
                output_schema=output_schema,
                output_schema_requirements=step_data.get('output_schema_requirements'),
            )
            steps.append(step)
        
        # Обработка global_retry_policy
        global_retry_policy = None
        if 'global_retry_policy' in data:
            retry_data = data['global_retry_policy']
            global_retry_policy = RetryPolicy(
                max_retries=retry_data.get('max_retries', 3),
                backoff_strategy=retry_data.get('backoff_strategy', 'exponential'),
                base_delay=retry_data.get('base_delay', 1.0),
                max_delay=retry_data.get('max_delay', 60.0),
                retry_on_errors=retry_data.get('retry_on_errors', [
                    "network_error", "rate_limit", "temporary_failure", "timeout"
                ])
            )
        
        # Обработка global_resource_limits
        global_resource_limits = None
        if 'global_resource_limits' in data:
            limit_data = data['global_resource_limits']
            global_resource_limits = ResourceLimits(
                max_memory_mb=limit_data.get('max_memory_mb'),
                max_duration_seconds=limit_data.get('max_duration_seconds'),
                max_api_calls_per_minute=limit_data.get('max_api_calls_per_minute'),
                max_concurrent_steps=limit_data.get('max_concurrent_steps', 1)
            )
        
        # Опциональная секция `pipeline:` с контрактными флагами уровня всего
        # пайплайна. Сейчас поддерживается только `requires_enhanced_engine`.
        pipeline_section = data.get('pipeline', {}) or {}
        requires_enhanced_engine = _coerce_pipeline_bool(
            pipeline_section.get('requires_enhanced_engine'),
            field_name='pipeline.requires_enhanced_engine',
        )

        return cls(
            name=data['name'],
            version=data.get('version', '1.0'),
            description=data.get('description', ''),
            inputs=data.get('inputs', {}),
            outputs=data.get('outputs', {}),
            steps=steps,
            global_retry_policy=global_retry_policy,
            global_resource_limits=global_resource_limits,
            error_handling=data.get('error_handling', {}),
            notifications=data.get('notifications', []),
            metadata=data.get('metadata', {}),
            parallel_execution=data.get('parallel_execution', False),
            max_parallel_steps=data.get('max_parallel_steps', 3),
            requires_enhanced_engine=requires_enhanced_engine,
        )
    
    def to_yaml(self, yaml_path: Union[str, Path]) -> None:
        """Сохранение workflow definition в YAML файл"""
        with open(yaml_path, 'w', encoding='utf-8') as f:
            yaml.dump(self.to_dict(), f, default_flow_style=False, allow_unicode=True, indent=2)
    
    def to_yaml_string(self) -> str:
        """Конвертация workflow definition в YAML строку"""
        return yaml.dump(self.to_dict(), default_flow_style=False, allow_unicode=True, indent=2)
    
    def to_dict(self) -> Dict[str, Any]:
        """Конвертация WorkflowDefinition в словарь"""
        data = {
            'name': self.name,
            'version': self.version,
            'description': self.description,
            'inputs': self.inputs,
            'outputs': self.outputs,
            'steps': []
        }
        if self.requires_enhanced_engine:
            data['pipeline'] = {'requires_enhanced_engine': True}
        
        # Конвертация шагов
        for step in self.steps:
            step_data = {
                'id': step.id,
                'task': step.task
            }
            
            # Добавляем поля в зависимости от типа шага
            if step.step_type != "agent":  # Указываем step_type только если он не по умолчанию
                step_data['step_type'] = step.step_type
                
            if step.step_type == "agent" and step.agent_type:
                step_data['agent_type'] = step.agent_type
            elif step.step_type == "tool":
                if step.tool_name:
                    step_data['tool_name'] = step.tool_name
                if step.tool_params:
                    step_data['tool_params'] = step.tool_params
            
            if step.depends_on:
                step_data['depends_on'] = step.depends_on
            if step.condition:
                step_data['condition'] = step.condition
            if step.timeout:
                step_data['timeout'] = step.timeout
            if step.rollback_action:
                step_data['rollback_action'] = step.rollback_action
            if step.metadata:
                step_data['metadata'] = step.metadata
            if step.output_retry_policy:
                step_data['output_retry_policy'] = step.output_retry_policy
            if step.output_schema:
                step_data['output_schema'] = step.output_schema
            if step.output_schema_requirements:
                step_data['output_schema_requirements'] = step.output_schema_requirements

            if step.retry_policy:
                step_data['retry_policy'] = {
                    'max_retries': step.retry_policy.max_retries,
                    'backoff_strategy': step.retry_policy.backoff_strategy,
                    'base_delay': step.retry_policy.base_delay,
                    'max_delay': step.retry_policy.max_delay,
                    'retry_on_errors': step.retry_policy.retry_on_errors
                }
            
            if step.resource_limits:
                limits = {}
                if step.resource_limits.max_memory_mb:
                    limits['max_memory_mb'] = step.resource_limits.max_memory_mb
                if step.resource_limits.max_duration_seconds:
                    limits['max_duration_seconds'] = step.resource_limits.max_duration_seconds
                if step.resource_limits.max_api_calls_per_minute:
                    limits['max_api_calls_per_minute'] = step.resource_limits.max_api_calls_per_minute
                if step.resource_limits.max_concurrent_steps != 1:
                    limits['max_concurrent_steps'] = step.resource_limits.max_concurrent_steps
                if limits:
                    step_data['resource_limits'] = limits
            
            data['steps'].append(step_data)
        
        # Добавление глобальных настроек
        if self.global_retry_policy:
            data['global_retry_policy'] = {
                'max_retries': self.global_retry_policy.max_retries,
                'backoff_strategy': self.global_retry_policy.backoff_strategy,
                'base_delay': self.global_retry_policy.base_delay,
                'max_delay': self.global_retry_policy.max_delay,
                'retry_on_errors': self.global_retry_policy.retry_on_errors
            }
        
        if self.global_resource_limits:
            limits = {}
            if self.global_resource_limits.max_memory_mb:
                limits['max_memory_mb'] = self.global_resource_limits.max_memory_mb
            if self.global_resource_limits.max_duration_seconds:
                limits['max_duration_seconds'] = self.global_resource_limits.max_duration_seconds
            if self.global_resource_limits.max_api_calls_per_minute:
                limits['max_api_calls_per_minute'] = self.global_resource_limits.max_api_calls_per_minute
            if self.global_resource_limits.max_concurrent_steps != 1:
                limits['max_concurrent_steps'] = self.global_resource_limits.max_concurrent_steps
            if limits:
                data['global_resource_limits'] = limits
        
        if self.error_handling:
            data['error_handling'] = self.error_handling
        if self.notifications:
            data['notifications'] = self.notifications
        if self.metadata:
            data['metadata'] = self.metadata
        
        # Добавляем настройки параллельного выполнения только если они не по умолчанию
        if self.parallel_execution:
            data['parallel_execution'] = self.parallel_execution
        if self.max_parallel_steps != 3:
            data['max_parallel_steps'] = self.max_parallel_steps
        
        return data


@dataclass
class StepResult:
    """Результат выполнения шага workflow"""
    step_id: str
    status: StepStatus
    output: Any = None
    error: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    attempt_number: int = 1
    agent_name: Optional[str] = None
    resource_usage: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Enhanced fields for intelligent workflow management
    quality_score: float = 0.0
    decision: str = "proceed"  # proceed|retry|alternate|escalate|stop
    decision_reason: str = ""
    expected_contract: Dict[str, Any] = field(default_factory=dict)
    actual_contract: Dict[str, Any] = field(default_factory=dict)
    retry_count: int = 0
    error_class: str = ""
    validator_results: List[Dict[str, Any]] = field(default_factory=list)
    policy_version: str = "default"
    provenance: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowContext:
    """Контекст выполнения workflow"""
    workflow_id: Optional[str] = None
    session_id: Optional[str] = None
    client_id: Optional[str] = None
    variables: Dict[str, Any] = field(default_factory=dict)
    step_outputs: Dict[str, Any] = field(default_factory=dict)
    current_step: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Автоматически генерируем workflow_id и session_id, если не указаны"""
        import uuid
        from datetime import datetime
        
        if self.workflow_id is None:
            # Генерируем уникальный workflow_id
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            short_uuid = uuid.uuid4().hex[:8]
            self.workflow_id = f"workflow_{timestamp}_{short_uuid}"
        
        if self.session_id is None:
            # Используем workflow_id как session_id, если не указан
            self.session_id = self.workflow_id


@dataclass
class WorkflowCheckpoint:
    """Checkpoint состояния workflow для восстановления"""
    workflow_id: str
    timestamp: datetime
    status: WorkflowStatus
    current_step: Optional[str] = None
    completed_steps: List[str] = field(default_factory=list)
    failed_steps: List[str] = field(default_factory=list)
    context: Optional[WorkflowContext] = None
    step_results: Dict[str, StepResult] = field(default_factory=dict)
    resumable: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowResult:
    """Итоговый результат выполнения workflow"""
    workflow_id: str
    status: WorkflowStatus
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    step_results: Dict[str, StepResult] = field(default_factory=dict)
    final_output: Any = None
    error: Optional[str] = None
    resource_usage_summary: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ResourceLease:
    """Lease ресурсов для выполнения workflow"""
    lease_id: str
    workflow_id: str
    allocated_memory_mb: int
    allocated_api_calls: int
    start_time: datetime
    expires_at: datetime
    active: bool = True


class WorkflowExecutionError(Exception):
    """Базовая ошибка выполнения workflow"""
    pass


class WorkflowStepError(Exception):
    """Ошибка выполнения шага workflow"""
    pass


class ResourceQuotaExceededError(Exception):
    """Ошибка превышения квоты ресурсов"""
    pass


class WorkflowNotFoundError(Exception):
    """Ошибка - workflow не найден"""
    pass


# Enhanced Workflow Models
@dataclass
class StepPlan:
    """План выполнения шага от Pre-Step Planner"""
    step_id: str
    refined_task: str
    expected_output_format: str
    quality_criteria: Dict[str, Any]
    resource_budget: Dict[str, Any]
    timeout_seconds: int
    retry_budget: int
    context_hints: List[str] = field(default_factory=list)
    fallback_strategies: List[str] = field(default_factory=list)


@dataclass 
class ValidationResult:
    """Результат валидации от Post-Step Judge"""
    step_id: str
    overall_score: float
    validation_passed: bool
    validator_results: List[Dict[str, Any]]
    error_class: str = ""
    improvement_suggestions: List[str] = field(default_factory=list)
    contract_compliance: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    """Решение от Decision Engine"""
    action: str  # proceed|retry|alternate|escalate|stop
    reason: str
    confidence: float
    suggested_modifications: Dict[str, Any] = field(default_factory=dict)
    resource_impact: Dict[str, Any] = field(default_factory=dict)
    next_step_hints: List[str] = field(default_factory=list)


@dataclass
class Policy:
    """Политика качества и управления"""
    name: str
    version: str
    quality_gates: Dict[str, Any]
    validation_rules: Dict[str, Any]
    retry_policies: Dict[str, Any]
    budgets: Dict[str, Any]
    escalation: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Contract:
    """Контракт артефакта (схема ожидаемого результата)"""
    name: str
    version: str
    schema: Dict[str, Any]
    business_rules: List[str]
    quality_thresholds: Dict[str, Any]
    validators: List[str] = field(default_factory=list)


# Enhanced Status Types
class DecisionType(Enum):
    """Типы решений Decision Engine"""
    PROCEED = "proceed"
    RETRY = "retry"
    ALTERNATE = "alternate" 
    ESCALATE = "escalate"
    STOP = "stop"
    HUMAN_REQUIRED = "human_required"


class ErrorClass(Enum):
    """Классы ошибок для intelligent retry"""
    TIMEOUT = "timeout"
    EMPTY_RESPONSE = "empty_response"
    LOW_QUALITY = "low_quality"
    VALIDATION_FAILED = "validation_failed"
    SECURITY_VIOLATION = "security_violation"
    RESOURCE_EXCEEDED = "resource_exceeded"
    UNKNOWN = "unknown"


class HILRequestStatus(Enum):
    """Статусы Human-in-the-Loop запросов"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class HILRequest:
    """Запрос на Human-in-the-Loop вмешательство"""
    request_id: str
    workflow_id: str
    step_id: str
    status: HILRequestStatus
    created_at: datetime
    context: Dict[str, Any]
    failed_attempts: List[StepResult]
    human_input: Optional[Dict[str, Any]] = None
    resolution_time: Optional[datetime] = None
    assigned_to: Optional[str] = None
