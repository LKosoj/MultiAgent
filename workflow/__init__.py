"""MultiAgent Workflow Engine package."""

__all__ = [
    'WorkflowEngine',
    'WorkflowStateManager', 
    'RetryEngine',
    'ResourceManager',
    'WorkflowDefinition',
    'WorkflowResult',
    'WorkflowContext',
    'EventDrivenWorkflowEngine',
    'AgentTrigger',
    'TriggerManager',
    'TriggerType',
    'ContextEnrichment',
    'MemoryQuery',
    'TemporalEngine',
    'Timer',
    'Signal',
    'WorkflowTimer',
    'TimerStatus',
    'SignalStatus',
]

__version__ = "1.0.0"

_LAZY_EXPORTS = {
    "WorkflowEngine": ("workflow.engine", "WorkflowEngine"),
    "WorkflowStateManager": ("workflow.state_manager", "WorkflowStateManager"),
    "RetryEngine": ("workflow.retry_engine", "RetryEngine"),
    "ResourceManager": ("workflow.resource_manager", "ResourceManager"),
    "WorkflowDefinition": ("workflow.models", "WorkflowDefinition"),
    "WorkflowResult": ("workflow.models", "WorkflowResult"),
    "WorkflowContext": ("workflow.models", "WorkflowContext"),
    "EventDrivenWorkflowEngine": ("workflow.event_driven", "EventDrivenWorkflowEngine"),
    "AgentTrigger": ("workflow.event_driven", "AgentTrigger"),
    "TriggerManager": ("workflow.event_driven", "TriggerManager"),
    "TriggerType": ("workflow.event_driven", "TriggerType"),
    "ContextEnrichment": ("workflow.event_driven", "ContextEnrichment"),
    "MemoryQuery": ("workflow.event_driven", "MemoryQuery"),
    "TemporalEngine": ("workflow.temporal", "TemporalEngine"),
    "Timer": ("workflow.temporal", "Timer"),
    "Signal": ("workflow.temporal", "Signal"),
    "WorkflowTimer": ("workflow.temporal", "WorkflowTimer"),
    "TimerStatus": ("workflow.temporal", "TimerStatus"),
    "SignalStatus": ("workflow.temporal", "SignalStatus"),
}


def __getattr__(name):
    if name not in _LAZY_EXPORTS:
        raise AttributeError(name)
    from importlib import import_module

    module_name, attr_name = _LAZY_EXPORTS[name]
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
